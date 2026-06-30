import { Key, matchesKey, truncateToWidth, visibleWidth } from "@mariozechner/pi-tui";

export type MementoPanelView = "actions" | "status" | "queue" | "process";

export type MementoQueuedCaptureSummary = {
	id: string;
	title: string;
	excerpt: string;
	generatedSummary?: string;
	project: string;
	branch: string;
	size: string;
};

export type MementoPanelState = {
	view: MementoPanelView;
	selectedIndex: number;
	message?: string;
	confirmProcess?: boolean;
	confirmDiscard?: boolean;
	queueItemCount?: number;
	processItemCount?: number;
	selectedCaptureIds?: string[];
	inspectIndex?: number;
	discardCapture?: MementoQueuedCaptureSummary;
};

export type MementoPanelAction =
	| { type: "close" }
	| { type: "capture-current" }
	| { type: "refresh" }
	| { type: "toggle-widget" }
	| { type: "show"; view: MementoPanelView }
	| { type: "toggle-capture" }
	| { type: "request-discard" }
	| { type: "inspect-group" }
	| { type: "dry-run" }
	| { type: "process" }
	| { type: "retry-failed" }
	| { type: "discard" };

const ACTIONS = [
	{ label: "Capture current session", action: { type: "capture-current" } as MementoPanelAction },
	{ label: "Review queued captures", action: { type: "show", view: "queue" } as MementoPanelAction },
	{ label: "Preview queued processing", action: { type: "dry-run" } as MementoPanelAction },
	{ label: "Process queued captures", action: { type: "process" } as MementoPanelAction },
	{ label: "Discard highlighted queued capture", action: { type: "request-discard" } as MementoPanelAction },
	{ label: "Status / diagnostics", action: { type: "show", view: "status" } as MementoPanelAction },
	{ label: "Toggle footer details", action: { type: "toggle-widget" } as MementoPanelAction },
];

export function reduceMementoPanelState(
	state: MementoPanelState,
	input: string,
): { state: MementoPanelState; action?: MementoPanelAction } {
	const key = normalizeInput(input);
	const maxIndex = state.view === "actions" ? ACTIONS.length - 1 : Math.max(0, visibleItemCountForView(state) - 1);
	const selectedIndex = Math.min(Math.max(0, state.selectedIndex), maxIndex);

	if (state.confirmProcess) {
		if (key === "y") return { state: { ...state, confirmProcess: false, selectedIndex }, action: { type: "process" } };
		if (key === "n" || key === "escape" || key === "q") return { state: { ...state, confirmProcess: false, selectedIndex } };
		return { state: { ...state, selectedIndex } };
	}
	if (state.confirmDiscard) {
		if (key === "y") return { state: { ...state, confirmDiscard: false, selectedIndex }, action: { type: "discard" } };
		if (key === "n" || key === "escape" || key === "q") return { state: { ...state, confirmDiscard: false, selectedIndex, discardCapture: undefined } };
		return { state: { ...state, selectedIndex } };
	}

	if (key === "down" || key === "j") return { state: { ...state, selectedIndex: Math.min(maxIndex, selectedIndex + 1) } };
	if (key === "up" || key === "k") return { state: { ...state, selectedIndex: Math.max(0, selectedIndex - 1) } };
	if (key === "escape" || key === "q") {
		if (state.inspectIndex !== undefined) return { state: { ...state, inspectIndex: undefined, selectedIndex } };
		if (state.view !== "actions") return { state: { ...state, view: "actions", selectedIndex: 0 } };
		return { state: { ...state, selectedIndex }, action: { type: "close" } };
	}
	if (key === "r") return { state: { ...state, selectedIndex }, action: { type: "refresh" } };
	if (key === "s") return { state: { ...state, view: "status", selectedIndex: 0, inspectIndex: undefined }, action: { type: "show", view: "status" } };
	if (key === "a") return { state: { ...state, view: "actions", selectedIndex: 0, inspectIndex: undefined } };
	if (key === "c") return { state: { ...state, selectedIndex }, action: { type: "capture-current" } };
	if (key === "w") return { state: { ...state, selectedIndex }, action: { type: "toggle-widget" } };
	if (key === " " && state.view === "queue") return { state: { ...state, selectedIndex }, action: { type: "toggle-capture" } };
	if (key === "x" && state.view === "queue") return { state: { ...state, selectedIndex }, action: { type: "request-discard" } };
	if (key === "i" && state.view === "process") return { state: { ...state, inspectIndex: selectedIndex }, action: { type: "inspect-group" } };
	if (key === "t" && state.view === "process") return { state: { ...state, selectedIndex }, action: { type: "retry-failed" } };
	if (key === "d") return { state: { ...state, view: "process", selectedIndex: 0, inspectIndex: undefined }, action: { type: "dry-run" } };
	if (key === "p") return { state: { ...state, view: "process", selectedIndex: 0, inspectIndex: undefined, confirmProcess: true } };
	if (key === "enter") {
		if (state.view === "actions") return { state: { ...state, selectedIndex }, action: ACTIONS[selectedIndex]?.action };
		if (state.view === "queue") return { state: { ...state, selectedIndex }, action: { type: "toggle-capture" } };
		if (state.view === "process") return { state: { ...state, inspectIndex: selectedIndex }, action: { type: "inspect-group" } };
		return { state: { ...state, view: "actions", selectedIndex: 0 } };
	}
	return { state: { ...state, selectedIndex } };
}

