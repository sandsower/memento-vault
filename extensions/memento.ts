import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
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
	let briefingInjected = false;

	pi.on("session_start", async (_event, ctx) => {
		briefingInjected = false;
		ctx.ui.setStatus("memento", "memento ready");
	});

	pi.on("before_agent_start", async (event, ctx) => {
		const messages: Array<{ customType: string; content: string; display: boolean }> = [];
		const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";

		if (!briefingInjected) {
			briefingInjected = true;
			const briefing = await runLifecycle(
				pi,
				ctx,
				["briefing", "--cwd", ctx.cwd, "--session-id", sessionFile],
				"briefing",
			);
			if (briefing.should_inject && briefing.content) {
				messages.push({ customType: "memento-briefing", content: briefing.content, display: true });
			}
		}

		const recall = await runLifecycle(
			pi,
			ctx,
			["recall", "--prompt", event.prompt, "--cwd", ctx.cwd, "--session-id", sessionFile],
			"recall",
		);
		if (recall.should_inject && recall.content) {
			messages.push({ customType: "memento-recall", content: recall.content, display: true });
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
		if (!toolContext.should_inject || !toolContext.content) return;

		return {
			content: [...event.content, textPart(`\n\n${toolContext.content}`)],
		};
	});

	pi.registerTool({
		name: "memento_status",
		label: "Memento Status",
		description: "Show memento vault and lifecycle bridge status.",
		parameters: Type.Object({}),
		async execute(_toolCallId, _params, _signal, _onUpdate, ctx) {
			const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
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
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const sessionFile = ctx.sessionManager.getSessionFile() ?? "unknown";
			const payload = await runJson(pi, ctx, [
				"capture",
				"--title",
				params.title,
				"--body",
				params.body,
				"--cwd",
				ctx.cwd,
				"--session-id",
				sessionFile,
			]);
			return { content: [textPart(JSON.stringify(payload, null, 2))], details: payload };
		},
	});

	pi.registerCommand("memento-status", {
		description: "Show memento pi bridge status",
		handler: async (_args, ctx) => {
			const payload = await runJson(pi, ctx, ["status", "--cwd", ctx.cwd]);
			if (payload.error) {
				ctx.ui.notify(`memento bridge failed: ${String(payload.error)}`, "error");
			} else {
				ctx.ui.notify("memento bridge reachable", "success");
				ctx.ui.setWidget("memento", JSON.stringify(payload, null, 2).split("\n"));
			}
		},
	});
}
