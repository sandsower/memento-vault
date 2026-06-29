import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export const DEFAULT_PROCESS_QUEUE_MODEL = "claude-sonnet-4-20250514";

export const defaultConfig = {
	enabled: true,
	briefing: true,
	promptRecall: true,
	toolContext: true,
	autoCapture: true,
	captureQueue: true,
	processQueue: true,
	processQueueOnSessionClose: false,
	processQueueMaxCaptures: 3,
	processQueueModel: DEFAULT_PROCESS_QUEUE_MODEL,
	maxInjectedChars: 4000,
	maxToolContextPerSession: 5,
};

function envBool(name) {
	const raw = process.env[name];
	if (raw === undefined) return undefined;
	return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

function envInt(name) {
	const raw = process.env[name];
	if (raw === undefined) return undefined;
	const parsed = Number.parseInt(raw, 10);
	return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

function readJson(path) {
	if (!existsSync(path)) return undefined;
	return JSON.parse(readFileSync(path, "utf8"));
}

function bridgeConfigFrom(raw) {
	const root = raw && typeof raw === "object" ? raw : undefined;
	const memento = root?.memento && typeof root.memento === "object" ? root.memento : undefined;
	const candidate = (memento?.piBridge ?? root?.piBridge ?? root) || undefined;
	const partial = {};
	if (!candidate || typeof candidate !== "object") return partial;

	for (const key of ["enabled", "briefing", "promptRecall", "toolContext", "autoCapture", "captureQueue", "processQueue", "processQueueOnSessionClose"]) {
		if (typeof candidate[key] === "boolean") partial[key] = candidate[key];
	}
	for (const key of ["maxInjectedChars", "maxToolContextPerSession", "processQueueMaxCaptures"]) {
		if (typeof candidate[key] === "number" && Number.isFinite(candidate[key]) && candidate[key] >= 0) partial[key] = candidate[key];
	}
	if (typeof candidate.processQueueModel === "string" || candidate.processQueueModel === null) partial.processQueueModel = candidate.processQueueModel;
	return partial;
}

function applyEnv(config) {
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

export function loadConfig(cwd = process.cwd()) {
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