export function renderMementoPanelLines(
	state: MementoPanelState,
	data: { status?: Record<string, unknown>; queue?: Record<string, unknown>; process?: Record<string, unknown>; widgetEnabled: boolean },
	width: number,
): string[] {
	const body = [summaryLine(data.status, data.queue, data.process), ""];
	if (state.message) body.push(state.message, "");
	if (state.confirmProcess) body.push(`Process ${state.selectedCaptureIds?.length ?? 0} selected queued capture(s) now? y/N`, "");
	if (state.confirmDiscard && state.discardCapture) {
		body.push("Discard this queued capture? It will be archived, not deleted. y/N", "");
		body.push(`ID: ${state.discardCapture.id}`, `Title: ${fitLine(state.discardCapture.title, 84)}`);
		if (state.discardCapture.generatedSummary) body.push(`Summary: ${fitLine(state.discardCapture.generatedSummary, 78)}`);
		if (state.discardCapture.excerpt) body.push(`Excerpt: ${fitLine(state.discardCapture.excerpt, 78)}`);
		body.push(
			`Project: ${state.discardCapture.project}${state.discardCapture.branch ? `/${state.discardCapture.branch}` : ""}`,
			`Size: ${state.discardCapture.size}`,
			"",
		);
	}
	if (state.view === "actions") body.push(...renderActions(state.selectedIndex, data.widgetEnabled));
	else if (state.view === "status") body.push(...formatStatusLines(data.status, { includeDetails: true }));
	else if (state.view === "queue") body.push(...formatQueueLines(data.queue, { limit: 10, selectedIds: state.selectedCaptureIds, cursorIndex: state.selectedIndex }));
	else body.push(...formatProcessLines(data.process, { cursorIndex: state.selectedIndex, inspectIndex: state.inspectIndex }));
	const help = state.view === "process"
		? "↑↓/j/k move · i inspect · t retry failed · d dry-run · p process · r refresh · q back"
		: "↑↓/j/k move · space select · x discard · i inspect · d dry-run · p process · r refresh · q back";
	body.push("", help);
	return frameLines("Memento Vault", body, Math.max(1, width));
}

