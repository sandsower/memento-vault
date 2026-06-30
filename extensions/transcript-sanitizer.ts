import { createHash } from "node:crypto";

const RECORD_CONTENT_CAP = 500;
const TOOL_ARGUMENT_CAP = 160;
const TOOL_RESULT_CAP = 200;

const THOUGHT_BLOCK_TYPES = new Set([
	"thinking",
	"reasoning",
	"redacted_thinking",
	"encrypted_reasoning",
	"internal_reasoning",
	"chain_of_thought",
	"thought",
]);

const THOUGHT_ARTIFACT_KEYS = new Set([
	"thinking",
	"thinkingSignature",
	"thinking_signature",
	"encrypted_content",
	"encryptedContent",
	"reasoning",
	"reasoningSignature",
	"reasoning_signature",
]);

const TOOL_CALL_TYPES = new Set(["toolcall", "tool_call", "tool_use", "function_call"]);
const TOOL_RESULT_TYPES = new Set(["toolresult", "tool_result", "function_call_output"]);
const IMAGE_TYPES = new Set(["image", "input_image"]);

type AnyRecord = Record<string, unknown>;

function isRecord(value: unknown): value is AnyRecord {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}

function compactWhitespace(text: string): string {
	return text.replace(/\s+/g, " ").trim();
}

function normalizeType(value: unknown): string {
	return String(value ?? "").toLowerCase();
}

function isThoughtType(value: unknown): boolean {
	return THOUGHT_BLOCK_TYPES.has(normalizeType(value));
}

function hasThoughtArtifactKey(record: AnyRecord): boolean {
	return Object.keys(record).some((key) => THOUGHT_ARTIFACT_KEYS.has(key));
}

function isThoughtArtifact(record: AnyRecord): boolean {
	return isThoughtType(record.type) || hasThoughtArtifactKey(record);
}

function safeStringify(value: unknown, maxChars: number): string {
	const seen = new WeakSet<object>();
	let rendered: string;
	try {
		rendered = JSON.stringify(value, (_key, nested) => {
			if (isRecord(nested)) {
				if (seen.has(nested)) return "[Circular]";
				seen.add(nested);
				const filtered: AnyRecord = {};
				for (const [key, child] of Object.entries(nested)) {
					if (THOUGHT_ARTIFACT_KEYS.has(key)) continue;
					if (isRecord(child) && isThoughtArtifact(child)) continue;
					if (Array.isArray(child)) filtered[key] = child.filter((item) => !(isRecord(item) && isThoughtArtifact(item)));
					else filtered[key] = child;
				}
				return filtered;
			}
			return nested;
		});
	} catch {
		rendered = String(value);
	}
	if (!rendered) return "";
	return rendered.length > maxChars ? `${rendered.slice(0, maxChars)}…` : rendered;
}

function stringifyToolPayload(value: unknown, maxChars: number): string {
	if (typeof value === "string") return value.length > maxChars ? `${value.slice(0, maxChars)}…` : value;
	return safeStringify(value ?? {}, maxChars);
}

function contentToText(value: unknown): string {
	if (typeof value === "string") return value;
	if (Array.isArray(value)) return value.map((part) => sanitizeContentPart(part)).filter(Boolean).join("\n");
	if (isRecord(value)) {
		if (isThoughtArtifact(value)) return "";
		if (typeof value.text === "string") return value.text;
		if (value.content !== undefined) return contentToText(value.content);
	}
	return stringifyToolPayload(value, TOOL_RESULT_CAP);
}

function renderToolCall(part: AnyRecord): string {
	const name = String(part.name ?? part.toolName ?? (isRecord(part.function) ? part.function.name : undefined) ?? "tool");
	const args = part.arguments ?? part.input ?? part.parameters ?? part.args ?? (isRecord(part.function) ? part.function.arguments : undefined) ?? {};
	return `[tool call] ${name} ${stringifyToolPayload(args, TOOL_ARGUMENT_CAP)}`;
}

function renderToolResult(part: AnyRecord): string {
	const raw = part.text ?? part.content ?? part.output ?? part.result ?? "";
	const text = compactWhitespace(contentToText(raw));
	const truncated = text.length > TOOL_RESULT_CAP;
	return `[tool result] ${text.slice(0, TOOL_RESULT_CAP)}${truncated ? "… [tool result truncated]" : ""}`;
}

function sanitizeContentPart(part: unknown): string {
	if (typeof part === "string") return part;
	if (!isRecord(part)) return "";
	if (isThoughtArtifact(part)) return "";

	const partType = normalizeType(part.type);
	if (partType === "text" && typeof part.text === "string") return part.text;
	if (TOOL_CALL_TYPES.has(partType)) return renderToolCall(part);
	if (TOOL_RESULT_TYPES.has(partType)) return renderToolResult(part);
	if (IMAGE_TYPES.has(partType)) return "[image omitted]";
	if (typeof part.text === "string") return part.text;
	if (part.content !== undefined) return contentToText(part.content);
	return safeStringify(part, RECORD_CONTENT_CAP);
}

export function summarizeRecord(value: unknown, fallbackRole: string): string {
	const record = isRecord(value) ? value : {};
	if (isThoughtType(record.type)) return "";

	const nested = isRecord(record.message) ? record.message : undefined;
	if (nested && isThoughtType(nested.type)) return "";

	const role = String(nested?.role ?? record.role ?? record.type ?? fallbackRole);
	const rawContent = nested?.content ?? record.content ?? record.summary ?? record.text ?? "";
	const content = compactWhitespace(contentToText(rawContent));
	if (!content) return "";
	return `- ${role}: ${content.slice(0, RECORD_CONTENT_CAP)}`;
}

export function sanitizeEventDetails(value: unknown, maxChars = 2000): string {
	return safeStringify(value, maxChars);
}

export function summarizeMessages(messages: unknown): string {
	if (!Array.isArray(messages)) return "Pi agent turn ended; message details unavailable.";
	const summary = messages
		.slice(-8)
		.map((message, index) => summarizeRecord(message, `message-${index + 1}`))
		.filter((line) => line.length > 4)
		.join("\n");
	return summary || "Pi agent turn ended; no message summary available.";
}

export function summarizeSessionEntries(entries: unknown, reason: string): string {
	if (!Array.isArray(entries)) return `Pi ${reason}; session entry details unavailable.`;
	const recent = entries
		.slice(-12)
		.map((entry, index) => summarizeRecord(entry, `entry-${index + 1}`))
		.filter((line) => line.length > 4);
	return [`Pi ${reason}.`, "", "Recent session entries:", ...recent].join("\n");
}

function digestText(text: string): string {
	return createHash("sha256").update(text).digest("hex").slice(0, 16);
}

export function addSessionPointerDigest(body: string, sessionFile: string): string {
	if (!sessionFile || sessionFile === "unknown") return body;
	return [
		"Pi lifecycle capture summary.",
		`Session transcript: ${sessionFile}`,
		`Sanitized summary digest: sha256:${digestText(body)}`,
		"Queue processing should prefer the cleaned session transcript when available.",
		"",
		"Sanitized lifecycle summary:",
		body,
	].join("\n");
}
