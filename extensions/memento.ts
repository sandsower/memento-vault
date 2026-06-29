import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Text } from "@mariozechner/pi-tui";
import { Type } from "typebox";
import {
	formatProcessLines,
	formatQueueLines,
	formatStatusLines,
	reduceMementoPanelState,
	renderMementoPanelLines,
	renderMementoStatusText,
	type MementoPanelState,
} from "./memento-ui.js";
import { addSessionPointerDigest, sanitizeEventDetails, summarizeMessages, summarizeSessionEntries } from "./transcript-sanitizer.js";

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

const DEFAULT_PROCESS_QUEUE_MODEL = "claude-sonnet-4-20250514";

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
	processQueueModel: DEFAULT_PROCESS_QUEUE_MODEL,
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
		else if (key === "id" && Array.isArray(value)) {
			for (const id of value) if (typeof id === "string" && id.trim()) args.push(flag, id.trim());
		}
	}
	if (typeof params.limit === "number" && Number.isFinite(params.limit) && params.limit > 0) args.push("--limit", String(Math.floor(params.limit)));
	args.push(params.newest ? "--newest" : "--oldest");
	return args;
}

function withProcessLimit(args: string[], maxCaptures: number): string[] {
	const capped = Math.max(1, Math.floor(maxCaptures));
	const next = [...args];
	const index = next.indexOf("--limit");
	if (index >= 0) {
		const requested = Number.parseInt(next[index + 1] ?? "", 10);
		next[index + 1] = String(Number.isFinite(requested) && requested > 0 ? Math.min(requested, capped) : capped);
	} else {
		next.push("--limit", String(capped));
	}
	return next;
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

function processArgsFromCaptureIds(ids: string[], maxCaptures: number): string[] {
	return withProcessLimit(ids.flatMap((id) => ["--id", id]), maxCaptures);
}

function captureIdsFromQueue(queue?: Record<string, unknown>): string[] {
	const captures = Array.isArray(queue?.captures) ? queue.captures as Record<string, unknown>[] : [];
	return captures.map((capture) => String(capture.id ?? "")).filter(Boolean);
}

function defaultSelectedCaptureIds(queue: Record<string, unknown> | undefined, projectSlug: string, maxCaptures: number): string[] {
	const captures = Array.isArray(queue?.captures) ? queue.captures as Record<string, unknown>[] : [];
	const selected: string[] = [];
	for (const capture of captures) {
		const metadata = capture.metadata && typeof capture.metadata === "object" ? capture.metadata as Record<string, unknown> : {};
		if (projectSlug !== "unknown" && metadata.project !== projectSlug) continue;
		const id = String(capture.id ?? "");
		if (id) selected.push(id);
		if (selected.length >= Math.max(1, Math.floor(maxCaptures))) break;
	}
	return selected;
}

function countGroups(payload?: Record<string, unknown>): number {
	return Array.isArray(payload?.groups) ? payload.groups.length : 1;
}

function processingMessage(payload?: Record<string, unknown>): string {
	if (payload?.error) return "Processing failed.";
	const status = String(payload?.status ?? "");
	if (status === "failed") return "Processing failed.";
	if (status === "interrupted") return "Processing interrupted.";
	return "Processing finished.";
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
	let footerDetailsPinned = false;
	let latestStatus: Record<string, unknown> | undefined;
	let latestQueue: Record<string, unknown> | undefined;
	let latestProcess: Record<string, unknown> | undefined;

	async function queueLifecycleCapture(ctx: ExtensionContext, title: string, body: string, reason: string, sourceEvent: string) {
		if (!config.enabled || !config.autoCapture || !config.captureQueue) return undefined;
		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
		const queuedBody = addSessionPointerDigest(body, sessionFile);
		const payload = await runJson(pi, ctx, [
			"capture",
			"--title",
			title,
			"--body",
			queuedBody,
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
		lifecycleCaptureQueued = lifecycleCaptureQueued || Boolean(payload.queued);
		lastLifecycleReason = payload.error
			? `queue-error:${String(payload.error)}`
			: payload.skipped
				? `${sourceEvent}-capture-skipped:${String(payload.reason ?? "unspecified")}`
				: `${sourceEvent}-capture-queued`;
		await refreshAmbientWidget(ctx);
		return payload;
	}

	function statusDetails(payload: Record<string, unknown>) {
		return { ...payload, piBridge: { config, configSources: loadedConfig.sources, toolContextCount, lifecycleCaptureQueued, lastLifecycleReason } };
	}

	async function loadStatus(ctx: ExtensionContext) {
		const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
		latestStatus = statusDetails(payload);
		return latestStatus;
	}

	async function loadQueue(ctx: ExtensionContext, includeBody = false, limit = 20) {
		latestQueue = await runJson(pi, ctx, ["queue", "list", "--limit", String(limit), ...(includeBody ? ["--include-body"] : [])]);
		return latestQueue;
	}

	async function loadProcessStatus(ctx: ExtensionContext) {
		latestProcess = await runJson(pi, ctx, ["queue", "process-status"]);
		return latestProcess;
	}

	async function refreshAmbientWidget(ctx: ExtensionContext) {
		if (!ctx.hasUI) return;
		ctx.ui.setWidget("memento", undefined);
		ctx.ui.setStatus("memento", renderMementoStatusText(latestStatus, latestQueue, { pinned: footerDetailsPinned, process: latestProcess }));
	}

	function invokeMementoSkill(ctx: ExtensionContext) {
		const prompt = [
			"/skill:memento",
			"",
			"Capture durable knowledge from the current pi session. Search first to avoid duplicates. Use memento_capture for durable decisions, discoveries, fixes, or reusable patterns. Do not process queued captures in this flow.",
		].join("\n");
		const options = ctx.isIdle() ? undefined : { deliverAs: "followUp" as const };
		pi.sendUserMessage(prompt, options);
	}

	pi.on("session_start", async (_event, ctx) => {
		loadedConfig = loadConfig(ctx.cwd);
		config = loadedConfig.config;
		briefingInjected = false;
		toolContextCount = 0;
		lifecycleCaptureQueued = false;
		lastLifecycleReason = config.enabled ? "ready" : "disabled";
		ctx.ui.setStatus("memento", config.enabled ? "🧠 …" : "🧠 off");
		if (ctx.hasUI) ctx.ui.setWidget("memento", undefined);
		if (config.enabled) {
			latestStatus = await loadStatus(ctx);
			latestQueue = await loadQueue(ctx, false, 5);
			latestProcess = await loadProcessStatus(ctx);
			await refreshAmbientWidget(ctx);
		}
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
		const body = `Pi compacted the current session.\n\nEvent details:\n${sanitizeEventDetails(event, 2000)}`;
		await queueLifecycleCapture(ctx, "Pi compaction candidate capture", body, "session_compact", "session_compact");
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (!lifecycleCaptureQueued) {
			const reason = String((event as { reason?: unknown }).reason ?? "shutdown");
			const body = summarizeSessionEntries(ctx.sessionManager.getEntries(), `session is shutting down (${reason})`);
			await queueLifecycleCapture(ctx, "Pi shutdown candidate capture", body, `session_shutdown:${reason}`, "session_shutdown");
		}
		ctx.ui.setStatus("memento", "🧠 stopped");
		if (ctx.hasUI) ctx.ui.setWidget("memento", undefined);
	});

	pi.registerTool({
		name: "memento_status",
		label: "Memento Status",
		description: "Show memento vault and lifecycle bridge health/config status. Use for operational checks and setup debugging, not for prior decisions, project history, or note content; use memento_search and memento_get for recall.",
		parameters: Type.Object({}),
		async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
			const details = await loadStatus(ctx);
			await refreshAmbientWidget(ctx);
			return { content: [textPart(formatStatusLines(details).join("\n"))], details };
		},
		renderResult(result, { expanded }, theme) {
			const details = result.details as Record<string, unknown> | undefined;
			return new Text(formatStatusLines(details, { includeDetails: expanded }).join("\n"), 0, 0);
		},
	});

	pi.registerTool({
		name: "memento_session_context",
		label: "Memento Session Context",
		description: "Build a one-call budgeted memento session context packet with briefing, prompt recall, vault health, queue status, and expandable note paths. Host-adapter primitive for startup/context injection; use memento_search and memento_get for explicit user recall questions.",
		parameters: Type.Object({
			prompt: Type.Optional(Type.String({ description: "Current user prompt for optional recall context" })),
			session_id: Type.Optional(Type.String({ description: "Session identifier, defaults to the current pi session file" })),
			token_budget: Type.Optional(Type.Integer({ description: "Approximate token budget for returned content, default 2000" })),
			include_status: Type.Optional(Type.Boolean({ description: "Include vault health and pi capture queue status, default true" })),
			include_recent: Type.Optional(Type.Boolean({ description: "Include project briefing/recent context, default true" })),
			include_recall: Type.Optional(Type.Boolean({ description: "Include prompt recall when prompt is supplied, default true" })),
			include_tool_context_preview: Type.Optional(Type.Boolean({ description: "Include tool-context preview metadata, default false" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionFile = params.session_id ?? ctx.sessionManager.getSessionFile() ?? "unknown";
			const args = [
				"session-context",
				"--cwd",
				ctx.cwd,
				"--prompt",
				params.prompt ?? "",
				"--session-id",
				sessionFile,
				"--token-budget",
				String(params.token_budget ?? 2000),
			];
			if (params.include_status === false) args.push("--no-include-status");
			if (params.include_recent === false) args.push("--no-include-recent");
			if (params.include_recall === false) args.push("--no-include-recall");
			if (params.include_tool_context_preview) args.push("--include-tool-context-preview");
			const payload = await runJson(pi, ctx, args);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
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
			note_type: Type.Optional(Type.String({ description: "Frontmatter type such as decision, discovery, pattern, bugfix, or tool" })),
			tags: Type.Optional(Type.Array(Type.String(), { description: "Frontmatter tags for this note" })),
			certainty: Type.Optional(Type.Number({ description: "Frontmatter certainty from 1 to 5" })),
			cwd: Type.Optional(Type.String({ description: "Original project cwd for frontmatter/project detection; defaults to current cwd" })),
			branch: Type.Optional(Type.String({ description: "Original git branch for frontmatter; defaults to the branch detected from cwd" })),
			session_id: Type.Optional(Type.String({ description: "Original session identifier for frontmatter; defaults to current pi session" })),
			queue: Type.Optional(Type.Boolean({ description: "Queue for review instead of writing a note immediately" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionFile = params.session_id ?? ctx.sessionManager.getSessionFile() ?? "unknown";
			const args = [
				"capture",
				"--title",
				params.title,
				"--body",
				params.body,
				"--cwd",
				params.cwd ?? ctx.cwd,
				"--session-id",
				sessionFile,
			];
			if (params.note_type) args.push("--note-type", params.note_type);
			if (params.branch) args.push("--branch", params.branch);
			if (typeof params.certainty === "number" && Number.isFinite(params.certainty)) {
				const certainty = Math.floor(params.certainty);
				if (certainty < 1 || certainty > 5) {
					const payload = { error: "certainty must be an integer from 1 to 5" };
					return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload, isError: true };
				}
				args.push("--certainty", String(certainty));
			}
			for (const tag of params.tags ?? []) if (tag.trim()) args.push("--tag", tag.trim());
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
			const payload = await loadQueue(ctx, Boolean(params.includeBody), params.limit ?? 20);
			await refreshAmbientWidget(ctx);
			return { content: [textPart(formatQueueLines(payload).join("\n"))], details: payload };
		},
		renderResult(result, { expanded }, theme) {
			const details = result.details as Record<string, unknown> | undefined;
			return new Text(formatQueueLines(details, { limit: expanded ? 20 : 8 }).join("\n"), 0, 0);
		},
	});

	pi.registerTool({
		name: "memento_process",
		label: "Memento Process Queue",
		description: "Process selected queued pi captures into curated Memento notes. Requires explicit selection; use dryRun to preview.",
		parameters: Type.Object({
			id: Type.Optional(Type.Union([
				Type.String({ minLength: 1, description: "Capture id to process" }),
				Type.Array(Type.String({ minLength: 1 }), { minItems: 1, description: "Capture ids to process" }),
			])),
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
				return { content: [textPart(formatProcessLines(payload).join("\n"))], details: payload, isError: true };
			}
			const hasSelection = Boolean(params.id || params.project || params.branch || params.session || params.limit);
			if (!hasSelection) {
				const payload = { error: "memento_process requires explicit selection", guidance: "Pass id, project, branch, session, limit, or dryRun with filters. Use /memento-process interactively." };
				return { content: [textPart(formatProcessLines(payload).join("\n"))], details: payload, isError: true };
			}
			const cliArgs = withProcessLimit(processArgsFromParams(params as Record<string, unknown>), config.processQueueMaxCaptures);
			if (params.dryRun) {
				const payload = await runJson(pi, ctx, ["queue", "process-start", ...cliArgs, "--dry-run"]);
				return { content: [textPart(formatProcessLines(payload).join("\n"))], details: payload };
			}
			const payload = await runProcessWorker(pi, ctx, cliArgs, config);
			latestQueue = await loadQueue(ctx, false, 5);
			latestProcess = await loadProcessStatus(ctx);
			await refreshAmbientWidget(ctx);
			return { content: [textPart(formatProcessLines(latestProcess?.status === "idle" ? payload : latestProcess).join("\n"))], details: latestProcess?.status === "idle" ? payload : latestProcess };
		},
		renderResult(result, _options, theme) {
			const details = result.details as Record<string, unknown> | undefined;
			return new Text(formatProcessLines(details).join("\n"), 0, 0);
		},
	});

	pi.registerCommand("memento-capture", {
		description: "Invoke the Memento skill on the current pi session",
		handler: async (_args, ctx) => {
			invokeMementoSkill(ctx);
			ctx.ui.notify("memento capture queued via /skill:memento", "info");
		},
	});

	pi.registerCommand("memento", {
		description: "Open the Memento Vault dashboard",
		handler: async (_args, ctx) => {
			if (!ctx.hasUI || typeof ctx.ui.custom !== "function") {
				invokeMementoSkill(ctx);
				return;
			}
			latestStatus = await loadStatus(ctx);
			latestQueue = await loadQueue(ctx, false, 10);
			latestProcess = await loadProcessStatus(ctx);
			let processPreview: Record<string, unknown> | undefined = latestProcess?.status === "idle" ? undefined : latestProcess;
			let state: MementoPanelState = {
				view: "actions",
				selectedIndex: 0,
				queueItemCount: captureIdsFromQueue(latestQueue).length,
				processItemCount: countGroups(processPreview),
				selectedCaptureIds: defaultSelectedCaptureIds(latestQueue, String(latestStatus?.project_slug ?? "unknown"), config.processQueueMaxCaptures),
			};
			let requestRender = () => {};
			let pollTimer: ReturnType<typeof setInterval> | undefined;
			const syncCounts = () => {
				state = { ...state, queueItemCount: captureIdsFromQueue(latestQueue).length, processItemCount: countGroups(processPreview) };
			};
			const refresh = async () => {
				latestStatus = await loadStatus(ctx);
				latestQueue = await loadQueue(ctx, false, 10);
				latestProcess = await loadProcessStatus(ctx);
				if (latestProcess?.status !== "idle") processPreview = latestProcess;
				syncCounts();
				await refreshAmbientWidget(ctx);
				requestRender();
			};
			const selectedProcessArgs = () => processArgsFromCaptureIds(state.selectedCaptureIds ?? [], config.processQueueMaxCaptures);
			const startPolling = () => {
				if (pollTimer) clearInterval(pollTimer);
				pollTimer = setInterval(() => {
					void loadProcessStatus(ctx).then(async (payload) => {
						processPreview = payload;
						syncCounts();
						await refreshAmbientWidget(ctx);
						requestRender();
						if (payload.status !== "running" && pollTimer) {
							clearInterval(pollTimer);
							pollTimer = undefined;
						}
					});
				}, 1500);
			};
			await ctx.ui.custom((tui, _theme, _keybindings, done) => {
				requestRender = () => tui.requestRender();
				return {
					render(width: number) {
						return renderMementoPanelLines(state, { status: latestStatus, queue: latestQueue, process: processPreview, widgetEnabled: footerDetailsPinned }, width);
					},
					handleInput(data: string) {
						const next = reduceMementoPanelState(state, data);
						state = next.state;
						if (next.action?.type === "close") {
							if (pollTimer) clearInterval(pollTimer);
							done();
							return;
						}
						if (next.action?.type === "capture-current") {
							invokeMementoSkill(ctx);
							state = { ...state, message: "Current-session capture sent to /skill:memento." };
						}
						if (next.action?.type === "toggle-widget") {
							footerDetailsPinned = !footerDetailsPinned;
							void refreshAmbientWidget(ctx);
							state = { ...state, message: `Footer status ${footerDetailsPinned ? "detailed" : "compact"}.` };
						}
						if (next.action?.type === "refresh" || next.action?.type === "show") void refresh();
						if (next.action?.type === "toggle-capture") {
							const ids = captureIdsFromQueue(latestQueue);
							const id = ids[state.selectedIndex];
							if (id) {
								const selected = new Set(state.selectedCaptureIds ?? []);
								if (selected.has(id)) selected.delete(id);
								else if (selected.size >= Math.max(1, Math.floor(config.processQueueMaxCaptures))) {
									state = { ...state, message: `Selection capped at ${config.processQueueMaxCaptures} capture(s).` };
									requestRender();
									return;
								} else selected.add(id);
								state = { ...state, selectedCaptureIds: [...selected], message: `${selected.size} capture(s) selected.` };
							}
						}
						if (next.action?.type === "inspect-group") {
							state = { ...state, message: "Showing artifact paths for selected group." };
						}
						if (next.action?.type === "dry-run") {
							if ((state.selectedCaptureIds ?? []).length === 0) {
								state = { ...state, view: "queue", message: "Select at least one queued capture first." };
							} else {
								state = { ...state, view: "process", message: "Loading process preview…" };
								void (async () => {
									processPreview = await runJson(pi, ctx, ["queue", "process-start", ...selectedProcessArgs(), "--dry-run"]);
									syncCounts();
									state = { ...state, view: "process", message: "Process preview loaded." };
									requestRender();
								})();
							}
						}
						if (next.action?.type === "process") {
							if (!config.processQueue) {
								state = { ...state, view: "process", message: "Queue processing is disabled." };
							} else if ((state.selectedCaptureIds ?? []).length === 0) {
								state = { ...state, view: "queue", message: "Select at least one queued capture first." };
							} else {
								state = { ...state, view: "process", message: "Processing queued captures…" };
								startPolling();
								void (async () => {
									processPreview = await runProcessWorker(pi, ctx, selectedProcessArgs(), config);
									latestQueue = await loadQueue(ctx, false, 10);
									latestProcess = await loadProcessStatus(ctx);
									processPreview = latestProcess?.status === "idle" ? processPreview : latestProcess;
									syncCounts();
									await refreshAmbientWidget(ctx);
									state = { ...state, selectedCaptureIds: defaultSelectedCaptureIds(latestQueue, String(latestStatus?.project_slug ?? "unknown"), config.processQueueMaxCaptures), message: processingMessage(processPreview) };
									requestRender();
								})();
							}
						}
						requestRender();
					},
					invalidate() {},
				};
			}, { overlay: true });
		},
	});

	pi.registerCommand("memento-status", {
		description: "Show memento pi bridge status",
		handler: async (_args, ctx) => {
			const details = await loadStatus(ctx);
			await refreshAmbientWidget(ctx);
			ctx.ui.notify(formatStatusLines(details).join("\n"), details.error ? "error" : "success");
		},
	});

	pi.registerCommand("memento-queue", {
		description: "Show queued memento pi capture candidates",
		handler: async (args, ctx) => {
			const includeBody = args.includes("--include-body");
			const payload = await loadQueue(ctx, includeBody, 20);
			await refreshAmbientWidget(ctx);
			ctx.ui.setWidget("memento-queue", formatQueueLines(payload, { limit: 10 }), { placement: "aboveEditor" });
		},
	});

	pi.registerCommand("memento-process", {
		description: "Process queued memento pi captures into curated notes",
		handler: async (args, ctx) => {
			if (!config.processQueue) {
				ctx.ui.notify("memento queue processing is disabled", "error");
				return;
			}
			let cliArgs = withProcessLimit(parseProcessCommandArgs(args), config.processQueueMaxCaptures);
			if (args.trim().length === 0) {
				const projectSlug = await currentProjectSlug(pi, ctx);
				cliArgs = withProcessLimit(
					[...(projectSlug !== "unknown" ? ["--project", projectSlug] : []), "--oldest", "--dry-run"],
					config.processQueueMaxCaptures,
				);
				ctx.ui.notify("showing memento process preview; use /memento for confirm flow", "info");
			}
			if (cliArgs.includes("--dry-run")) {
				const payload = await runJson(pi, ctx, ["queue", "process-start", ...cliArgs]);
				ctx.ui.setWidget("memento-process", formatProcessLines(payload), { placement: "aboveEditor" });
				return;
			}
			ctx.ui.notify("memento processing started", "info");
			const payload = await runProcessWorker(pi, ctx, cliArgs, config);
			latestQueue = await loadQueue(ctx, false, 5);
			latestProcess = await loadProcessStatus(ctx);
			await refreshAmbientWidget(ctx);
			const displayPayload = latestProcess?.status === "idle" ? payload : latestProcess;
			const message = processingMessage(displayPayload);
			ctx.ui.notify(payload.error ? `memento processing failed: ${String(payload.error)}` : message.toLowerCase(), message === "Processing finished." ? "success" : "error");
			ctx.ui.setWidget("memento-process", formatProcessLines(displayPayload), { placement: "aboveEditor" });
		},
	});
}