export function renderMementoStatusText(status?: Record<string, unknown>, queue?: Record<string, unknown>, options: { pinned?: boolean; process?: Record<string, unknown> } = {}): string {
	if (status?.error) return `🧠 ! ${String(status.error)} · /memento`;
	const process = options.process;
	const processStatus = String(process?.status ?? "");
	const queueCount = numberValue(queue?.count ?? status?.queued_capture_count);
	if (processStatus === "running") return `🧠 processing ${numberValue(process?.completed_group_count)}/${numberValue(process?.group_count)} · ${queueCount}q · /memento`;
	const piBridge = recordValue(status?.piBridge);
	const bridgeHealth = recordValue(status?.pi_bridge_health) ?? recordValue(piBridge?.health);
	const bridgeCount = numberValue(bridgeHealth?.recent_failure_count ?? bridgeHealth?.failures ?? bridgeHealth?.events);
	const bridgeSuffix = bridgeHealth?.status === "warn" ? ` · bridge ${bridgeCount || "!"}` : "";
	const auditCount = numberValue(status?.capture_audit_count);
	const auditSuffix = auditCount > 0 ? ` · audit ${auditCount}` : "";
	if (["failed", "interrupted"].includes(processStatus)) {
		const failedCount = numberValue(process?.failed_group_count ?? process?.retryable_group_count);
		return `🧠 processing ${processStatus}${failedCount > 0 ? ` · ${failedCount} failed` : ""} · ${queueCount}q${bridgeSuffix} · /memento`;
	}
	const vault = status?.vault_exists === false ? "!" : "✓";
	const notes = numberValue(status?.note_count);
	if (queueCount > 0 || options.pinned || bridgeHealth?.status === "warn" || auditCount > 0) return `🧠 ${queueCount}q · ${vault} · ${notes}n${bridgeSuffix}${auditSuffix} · /memento`;
	return `🧠 ${vault}${bridgeSuffix}${auditSuffix} · /memento`;
}

export function renderMementoWidgetLines(status?: Record<string, unknown>, queue?: Record<string, unknown>, width = 100): string[] | undefined {
	const queueCount = numberValue(queue?.count ?? status?.queued_capture_count);
	if (queueCount <= 0 && !status?.error) return undefined;
	return [fitLine(renderMementoStatusText(status, queue, { pinned: true }), width)];
}

export function formatStatusSummary(status?: Record<string, unknown>, queue?: Record<string, unknown>): string {
	return summaryLine(status, queue);
}

export function formatStatusLines(status?: Record<string, unknown>, options: { includeDetails?: boolean } = {}): string[] {
	if (!status) return ["Status not loaded. Press r to refresh."];
	if (status.error) return [`Bridge error: ${String(status.error)}`];
	const piBridge = recordValue(status.piBridge);
	const lifecycle = recordValue(status.lifecycle);
	const bridgeConfig = recordValue(piBridge?.config);
	const bridgeHealth = recordValue(status.pi_bridge_health) ?? recordValue(piBridge?.health);
	const lines = [
		`Status: ${status.vault_exists === false ? "! vault missing" : "✓ reachable"} · qmd ${boolMark(status.qmd_available)} · remote ${status.remote_configured ? boolMark(status.remote_available) : "off"}`,
		`Vault: ${shortPath(String(status.vault_path ?? "unknown"), 80)}`,
		`Project: ${String(status.project_slug ?? "unknown")}`,
		`Notes: ${numberValue(status.note_count)} · Projects: ${numberValue(status.project_count)} · Queue: ${numberValue(status.queued_capture_count)}`,
		`Lifecycle: briefing ${boolMark(lifecycle?.briefing)} · recall ${boolMark(lifecycle?.prompt_recall)} · tool context ${boolMark(lifecycle?.tool_context)} · capture queue ${boolMark(lifecycle?.capture_queue)}`,
	];
	if (bridgeHealth?.status === "warn") {
		const count = numberValue(bridgeHealth?.recent_failure_count ?? bridgeHealth?.failures ?? bridgeHealth?.events);
		const lastFailure = recordValue(bridgeHealth?.last_failure);
		const lastError = String(lastFailure?.error ?? "");
		lines.push(`Pi bridge: ${count || 1} recent failure${count === 1 ? "" : "s"}${lastError ? ` · last: ${fitLine(lastError, 72)}` : ""}`);
	}
	if (options.includeDetails) {
		const lastAudit = recordValue(status.last_capture_audit) ?? recordValue(piBridge?.lastCaptureAudit);
		lines.push(
			`Config: enabled ${boolMark(bridgeConfig?.enabled)} · auto capture ${boolMark(bridgeConfig?.autoCapture)} · process queue ${boolMark(bridgeConfig?.processQueue)}`,
			`Last lifecycle: ${String(piBridge?.lastLifecycleReason ?? "unknown")}`,
			`Capture audit: ${numberValue(status.capture_audit_count ?? 0)} record${numberValue(status.capture_audit_count ?? 0) === 1 ? "" : "s"}${lastAudit ? ` · last ${String(lastAudit?.decision ?? lastAudit?.reason ?? "unknown")}` : ""}`,
		);
		if (bridgeHealth) {
			const lastFailure = recordValue(bridgeHealth.last_failure);
			lines.push(
				`Pi bridge health: ${String(bridgeHealth.status ?? "unknown")} · recent ${numberValue(bridgeHealth.recent_failure_count ?? bridgeHealth.failures ?? bridgeHealth.events)}`,
				`Pi bridge last failure: ${String(lastFailure?.operation ?? lastFailure?.action ?? "unknown")} · ${String(lastFailure?.backend ?? "unknown")} · ${String(lastFailure?.project ?? "unknown")} · ${String(lastFailure?.session_id ?? "unknown")}`,
			);
		}
	}
	return lines;
}

