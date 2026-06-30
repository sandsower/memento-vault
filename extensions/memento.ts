import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
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
	queueCaptureSummary,
	reduceMementoPanelState,
	renderMementoPanelLines,
	renderMementoStatusText,
	type MementoPanelState,
} from "./memento-ui.js";
import { defaultConfig as bridgeDefaultConfig, loadConfig } from "./memento-config.js";
import { decorateStatusDetails } from "./memento-status.js";
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

const defaultConfig: BridgeConfig = bridgeDefaultConfig as BridgeConfig;

interface LoadedBridgeConfig {
	config: BridgeConfig;
	sources: string[];
}

function capText(text: string, maxChars: number): string {
	if (maxChars <= 0 || text.length <= maxChars) return text;
	return `${text.slice(0, maxChars)}\n[vault] truncated by memento pi bridge cap (${maxChars} chars)`;
}


const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const repoRoot = resolve(__dirname, "..");
const bridgeHealthLogPath = join(process.env.XDG_CONFIG_HOME ?? join(homedir(), ".config"), "memento-vault", "triage-health.jsonl");

function sanitizeBridgeHealthText(value: unknown): string {
	let text = String(value ?? "");
	for (const [pattern, replacement] of [
		[/sk-[A-Za-z0-9]{20,}/g, "[REDACTED_API_KEY]"],
		[/ghp_[A-Za-z0-9]{36,}/g, "[REDACTED_GITHUB_TOKEN]"],
		[/github_pat_[A-Za-z0-9_]{20,}/g, "[REDACTED_GITHUB_TOKEN]"],
		[/Bearer\s+[A-Za-z0-9_\-.]{20,}/g, "Bearer [REDACTED_TOKEN]"],
	] as const) {
		text = text.replace(pattern, replacement);
	}
	return text.length > 500 ? `${text.slice(0, 500)}...` : text;
}

function appendBridgeHealthRecord(entry: Record<string, unknown>): void {
	try {
		mkdirSync(dirname(bridgeHealthLogPath), { recursive: true });
		appendFileSync(bridgeHealthLogPath, `${JSON.stringify(entry)}\n`);
	} catch {
		// best-effort telemetry only
	}
}

async function runJson(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
	health?: { operation: string; cwd?: string; sessionId?: string; project?: string; config?: BridgeConfig; configSources?: string[] },
): Promise<Record<string, unknown>> {
	try {
		const result = await pi.exec("python3", ["-m", "memento.pi_bridge", ...args], {
			cwd: repoRoot,
			signal: ctx.signal,
			timeout: 15_000,
		});
		if (result.code !== 0) {
			if (health) {
				appendBridgeHealthRecord({
					ts: new Date().toISOString(),
					hook: "pi-bridge",
					action: `${health.operation}_failed`,
					operation: health.operation,
					backend: "python3",
					config: health.config,
					config_sources: health.configSources,
					cwd: health.cwd ?? "",
					project: health.project ?? "unknown",
					session_id: health.sessionId ?? "unknown",
					code: result.code,
					error: sanitizeBridgeHealthText(result.stderr || `exit code ${result.code}`),
				});
			}
			return { error: "process-failed", code: result.code, stderr: result.stderr };
		}
		try {
			return JSON.parse(result.stdout) as Record<string, unknown>;
		} catch (error) {
			if (health) {
				appendBridgeHealthRecord({
					ts: new Date().toISOString(),
					hook: "pi-bridge",
					action: `${health.operation}_failed`,
					operation: health.operation,
					backend: "python3",
					config: health.config,
					config_sources: health.configSources,
					cwd: health.cwd ?? "",
					project: health.project ?? "unknown",
					session_id: health.sessionId ?? "unknown",
					error: sanitizeBridgeHealthText(String(error)),
					stdout: sanitizeBridgeHealthText(result.stdout),
				});
			}
			return { error: "invalid-json", stdout: result.stdout, message: String(error) };
		}
	} catch (error) {
		if (health) {
			appendBridgeHealthRecord({
				ts: new Date().toISOString(),
				hook: "pi-bridge",
				action: `${health.operation}_failed`,
				operation: health.operation,
				backend: "python3",
				config: health.config,
				config_sources: health.configSources,
				cwd: health.cwd ?? "",
				project: health.project ?? "unknown",
				session_id: health.sessionId ?? "unknown",
				error: sanitizeBridgeHealthText(String(error)),
			});
		}
		return { error: "process-failed", message: String(error) };
	}
}

