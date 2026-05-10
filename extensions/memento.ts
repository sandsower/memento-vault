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

	for (const key of ["enabled", "briefing", "promptRecall", "toolContext", "autoCapture", "captureQueue"] as const) {
		if (typeof candidate[key] === "boolean") partial[key] = candidate[key];
	}
	for (const key of ["maxInjectedChars", "maxToolContextPerSession"] as const) {
		if (typeof candidate[key] === "number" && Number.isFinite(candidate[key]) && candidate[key] >= 0) partial[key] = candidate[key];
	}
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
		description: "Show memento vault and lifecycle bridge status.",
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
		description: "Search memento vault notes for prior decisions, discoveries, and session context.",
		parameters: Type.Object({
			query: Type.String({ description: "Search query" }),
			limit: Type.Optional(Type.Number({ description: "Maximum results, default 5" })),
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
			]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_get",
		label: "Memento Get",
		description: "Read a specific memento note by path or note name.",
		parameters: Type.Object({
			path: Type.String({ description: "Note path or note name" }),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, ["get", "--path", params.path]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_capture",
		label: "Memento Capture",
		description: "Manually capture durable knowledge from the current pi session. Use only when the user asks to save memory.",
		parameters: Type.Object({
			title: Type.String({ description: "Short note title" }),
			body: Type.String({ description: "Durable knowledge to capture" }),
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
		description: "List queued pi capture candidates.",
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
		name: "memento_flush_queue",
		label: "Memento Flush Queue",
		description: "Write queued pi capture candidates to durable notes. Use only after user approval.",
		parameters: Type.Object({
			id: Type.Optional(Type.String({ description: "Capture id to flush" })),
			all: Type.Optional(Type.Boolean({ description: "Flush all queued captures" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const args = ["queue", "flush"];
			if (params.all) args.push("--all");
			else if (params.id) args.push("--id", params.id);
			const payload = await runJson(pi, ctx, args);
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

	pi.registerCommand("memento-flush-queue", {
		description: "Flush queued memento pi captures. Pass an id, or --all.",
		handler: async (args, ctx) => {
			const trimmed = args.trim();
			const cliArgs = ["queue", "flush"];
			if (trimmed === "--all") cliArgs.push("--all");
			else if (trimmed) cliArgs.push("--id", trimmed);
			else {
				ctx.ui.notify("Pass a queued capture id or --all", "error");
				return;
			}
			const payload = await runJson(pi, ctx, cliArgs);
			ctx.ui.notify(`memento queue flushed ${String(payload.flushed ?? 0)} capture(s)`, payload.error ? "error" : "success");
			ctx.ui.setWidget("memento-queue", JSON.stringify(payload, null, 2).split("\n"));
		},
	});
}