export function formatQueueLines(queue?: Record<string, unknown>, options: { limit?: number; selectedIds?: string[]; cursorIndex?: number } = {}): string[] {
	if (!queue) return ["Queue not loaded. Press r to refresh."];
	if (queue.error) return [`Queue error: ${String(queue.error)}`];
	const captures = Array.isArray(queue.captures) ? queue.captures as Record<string, unknown>[] : [];
	const count = numberValue(queue.count ?? captures.length);
	if (count === 0) return ["Queue empty."];
	const selected = new Set(options.selectedIds ?? []);
	const selectedCount = selected.size;
	const lines = [`Queue: ${count} capture${count === 1 ? "" : "s"} · ${selectedCount} selected`];
	for (const [index, capture] of captures.slice(0, options.limit ?? 8).entries()) {
		const metadata = recordValue(capture.metadata);
		const project = String(metadata?.project ?? metadata?.project_slug ?? "unknown");
		const branch = String(metadata?.branch ?? "");
		const session = String(metadata?.session_id ?? "");
		const id = String(capture.id ?? "unknown");
		const created = formatDate(String(capture.created_at ?? ""));
		const cursor = index === options.cursorIndex ? ">" : " ";
		const check = selected.has(id) ? "[x]" : "[ ]";
		const size = formatSize(capture.body_size_bytes ?? capture.body_char_count);
		lines.push(`${cursor} ${check} ${compactId(id)} · ${size} · ${String(capture.reason ?? capture.source_event ?? "capture")} · ${created}`);
		lines.push(`    ${fitLine(String(capture.title ?? "Untitled capture"), 88)}`);
		lines.push(`    ${fitLine(`${project}${branch ? `/${branch}` : ""}${session ? ` · session: ${shortPath(session, 42)}` : ""}`, 88)}`);
		const generatedSummary = generatedSummaryText(capture);
		if (generatedSummary) lines.push(`    Summary: ${fitLine(generatedSummary, 79)}`);
		const excerpt = String(capture.body_excerpt ?? "");
		if (excerpt) lines.push(`    ${fitLine(excerpt, 88)}`);
		const lifecycle = recordValue(capture.lifecycle);
		if (lifecycle) {
			const lifecycleSummary = [
				String(lifecycle.source_event ?? capture.source_event ?? ""),
				lifecycle.turn_count !== undefined ? `turns ${String(lifecycle.turn_count)}` : "",
				lifecycle.tool_call_count !== undefined ? `tools ${String(lifecycle.tool_call_count)}` : "",
				lifecycle.file_edit_count !== undefined ? `edits ${String(lifecycle.file_edit_count)}` : "",
			]
				.filter(Boolean)
				.join(" · ");
			if (lifecycleSummary) lines.push(`    lifecycle: ${fitLine(lifecycleSummary, 88)}`);
		}
	}
	if (count > captures.length) lines.push(`… ${count - captures.length} more not loaded`);
	return lines;
}