async function runLifecycle(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	args: string[],
	source: string,
	health?: { cwd?: string; sessionId?: string; project?: string; config?: BridgeConfig; configSources?: string[] },
): Promise<LifecycleResult> {
	try {
		const result = await pi.exec("python3", ["-m", "memento.pi_bridge", ...args], {
			cwd: repoRoot,
			signal: ctx.signal,
			timeout: 15_000,
		});

		if (result.code !== 0) {
			if (health) {
				appendBridgeHealthRecord({
					ts: new Date().toISOString(),
					hook: "pi-bridge",
					action: `${source}_failed`,
					operation: source,
					backend: "python3",
					config: health.config,
					config_sources: health.configSources,
					cwd: health.cwd ?? "",
					project: health.project ?? "unknown",
					session_id: health.sessionId ?? "unknown",
					code: result.code,
					error: sanitizeBridgeHealthText(result.stderr || `exit code ${result.code}`),
				});
			}
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
			if (health) {
				appendBridgeHealthRecord({
					ts: new Date().toISOString(),
					hook: "pi-bridge",
					action: `${source}_failed`,
					operation: source,
					backend: "python3",
					config: health.config,
					config_sources: health.configSources,
					cwd: health.cwd ?? "",
					project: health.project ?? "unknown",
					session_id: health.sessionId ?? "unknown",
					error: sanitizeBridgeHealthText(String(error)),
					stdout: sanitizeBridgeHealthText(result.stdout),
				});
			}
			return {
				should_inject: false,
				content: "",
				source,
				results: [],
				reason: "invalid-json",
				metadata: { stdout: result.stdout, error: String(error) },
			};
		}
	} catch (error) {
		if (health) {
			appendBridgeHealthRecord({
				ts: new Date().toISOString(),
				hook: "pi-bridge",
				action: `${source}_failed`,
				operation: source,
				backend: "python3",
				config: health.config,
				config_sources: health.configSources,
				cwd: health.cwd ?? "",
				project: health.project ?? "unknown",
				session_id: health.sessionId ?? "unknown",
				error: sanitizeBridgeHealthText(String(error)),
			});
		}
		return {
			should_inject: false,
			content: "",
			source,
			results: [],
			reason: "process-failed",
			metadata: { error: String(error) },
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

function searchArgsFromParams(params: Record<string, unknown>, cwd: string): string[] {
	const args = ["search", "--query", String(params.query ?? ""), "--cwd", cwd, "--concrete", String(params.concrete ?? "auto")];
	if (typeof params.limit === "number" && Number.isFinite(params.limit) && params.limit > 0) args.push("--limit", String(Math.floor(params.limit)));
	if (typeof params.detail_level === "string" && ["brief", "summary", "full"].includes(params.detail_level)) args.push("--detail-level", params.detail_level);
	if (params.include_content) args.push("--include-content");
	if (typeof params.token_budget === "number" && Number.isFinite(params.token_budget)) args.push("--token-budget", String(Math.floor(params.token_budget)));
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

function isProcessorSession(): boolean {
	return String(process.env.MEMENTO_PI_PROCESSOR ?? "").toLowerCase() === "true";
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

function processGroups(payload?: Record<string, unknown>): Record<string, unknown>[] {
	return Array.isArray(payload?.groups) ? payload.groups as Record<string, unknown>[] : [];
}

function selectedProcessGroup(payload?: Record<string, unknown>, index = 0): Record<string, unknown> | undefined {
	const groups = processGroups(payload);
	if (groups.length === 0) return undefined;
	const safeIndex = Math.min(Math.max(0, Math.floor(index)), groups.length - 1);
	return groups[safeIndex];
}

function countGroups(payload?: Record<string, unknown>): number {
	const groups = processGroups(payload);
	return groups.length > 0 ? groups.length : 1;
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

	function isRecord(value: unknown): value is Record<string, unknown> {
		return Boolean(value) && typeof value === "object" && !Array.isArray(value);
	}

	function firstString(...values: unknown[]): string {
		for (const value of values) {
			if (typeof value === "string" && value.trim()) return value.trim();
		}
		return "";
	}

	function digestText(text: string): string {
		return createHash("sha256").update(text).digest("hex").slice(0, 16);
	}

	function toolPathFromArgs(args: unknown): string {
		if (!isRecord(args)) return "";
		return firstString(args.path, args.file_path, args.filePath, args.file);
	}

	function collectLifecycleMetadata(sourceEvent: string, reason: string, body: string, event: unknown, ctx: ExtensionContext): Record<string, unknown> {
		const sessionEntries = ctx.sessionManager.getEntries();
		const entries = Array.isArray(sessionEntries) ? sessionEntries : [];
		const fileEdits = new Set<string>();
		const fileReads = new Set<string>();
		let userMessageCount = 0;
		let assistantMessageCount = 0;
		let toolCallCount = 0;
		let firstEntryAt = "";
		let lastEntryAt = "";

		const noteTimestamp = (value: unknown) => {
			const stamp = firstString(value);
			if (!stamp) return;
			if (!firstEntryAt) firstEntryAt = stamp;
			lastEntryAt = stamp;
		};

		const scanContent = (content: unknown) => {
			if (typeof content === "string") return;
			if (Array.isArray(content)) {
				for (const part of content) scanContent(part);
				return;
			}
			if (!isRecord(content)) return;
			const type = firstString(content.type).toLowerCase();
			if (type === "toolcall") {
				toolCallCount += 1;
				const name = firstString(content.name, content.tool, content.toolName).toLowerCase();
				const path = toolPathFromArgs(content.arguments ?? content.input ?? content.parameters ?? content.args);
				if (path) {
					if (["edit", "write", "patch", "apply_patch", "multiedit"].includes(name)) fileEdits.add(path);
					if (name === "read") fileReads.add(path);
				}
				return;
			}
			if (type === "toolresult" || type === "tool_result" || type === "function_call_output") {
				return;
			}
			if (content.content !== undefined) scanContent(content.content);
			if (content.message !== undefined) scanContent(content.message);
			if (content.args !== undefined) scanContent(content.args);
			if (content.input !== undefined) scanContent(content.input);
			if (content.result !== undefined) scanContent(content.result);
		};

		const inspectMessage = (message: unknown) => {
			if (!isRecord(message)) return;
			const role = firstString(message.role).toLowerCase();
			if (role === "user") userMessageCount += 1;
			if (role === "assistant") assistantMessageCount += 1;
			scanContent(message.content);
		};

		for (const entry of entries) {
			if (!isRecord(entry)) continue;
			noteTimestamp(entry.timestamp ?? entry.created_at ?? entry.createdAt ?? entry.time);
			const type = firstString(entry.type).toLowerCase();
			if (type === "message" || type === "message_start" || type === "message_end") {
				inspectMessage(entry.message);
				continue;
			}
			if (type === "tool_execution_start") {
				toolCallCount += 1;
				const path = toolPathFromArgs(entry.args ?? entry.input ?? entry.arguments ?? {});
				const name = firstString(entry.toolName, entry.name).toLowerCase();
				if (path) {
					if (["edit", "write", "patch", "apply_patch", "multiedit"].includes(name)) fileEdits.add(path);
					if (name === "read") fileReads.add(path);
				}
				continue;
			}
			if (type === "custom_message") {
				scanContent(entry.content);
				continue;
			}
			inspectMessage(entry.message);
			scanContent(entry.content);
			scanContent(entry.result);
		}

		const summaryDigest = digestText(body);
		return {
			source_event: sourceEvent,
			reason,
			event_timestamp: new Date().toISOString(),
			event_index: entries.length,
			turn_count: Math.min(userMessageCount, assistantMessageCount),
			user_message_count: userMessageCount,
			assistant_message_count: assistantMessageCount,
			tool_call_count: toolCallCount,
			file_edit_count: fileEdits.size,
			file_read_count: fileReads.size,
			file_edits: Array.from(fileEdits).slice(0, 20),
			file_reads: Array.from(fileReads).slice(0, 20),
			session_entry_count: entries.length,
			session_first_entry_at: firstEntryAt,
			session_last_entry_at: lastEntryAt,
			summary: body,
			summary_digest: summaryDigest,
			tool_context_count: toolContextCount,
			last_lifecycle_reason: lastLifecycleReason,
			lifecycle_capture_queued: lifecycleCaptureQueued,
			project_slug: String(latestStatus?.project_slug ?? "unknown"),
			session_file: ctx.sessionManager.getSessionFile() ?? "unknown",
		};
	}

	async function queueLifecycleCapture(ctx: ExtensionContext, title: string, body: string, reason: string, sourceEvent: string, event?: unknown) {
		if (!config.enabled || !config.autoCapture || !config.captureQueue) return undefined;
		if (isProcessorSession()) {
			lastLifecycleReason = `${sourceEvent}-capture-skipped:processor_session`;
			await refreshAmbientWidget(ctx);
			return { skipped: true, reason: "processor_session", source_event: sourceEvent };
		}
		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
		const queuedBody = addSessionPointerDigest(body, sessionFile);
		const lifecycleMetadata = collectLifecycleMetadata(sourceEvent, reason, queuedBody, event, ctx);
		const payload = await runJson(
			pi,
			ctx,
			[
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
				"--lifecycle-metadata",
				JSON.stringify(lifecycleMetadata),
			],
			{ operation: "capture", cwd: ctx.cwd, sessionId: sessionFile, project: String(latestStatus?.project_slug ?? "unknown"), config: config, configSources: loadedConfig.sources },
		);
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
		return decorateStatusDetails(payload, {
			config,
			configSources: loadedConfig.sources,
			toolContextCount,
			lifecycleCaptureQueued,
			lastLifecycleReason,
		});
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
				{ cwd: ctx.cwd, sessionId: sessionFile, project: String(latestStatus?.project_slug ?? "unknown"), config: config, configSources: loadedConfig.sources },
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
				{ cwd: ctx.cwd, sessionId: sessionFile, project: String(latestStatus?.project_slug ?? "unknown"), config: config, configSources: loadedConfig.sources },
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
			{ cwd: ctx.cwd, sessionId: sessionFile, project: String(latestStatus?.project_slug ?? "unknown"), config: config, configSources: loadedConfig.sources },
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
		await queueLifecycleCapture(ctx, "Pi session candidate capture", body, "agent_end", "agent_end", event);
	});

	pi.on("session_before_compact", async (_event, ctx) => {
		const body = summarizeSessionEntries(ctx.sessionManager.getEntries(), "is about to compact the current session");
		await queueLifecycleCapture(ctx, "Pi pre-compaction candidate capture", body, "session_before_compact", "session_before_compact", _event);
	});

	pi.on("session_compact", async (event, ctx) => {
		const body = `Pi compacted the current session.\n\nEvent details:\n${sanitizeEventDetails(event, 2000)}`;
		await queueLifecycleCapture(ctx, "Pi compaction candidate capture", body, "session_compact", "session_compact", event);
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (!lifecycleCaptureQueued) {
			const reason = String((event as { reason?: unknown }).reason ?? "shutdown");
			const body = summarizeSessionEntries(ctx.sessionManager.getEntries(), `session is shutting down (${reason})`);
			await queueLifecycleCapture(ctx, "Pi shutdown candidate capture", body, `session_shutdown:${reason}`, "session_shutdown", event);
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
		description: "Search memento vault notes before answering questions about past decisions, prior fixes, project history, session context, recurring patterns, or exact identifiers. Use memento_get after search when you need full content for a returned path, or request detail_level=full/include_content when you need inline content; do not use search to read a known note path.",
		parameters: Type.Object({
			query: Type.String({ description: "Natural-language question or exact identifier to search for" }),
			limit: Type.Optional(Type.Number({ description: "Maximum results, default 5" })),
			concrete: Type.Optional(Type.Union([
				Type.Literal("auto"),
				Type.Literal("true"),
				Type.Literal("false"),
			], { description: "Literal search mode: auto, true, or false. Keep auto for identifier-like queries such as file names, function names, config keys, or error strings." })),
			detail_level: Type.Optional(Type.Union([
				Type.Literal("brief"),
				Type.Literal("summary"),
				Type.Literal("full"),
			], { description: "Response shape: brief, summary, or full. Summary is the default compact snippet view." })),
			include_content: Type.Optional(Type.Boolean({ description: "Include note content alongside the selected detail level." })),
			token_budget: Type.Optional(Type.Integer({ description: "Approximate token budget for returned content, default 2000" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, searchArgsFromParams(params as Record<string, unknown>, ctx.cwd));
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerTool({
		name: "memento_contradictions",
		label: "Memento Contradictions",
		description: "Inspect a topic for disagreements, stale conclusions, and supersession chains. Use when comparing competing notes about the same topic or when you need explicit superseded notes marked alongside their source paths and certainty/date context.",
		parameters: Type.Object({
			topic: Type.String({ description: "Topic or question to inspect for contradictions" }),
			limit: Type.Optional(Type.Number({ description: "Maximum results, default 20" })),
			min_certainty: Type.Optional(Type.Number({ description: "Minimum certainty to include, default 2" })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, [
				"contradictions",
				"--topic",
				params.topic,
				"--limit",
				String(params.limit ?? 20),
				"--min-certainty",
				String(params.min_certainty ?? 2),
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
			if (params.queue) {
				args.push("--queue", "--reason", "manual", "--source-event", "tool");
				args.push("--lifecycle-metadata", JSON.stringify(collectLifecycleMetadata("tool", "manual", params.body, undefined, ctx)));
			}
			const payload = await runJson(pi, ctx, args, { operation: "capture", cwd: params.cwd ?? ctx.cwd, sessionId: sessionFile, project: String(latestStatus?.project_slug ?? "unknown"), config: config, configSources: loadedConfig.sources });
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
						if (next.action?.type === "refresh" || next.action?.type === "show") {
							state = { ...state, confirmDiscard: false, discardCapture: undefined };
							void refresh();
						}
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
						if (next.action?.type === "request-discard") {
							const capture = queueCaptureSummary(latestQueue, state.selectedIndex);
							if (!capture) {
								state = { ...state, message: "Select a queued capture first." };
							} else {
								state = { ...state, confirmDiscard: true, discardCapture: capture, message: `Discard ${capture.id}? y/N` };
							}
						}
						if (next.action?.type === "inspect-group") {
							state = { ...state, message: "Showing artifact paths for selected group." };
						}
						if (next.action?.type === "retry-failed") {
							const selectedGroup = selectedProcessGroup(latestProcess, state.selectedIndex);
							if (!config.processQueue) {
								state = { ...state, view: "process", message: "Queue processing is disabled." };
							} else if (!selectedGroup || String(selectedGroup.status ?? "") !== "failed") {
								state = { ...state, view: "process", message: "Select a failed group first." };
							} else {
								state = { ...state, view: "process", message: `Retrying ${String(selectedGroup.group_id ?? "failed group")}…` };
								requestRender();
								void (async () => {
									const plan = await runJson(pi, ctx, ["queue", "process-retry", "--run-id", String(latestProcess?.run_id ?? ""), "--group-id", String(selectedGroup.group_id ?? "")]);
									const retryIds = Array.isArray(plan.selected_capture_ids) ? plan.selected_capture_ids.map((captureId) => String(captureId)).filter(Boolean) : [];
									if (plan.error || retryIds.length === 0) {
										state = { ...state, message: plan.error ? `Retry failed: ${String(plan.error)}` : "No captures available to retry." };
										requestRender();
										return;
									}
									startPolling();
									processPreview = await runProcessWorker(pi, ctx, processArgsFromCaptureIds(retryIds, config.processQueueMaxCaptures), config);
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
						if (next.action?.type === "discard") {
							const target = state.discardCapture;
							if (!target?.id) {
								state = { ...state, message: "Select a queued capture first.", confirmDiscard: false, discardCapture: undefined };
							} else {
								const discardId = target.id;
								const selectionSnapshot = [...(state.selectedCaptureIds ?? [])];
								const cursorSnapshot = state.selectedIndex;
								state = { ...state, view: "queue", message: `Discarding ${discardId}…` };
								requestRender();
								void (async () => {
									const payload = await runJson(pi, ctx, ["queue", "discard", "--id", discardId, "--apply"]);
									latestStatus = await loadStatus(ctx);
									latestQueue = await loadQueue(ctx, false, 10);
									latestProcess = await loadProcessStatus(ctx);
									syncCounts();
									const remainingIds = new Set(captureIdsFromQueue(latestQueue));
									const nextSelected = selectionSnapshot.filter((captureId) => remainingIds.has(captureId));
									const nextIndex = Math.min(cursorSnapshot, Math.max(0, captureIdsFromQueue(latestQueue).length - 1));
									const succeeded = !payload.error;
									state = {
										...state,
										selectedCaptureIds: succeeded ? nextSelected : selectionSnapshot,
										selectedIndex: nextIndex,
										confirmDiscard: false,
										discardCapture: undefined,
										message: payload.error ? `Discard failed: ${String(payload.error)}` : `Discarded ${discardId}.`,
									};
									await refreshAmbientWidget(ctx);
									requestRender();
								})();
							}
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
