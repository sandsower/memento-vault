import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";

interface LifecycleResult {
	should_inject: boolean;
	content: string;
	source: string;
	results: Array<Record<string, unknown>>;
	reason?: string;
	metadata?: Record<string, unknown>;
}

interface BridgeConfig {
	enabled: boolean;
	briefing: boolean;
	promptRecall: boolean;
	toolContext: boolean;
	autoCapture: boolean;
	captureQueue: boolean;
	processQueue: boolean;
	processQueueOnSessionClose: boolean;
	processQueueMaxCaptures: number;
	processQueueModel?: string | null;
	maxInjectedChars: number;
	maxToolContextPerSession: number;
}

const defaultConfig: BridgeConfig = {
	enabled: true,
	briefing: true,
	promptRecall: true,
	toolContext: false,
	autoCapture: false,
	captureQueue: true,
	processQueue: true,
	processQueueOnSessionClose: false,
	processQueueMaxCaptures: 3,
	processQueueModel: null,
	maxInjectedChars: 4000,
	maxToolContextPerSession: 5,
};

interface LoadedBridgeConfig {
	config: BridgeConfig;
	sources: string[];
}

function envBool(name: string): boolean | undefined {
	const raw = process.env[name];
	if (raw === undefined) return undefined;
	return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

function envInt(name: string): number | undefined {
	const raw = process.env[name];
	if (raw === undefined) return undefined;
	const parsed = Number.parseInt(raw, 10);
	return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

function readJson(path: string): unknown | undefined {
	if (!existsSync(path)) return undefined;
	return JSON.parse(readFileSync(path, "utf8"));
}

function bridgeConfigFrom(raw: unknown): Partial<BridgeConfig> {
	const root = raw as Record<string, unknown> | undefined;
	const memento = root?.memento as Record<string, unknown> | undefined;
	const candidate = (memento?.piBridge ?? root?.piBridge ?? root) as Record<string, unknown> | undefined;
	const partial: Partial<BridgeConfig> = {};
	if (!candidate) return partial;

	for (const key of ["enabled", "briefing", "promptRecall", "toolContext", "autoCapture", "captureQueue", "processQueue", "processQueueOnSessionClose"] as const) {
		if (typeof candidate[key] === "boolean") partial[key] = candidate[key];
	}
	for (const key of ["maxInjectedChars", "maxToolContextPerSession", "processQueueMaxCaptures"] as const) {
		if (typeof candidate[key] === "number" && Number.isFinite(candidate[key]) && candidate[key] >= 0) partial[key] = candidate[key];
	}
	if (typeof candidate.processQueueModel === "string" || candidate.processQueueModel === null) partial.processQueueModel = candidate.processQueueModel;
	return partial;
}

function applyEnv(config: BridgeConfig): BridgeConfig {
	return {
		...config,
		enabled: envBool("MEMENTO_PI_ENABLED") ?? config.enabled,
		briefing: envBool("MEMENTO_PI_BRIEFING") ?? config.briefing,
		promptRecall: envBool("MEMENTO_PI_PROMPT_RECALL") ?? config.promptRecall,
		toolContext: envBool("MEMENTO_PI_TOOL_CONTEXT") ?? config.toolContext,
		autoCapture: envBool("MEMENTO_PI_AUTO_CAPTURE") ?? config.autoCapture,
		captureQueue: envBool("MEMENTO_PI_CAPTURE_QUEUE") ?? config.captureQueue,
		processQueue: envBool("MEMENTO_PI_PROCESS_QUEUE") ?? config.processQueue,
		processQueueOnSessionClose: envBool("MEMENTO_PI_PROCESS_QUEUE_ON_SESSION_CLOSE") ?? config.processQueueOnSessionClose,
		processQueueMaxCaptures: envInt("MEMENTO_PI_PROCESS_QUEUE_MAX_CAPTURES") ?? config.processQueueMaxCaptures,
		processQueueModel: process.env.MEMENTO_PI_PROCESS_QUEUE_MODEL ?? config.processQueueModel,
		maxInjectedChars: envInt("MEMENTO_PI_MAX_INJECTED_CHARS") ?? config.maxInjectedChars,
		maxToolContextPerSession: envInt("MEMENTO_PI_MAX_TOOL_CONTEXT_PER_SESSION") ?? config.maxToolContextPerSession,
	};
}

function loadConfig(cwd = process.cwd()): LoadedBridgeConfig {
	let config = { ...defaultConfig };
	const sources = ["defaults"];
	const candidates = [
		join(homedir(), ".config", "memento-vault", "pi-bridge.json"),
		resolve(cwd, ".pi", "settings.json"),
		resolve(cwd, "package.json"),
	];

	for (const path of candidates) {
		try {
			const raw = readJson(path);
			if (!raw) continue;
			const partial = bridgeConfigFrom(raw);
			if (Object.keys(partial).length === 0) continue;
			config = { ...config, ...partial };
			sources.push(path);
		} catch (error) {
			sources.push(`${path}:error:${String(error)}`);
		}
	}

	const envConfig = applyEnv(config);
	if (JSON.stringify(envConfig) !== JSON.stringify(config)) sources.push("environment");
	return { config: envConfig, sources };
}

function capText(text: string, maxChars: number): string {
	if (maxChars <= 0 || text.length <= maxChars) return text;
	return `${text.slice(0, maxChars)}\n[vault] truncated by memento pi bridge cap (${maxChars} chars)`;
}

function summarizeMessages(messages: unknown): string {
	if (!Array.isArray(messages)) return "Pi agent turn ended; message details unavailable.";
	const summary = messages
		.slice(-8)
		.map((message, index) => summarizeRecord(message, `message-${index + 1}`))
		.filter((line) => line.length > 4)
		.join("\n");
	return summary || "Pi agent turn ended; no message summary available.";
}

function summarizeRecord(value: unknown, fallbackRole: string): string {
	const record = value as Record<string, unknown>;
	const nested = record.message as Record<string, unknown> | undefined;
	const role = String(nested?.role ?? record.role ?? record.type ?? fallbackRole);
	const rawContent = nested?.content ?? record.content ?? record.summary ?? record.text ?? "";
	const content = typeof rawContent === "string" ? rawContent : JSON.stringify(rawContent).slice(0, 500);
	return `- ${role}: ${content.replace(/\s+/g, " ").trim().slice(0, 500)}`;
}

function summarizeSessionEntries(entries: unknown, reason: string): string {
	if (!Array.isArray(entries)) return `Pi ${reason}; session entry details unavailable.`;
	const recent = entries.slice(-12).map((entry, index) => summarizeRecord(entry, `entry-${index + 1}`));
	return [`Pi ${reason}.`, "", "Recent session entries:", ...recent].join("\n");
}

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const repoRoot = resolve(__dirname, "..");

async function runJson(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
): Promise<Record<string, unknown>> {
	const result = await pi.exec("python3", ["-m", "memento.pi_bridge", ...args], {
		cwd: repoRoot,
		signal: ctx.signal,
		timeout: 15_000,
	});
	if (result.code !== 0) return { error: "process-failed", code: result.code, stderr: result.stderr };
	try {
		return JSON.parse(result.stdout) as Record<string, unknown>;
	} catch (error) {
		return { error: "invalid-json", stdout: result.stdout, message: String(error) };
	}
}

async function runLifecycle(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
	source: string,
): Promise<LifecycleResult> {
	const result = await pi.exec("python3", ["-m", "memento.pi_bridge", ...args], {
		cwd: repoRoot,
		signal: ctx.signal,
		timeout: 15_000,
	});

	if (result.code !== 0) {
		return {
			should_inject: false,
			content: "",
			source,
			results: [],
			reason: "process-failed",
			metadata: { code: result.code, stderr: result.stderr },
		};
	}

	try {
		return JSON.parse(result.stdout) as LifecycleResult;
	} catch (error) {
		return {
			should_inject: false,
			content: "",
			source,
			results: [],
			reason: "invalid-json",
			metadata: { stdout: result.stdout, error: String(error) },
		};
	}
}

function textPart(text: string) {
	return { type: "text" as const, text };
}

function processArgsFromParams(params: Record<string, unknown>): string[] {
	const args: string[] = [];
	for (const [key, flag] of [["id", "--id"], ["project", "--project"], ["branch", "--branch"], ["session", "--session"]] as const) {
		const value = params[key];
		if (typeof value === "string" && value.trim()) args.push(flag, value.trim());
	}
	if (typeof params.limit === "number" && Number.isFinite(params.limit) && params.limit > 0) args.push("--limit", String(Math.floor(params.limit)));
	args.push(params.newest ? "--newest" : "--oldest");
	return args;
}

function parseProcessCommandArgs(raw: string): string[] {
	const trimmed = raw.trim();
	if (!trimmed) return [];
	return trimmed.split(/\s+/g);
}

async function currentProjectSlug(pi: ExtensionAPI, ctx: ExtensionContext): Promise<string> {
	const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
	return String(payload.project_slug ?? "unknown");
}

async function currentBranch(pi: ExtensionAPI, ctx: ExtensionContext): Promise<string> {
	const result = await pi.exec("git", ["branch", "--show-current"], { cwd: ctx.cwd, signal: ctx.signal, timeout: 2_000 });
	return result.stdout.trim();
}

async function runProcessWorker(pi: ExtensionAPI, ctx: ExtensionContext, args: string[], config: BridgeConfig): Promise<Record<string, unknown>> {
	const worker = join(__dirname, "memento-process-worker.mjs");
	const workerArgs = config.processQueueModel ? ["--processor-model", config.processQueueModel, ...args] : args;
	const result = await pi.exec("node", [worker, ...workerArgs], { cwd: repoRoot, signal: ctx.signal, timeout: 60 * 60 * 1000 });
	if (result.code !== 0) return { error: "worker-failed", code: result.code, stderr: result.stderr, stdout: result.stdout };
	try {
		return JSON.parse(result.stdout) as Record<string, unknown>;
	} catch (error) {
		return { error: "worker-invalid-json", stdout: result.stdout, stderr: result.stderr, message: String(error) };
	}
}

export default function mementoExtension(pi: ExtensionAPI) {
	let loadedConfig = loadConfig();
	let config = loadedConfig.config;
	let briefingInjected = false;
	let toolContextCount = 0;
	let lastLifecycleReason = "startup";
	let lifecycleCaptureQueued = false;

	async function queueLifecycleCapture(ctx: ExtensionContext, title: string, body: string, reason: string, sourceEvent: string) {
		if (!config.enabled || !config.autoCapture || !config.captureQueue) return undefined;
		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
		const payload = await runJson(pi, ctx, [
			"capture",
			"--title",
			title,
			"--body",
			body,
			"--cwd",
			ctx.cwd,
			"--session-id",
			sessionFile,
			"--queue",
			"--reason",
			reason,
			"--source-event",
			sourceEvent,
		]);
		lifecycleCaptureQueued = !payload.error;
		lastLifecycleReason = payload.error ? `queue-error:${String(payload.error)}` : `${sourceEvent}-capture-queued`;
		return payload;
	}

	pi.on("session_start", async (_event, ctx) => {
		loadedConfig = loadConfig(ctx.cwd);
		config = loadedConfig.config;
		briefingInjected = false;
		toolContextCount = 0;
		lifecycleCaptureQueued = false;
		lastLifecycleReason = config.enabled ? "ready" : "disabled";
		ctx.ui.setStatus("memento", config.enabled ? "memento ready" : "memento disabled");
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!config.enabled) return;
		const messages: Array<{ customType: string; content: string; display: boolean }> = [];
		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";

		if (config.briefing && !briefingInjected) {
			briefingInjected = true;
			const briefing = await runLifecycle(
				pi,
				ctx,
				["briefing", "--cwd", ctx.cwd, "--session-id", sessionFile],
				"briefing",
			);
			lastLifecycleReason = briefing.reason ?? (briefing.should_inject ? "briefing-inject" : "briefing-skip");
			if (briefing.should_inject && briefing.content) {
				messages.push({ customType: "memento-briefing", content: capText(briefing.content, config.maxInjectedChars), display: true });
			}
		}

		if (config.promptRecall) {
			const recall = await runLifecycle(
				pi,
				ctx,
				["recall", "--prompt", event.prompt, "--cwd", ctx.cwd, "--session-id", sessionFile],
				"recall",
			);
			lastLifecycleReason = recall.reason ?? (recall.should_inject ? "recall-inject" : "recall-skip");
			if (recall.should_inject && recall.content) {
				messages.push({ customType: "memento-recall", content: capText(recall.content, config.maxInjectedChars), display: true });
			}
		}

		if (messages.length === 0) return;
		return {
			message: {
				customType: "memento-lifecycle",
				content: messages.map((message) => message.content).join("\n\n"),
				display: true,
			},
		};
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!config.enabled || !config.toolContext) return;
		if (toolContextCount >= config.maxToolContextPerSession) {
			lastLifecycleReason = "tool-context-cap-reached";
			return;
		}
		if (event.toolName !== "read" || event.isError) return;
		const input = event.input as { path?: string; file_path?: string } | undefined;
		const filePath = input?.path ?? input?.file_path ?? "";
		if (!filePath) return;

		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
		const toolContext = await runLifecycle(
			pi,
			ctx,
			["tool-context", "--tool-name", "read", "--file-path", filePath, "--cwd", ctx.cwd, "--session-id", sessionFile],
			"tool-context",
		);
		lastLifecycleReason = toolContext.reason ?? (toolContext.should_inject ? "tool-context-inject" : "tool-context-skip");
		if (!toolContext.should_inject || !toolContext.content) return;
		toolContextCount += 1;

		return {
			content: [...event.content, textPart(`\n\n${capText(toolContext.content, config.maxInjectedChars)}`)],
		};
	});

	pi.on("agent_end", async (event, ctx) => {
		const body = summarizeMessages((event as { messages?: unknown }).messages);
		await queueLifecycleCapture(ctx, "Pi session candidate capture", body, "agent_end", "agent_end");
	});

	pi.on("session_before_compact", async (_event, ctx) => {
		const body = summarizeSessionEntries(ctx.sessionManager.getEntries(), "is about to compact the current session");
		await queueLifecycleCapture(ctx, "Pi pre-compaction candidate capture", body, "session_before_compact", "session_before_compact");
	});

	pi.on("session_compact", async (event, ctx) => {
		const body = `Pi compacted the current session.\n\nEvent details:\n${JSON.stringify(event, null, 2).slice(0, 2000)}`;
		await queueLifecycleCapture(ctx, "Pi compaction candidate capture", body, "session_compact", "session_compact");
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (!lifecycleCaptureQueued) {
			const reason = String((event as { reason?: unknown }).reason ?? "shutdown");
			const body = summarizeSessionEntries(ctx.sessionManager.getEntries(), `session is shutting down (${reason})`);
			await queueLifecycleCapture(ctx, "Pi shutdown candidate capture", body, `session_shutdown:${reason}`, "session_shutdown");
		}
		ctx.ui.setStatus("memento", "memento stopped");
	});

	pi.registerTool({
		name: "memento_status",
		label: "Memento Status",
		description: "Show memento vault and lifecycle bridge health/config status. Use for operational checks and setup debugging, not for prior decisions, project history, or note content; use memento_search and memento_get for recall.",
		parameters: Type.Object({}),
		async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
			const details = { ...payload, piBridge: { config, configSources: loadedConfig.sources, toolContextCount, lifecycleCaptureQueued, lastLifecycleReason } };
			return { content: [textPart(JSON.stringify(details, null, 2))], details };
		},
	});

	pi.registerTool({
		name: "memento_search",
		label: "Memento Search",
		description: "Search memento vault notes before answering questions about past decisions, prior fixes, project history, session context, recurring patterns, or exact identifiers. Use memento_get after search when you need full content for a returned path; do not use search to read a known note path.",
		parameters: Type.Object({
			query: Type.String({ description: "Natural-language question or exact identifier to search for" }),
			limit: Type.Optional(Type.Number({ description: "Maximum results, default 5" })),
			concrete: Type.Optional(Type.Union([
				Type.Literal("auto"),
				Type.Literal("true"),
				Type.Literal("false"),
			], { description: "Literal search mode: auto, true, or false. Keep auto for identifier-like queries such as file names, function names, config keys, or error strings." })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, [
				"search",
				"--query",
				params.query,
				"--limit",
				String(params.limit ?? 5),
				"--cwd",
				ctx.cwd,
				"--concrete",
				params.concrete ?? "auto",
			]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_get",
		label: "Memento Get",
		description: "Read the full content of a specific memento note by path or note name. Use after memento_search when a result path needs full content, or directly when the user already supplied an exact note path/name. Do not use for topical discovery; search first.",
		parameters: Type.Object({
			path: Type.String({ description: "Exact note path or note name, for example notes/my-note.md or my-note" }),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, ["get", "--path", params.path]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_capture",
		label: "Memento Capture",
		description: "Manually capture durable knowledge from the current pi session. Use only when the user explicitly asks to remember, save, capture, or record memory. This is separate from interactive /memento skill workflows and from automatic lifecycle capture.",
		parameters: Type.Object({
			title: Type.String({ description: "Short note title for the durable memory" }),
			body: Type.String({ description: "Durable decision, discovery, fix, or reusable pattern to capture" }),
			queue: Type.Optional(Type.Boolean({ description: "Queue for review instead of writing a note immediately" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
			const args = [
				"capture",
				"--title",
				params.title,
				"--body",
				params.body,
				"--cwd",
				ctx.cwd,
				"--session-id",
				sessionFile,
			];
			if (params.queue) args.push("--queue", "--reason", "manual", "--source-event", "tool");
			const payload = await runJson(pi, ctx, args);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_queue",
		label: "Memento Capture Queue",
		description: "List queued pi capture candidates from lifecycle or manual capture review. Use for capture workflow review, not for searching prior decisions or project history.",
		parameters: Type.Object({
			limit: Type.Optional(Type.Number({ description: "Maximum queued captures to list, default 20" })),
			includeBody: Type.Optional(Type.Boolean({ description: "Include queued capture bodies" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const args = ["queue", "list", "--limit", String(params.limit ?? 20)];
			if (params.includeBody) args.push("--include-body");
			const payload = await runJson(pi, ctx, args);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_process",
		label: "Memento Process Queue",
		description: "Process selected queued pi captures into curated Memento notes. Requires explicit selection; use dryRun to preview.",
		parameters: Type.Object({
			id: Type.Optional(Type.String({ description: "Capture id to process" })),
			project: Type.Optional(Type.String({ description: "Project slug to process" })),
			branch: Type.Optional(Type.String({ description: "Branch to process" })),
			session: Type.Optional(Type.String({ description: "Session id/path to process" })),
			limit: Type.Optional(Type.Number({ description: "Maximum captures to select" })),
			newest: Type.Optional(Type.Boolean({ description: "Select newest first instead of oldest first" })),
			dryRun: Type.Optional(Type.Boolean({ description: "Preview selected session groups without processing" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			if (!config.processQueue) {
				const payload = { error: "memento queue processing is disabled", reason: "process_queue_disabled" };
				return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
			}
			const hasSelection = Boolean(params.id || params.project || params.branch || params.session || params.limit);
			if (!hasSelection) {
				const payload = { error: "memento_process requires explicit selection", guidance: "Pass id, project, branch, session, limit, or dryRun with filters. Use /memento-process interactively." };
				return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
			}
			const cliArgs = processArgsFromParams(params as Record<string, unknown>);
			if (params.dryRun) {
				const payload = await runJson(pi, ctx, ["queue", "process-start", ...cliArgs, "--dry-run"]);
				return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
			}
			const payload = await runProcessWorker(pi, ctx, cliArgs, config);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerCommand("memento-status", {
		description: "Show memento pi bridge status",
		handler: async (_args, ctx) => {
			const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
			const details = { ...payload, piBridge: { config, configSources: loadedConfig.sources, toolContextCount, lifecycleCaptureQueued, lastLifecycleReason } };
			if (payload.error) {
				ctx.ui.notify(`memento bridge failed: ${String(payload.error)}`, "error");
			} else {
				ctx.ui.notify("memento bridge reachable", "success");
				ctx.ui.setWidget("memento", JSON.stringify(details, null, 2).split("\n"));
			}
		},
	});

	pi.registerCommand("memento-queue", {
		description: "Show queued memento pi capture candidates",
		handler: async (args, ctx) => {
			const includeBody = args.includes("--include-body");
			const payload = await runJson(pi, ctx, ["queue", "list", "--limit", "20", ...(includeBody ? ["--include-body"] : [])]);
			ctx.ui.setWidget("memento-queue", JSON.stringify(payload, null, 2).split("\n"));
		},
	});

	pi.registerCommand("memento-process", {
		description: "Process queued memento pi captures into curated notes",
		handler: async (args, ctx) => {
			if (!config.processQueue) {
				ctx.ui.notify("memento queue processing is disabled", "error");
				return;
			}
			let cliArgs = parseProcessCommandArgs(args);
			if (cliArgs.length === 0) {
				const preview = await runJson(pi, ctx, ["queue", "process-start", "--project", await currentProjectSlug(pi, ctx), "--limit", "5", "--dry-run"]);
				const choice = ctx.hasUI ? await ctx.ui.select("Process queued Memento captures", [
					"Current project, oldest 5",
					"Current branch, oldest 5",
					"Oldest 5 overall",
					"Dry run current project",
					"Cancel",
				]) : "Cancel";
				if (!choice || choice === "Cancel") return;
				if (choice === "Current project, oldest 5") cliArgs = ["--project", await currentProjectSlug(pi, ctx), "--limit", "5"];
				else if (choice === "Current branch, oldest 5") cliArgs = ["--project", await currentProjectSlug(pi, ctx), "--branch", await currentBranch(pi, ctx), "--limit", "5"];
				else if (choice === "Oldest 5 overall") cliArgs = ["--limit", "5"];
				else if (choice === "Dry run current project") cliArgs = ["--project", await currentProjectSlug(pi, ctx), "--limit", "5", "--dry-run"];
				ctx.ui.setWidget("memento-process-preview", JSON.stringify(preview, null, 2).split("\n"));
			}
			if (cliArgs.includes("--dry-run")) {
				const payload = await runJson(pi, ctx, ["queue", "process-start", ...cliArgs]);
				ctx.ui.setWidget("memento-process", JSON.stringify(payload, null, 2).split("\n"));
				return;
			}
			ctx.ui.notify("memento processing started", "info");
			const payload = await runProcessWorker(pi, ctx, cliArgs, config);
			ctx.ui.notify(payload.error ? `memento processing failed: ${String(payload.error)}` : "memento processing finished", payload.error ? "error" : "success");
			ctx.ui.setWidget("memento-process", JSON.stringify(payload, null, 2).split("\n"));
		},
	});
}