export function formatProcessLines(payload?: Record<string, unknown>, options: { cursorIndex?: number; inspectIndex?: number } = {}): string[] {
	if (!payload) return ["No process preview/status yet. Press d to dry-run selected captures or p to process."];
	if (payload.error) return [`Process error: ${String(payload.error)}`, String(payload.guidance ?? "")].filter(Boolean);
	const groups = Array.isArray(payload.groups) ? payload.groups as Record<string, unknown>[] : [];
	const status = String(payload.status ?? (payload.dry_run ? "preview" : "result"));
	const completed = numberValue(payload.completed_group_count);
	const groupCount = numberValue(payload.group_count ?? groups.length);
	const lines = [
		payload.dry_run ? "Process preview" : `Process ${status}`,
		`Selected: ${numberValue(payload.selected_capture_count)} capture(s) · Groups: ${groupCount}${status === "running" ? ` · Progress: ${completed}/${groupCount}` : ""}`,
	];
	const oversizeTranscripts = numberValue(payload.oversize_transcript_group_count);
	const missingTranscripts = numberValue(payload.missing_transcript_group_count);
	const fallbackTranscripts = numberValue(payload.transcript_fallback_group_count);
	if (oversizeTranscripts || missingTranscripts || fallbackTranscripts) {
		lines.push(`Transcripts: ${missingTranscripts} missing · ${oversizeTranscripts} oversize · ${fallbackTranscripts} fallback`);
	}
	if (payload.run_id) lines.push(`Run: ${String(payload.run_id)}${payload.current_group_id ? ` · current: ${String(payload.current_group_id)}` : ""}`);
	for (const [index, group] of groups.slice(0, 10).entries()) {
		const project = String(group.project ?? "unknown");
		const branch = String(group.branch ?? "");
		const captureCount = Array.isArray(group.capture_ids) ? group.capture_ids.length : numberValue(group.capture_count);
		const cursor = index === options.cursorIndex ? ">" : " ";
		const groupStatus = String(group.status ?? "pending");
		const notes = Array.isArray(group.created) && group.created.length > 0 ? ` · notes: ${group.created.length}` : "";
		lines.push(`${cursor} ${index + 1}. ${groupStatus} · ${project}${branch ? `/${branch}` : ""} · ${captureCount} capture(s)${notes}`);
		if (group.session_id) lines.push(`     session: ${shortPath(String(group.session_id), 84)}`);
		if (group.discard_reason) lines.push(`     no notes: ${fitLine(String(group.discard_reason), 80)}`);
		if (group.error || group.reason) lines.push(`     ${fitLine(String(group.error ?? group.reason), 80)}`);
		if (group.status === "failed" && group.log_markdown) lines.push(`     log: ${shortPath(String(group.log_markdown), 78)}`);
	}
	const retryable = numberValue(payload.retryable_group_count ?? groups.filter((group) => String(group.status ?? "") === "failed").length);
	if (retryable > 0) lines.push(`Retryable failed groups: ${retryable} · select one and press t`);
	const inspected = options.inspectIndex !== undefined ? groups[options.inspectIndex] : undefined;
	if (inspected) {
		lines.push("", `Artifacts for ${String(inspected.group_id ?? `group ${options.inspectIndex + 1}`)}:`);
		for (const key of ["input_markdown", "result_json", "log_markdown"] as const) {
			if (inspected[key]) lines.push(`  ${key}: ${shortPath(String(inspected[key]), 86)}`);
		}
	}
	if (payload.remaining !== undefined) lines.push(`Remaining queue: ${numberValue(payload.remaining)}`);
	return lines;
}

function renderActions(selectedIndex: number, widgetEnabled: boolean): string[] {
	const lines = ["Actions"];
	for (const [index, item] of ACTIONS.entries()) {
		const cursor = index === selectedIndex ? ">" : " ";
		const suffix = item.label === "Toggle footer details" ? ` (${widgetEnabled ? "detailed" : "compact"})` : "";
		lines.push(`${cursor} ${item.label}${suffix}`);
	}
	return lines;
}

export function queueCaptureSummary(queue?: Record<string, unknown>, index = 0): MementoQueuedCaptureSummary | undefined {
	const captures = Array.isArray(queue?.captures) ? queue.captures as Record<string, unknown>[] : [];
	if (captures.length === 0) return undefined;
	const safeIndex = Math.min(Math.max(0, Math.floor(index)), captures.length - 1);
	const capture = captures[safeIndex];
	const metadata = recordValue(capture.metadata);
	const id = String(capture.id ?? "");
	if (!id) return undefined;
	return {
		id,
		title: String(capture.title ?? "Untitled capture"),
		excerpt: String(capture.body_excerpt ?? ""),
		generatedSummary: generatedSummaryText(capture),
		project: String(metadata?.project ?? metadata?.project_slug ?? "unknown"),
		branch: String(metadata?.branch ?? ""),
		size: formatSize(capture.body_size_bytes ?? capture.body_char_count),
	};
}

function generatedSummaryText(capture: Record<string, unknown>): string {
	const generated = recordValue(capture.generated_summary);
	if (!generated || generated.status !== "ok") return "";
	return String(generated.text ?? "").trim();
}

function summaryLine(status?: Record<string, unknown>, queue?: Record<string, unknown>, process?: Record<string, unknown>): string {
	if (status?.error) return `Bridge error: ${String(status.error)}`;
	const queueCount = numberValue(queue?.count ?? status?.queued_capture_count);
	const processStatus = String(process?.status ?? "");
	const suffix = processStatus === "running" ? ` · processing ${numberValue(process?.completed_group_count)}/${numberValue(process?.group_count)}` : "";
	return `Vault ${status?.vault_exists === false ? "!" : "✓"} · qmd ${boolMark(status?.qmd_available)} · ${numberValue(status?.note_count)} notes · ${queueCount} queued${suffix}`;
}

function visibleItemCountForView(state: MementoPanelState): number {
	if (state.view === "queue") return state.queueItemCount ?? 1;
	if (state.view === "process") return state.processItemCount ?? 1;
	return 1;
}

function normalizeInput(input: string): string {
	if (matchesKey(input, Key.down)) return "down";
	if (matchesKey(input, Key.up)) return "up";
	if (matchesKey(input, Key.enter)) return "enter";
	if (matchesKey(input, Key.escape)) return "escape";
	if (input === " ") return " ";
	if (input.length === 1) return input.toLowerCase();
	return input;
}

function boolMark(value: unknown): string {
	return value ? "✓" : "off";
}

function numberValue(value: unknown): number {
	const number = typeof value === "number" ? value : Number.parseInt(String(value ?? "0"), 10);
	return Number.isFinite(number) ? number : 0;
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
	return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function compactId(id: string): string {
	return id.length > 18 ? `${id.slice(0, 15)}…` : id;
}

function formatDate(raw: string): string {
	const date = new Date(raw);
	if (!Number.isFinite(date.getTime())) return raw || "unknown";
	return date.toISOString().slice(0, 16).replace("T", " ");
}

function formatSize(value: unknown): string {
	const bytes = numberValue(value);
	if (bytes <= 0) return "0 B";
	if (bytes < 1024) return `${bytes} B`;
	return `${(bytes / 1024).toFixed(1)} KB`;
}

function shortPath(path: string, width: number): string {
	const home = process.env.HOME;
	const display = home && path.startsWith(home) ? `~${path.slice(home.length)}` : path;
	return fitLine(display, width);
}

function frameLines(title: string, body: string[], width: number): string[] {
	if (width < 8) return [title, ...body].map((line) => fitLine(line, width));
	const innerWidth = Math.max(1, width - 4);
	const titleText = ` ${title} `;
	const topFill = Math.max(0, width - 2 - visibleWidth(titleText));
	const top = `┌${titleText}${"─".repeat(topFill)}┐`;
	const bottom = `└${"─".repeat(Math.max(0, width - 2))}┘`;
	const framed = body.map((line) => {
		const content = padCell(fitLine(line, innerWidth), innerWidth);
		return `│ ${content} │`;
	});
	return [top, ...framed, bottom].map((line) => fitLine(line, width));
}

function fitLine(value: string, width: number): string {
	return truncateToWidth(value, Math.max(1, width));
}

function padCell(value: string, width: number): string {
	const padding = Math.max(0, width - visibleWidth(value));
	return `${value}${" ".repeat(padding)}`;
}
