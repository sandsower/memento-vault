"""Pi coding agent transcript parser.

Parses Pi JSONL session transcripts into the same metadata and prompt text
shape used by the shared memento triage pipeline. Pi transcripts may contain
reasoning/thinking blocks and large tool results; this adapter omits private
reasoning and summarizes/caps tool payloads before LLM triage sees them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from memento.utils import sanitize_secrets

_EDIT_TOOLS = {"edit", "write", "patch", "apply_patch", "multiedit"}
_READ_TOOLS = {"read"}
_REASONING_PART_TYPES = {"thinking", "reasoning", "reasoning_content", "redacted_reasoning"}
_APPLY_PATCH_PATH_RE = re.compile(r"^\*\*\* (Add File|Delete File|Update File|Move to):\s*(.+?)\s*$")
_PROMPT_WRAPPER_RE = re.compile(
    r"^\s*<(?P<tag>(?:file|prompt|assistant|user|system(?:-[\w-]+)?))\b[^>]*>(?P<body>.*)</(?P=tag)>\s*$",
    re.DOTALL | re.IGNORECASE,
)
_MESSAGE_RECORD_TYPES = {"message", "message_end"}
_PI_EVENT_TYPES = {
    "session",
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
}


def _read_records(transcript_path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(transcript_path, encoding="utf-8") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def looks_like_pi_record(entry: Any) -> bool:
    """Return True when an early JSONL record has Pi's transcript shape."""
    if not isinstance(entry, dict):
        return False
    entry_type = entry.get("type")
    if entry_type in {"message", "message_start", "message_end"} and isinstance(entry.get("message"), dict):
        message = entry["message"]
        return bool(message.get("role") or "content" in message)
    if entry_type == "message_update" and isinstance(entry.get("assistantMessageEvent"), dict):
        return True
    if entry_type in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
        return bool(entry.get("toolName") or entry.get("toolCallId"))
    if entry_type == "session" and ("id" in entry or "cwd" in entry or "version" in entry):
        return True
    if entry_type == "custom_message" and ("content" in entry or "customType" in entry):
        return True
    return entry_type in _PI_EVENT_TYPES


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_metadata(entry: dict[str, Any]) -> dict[str, str | None]:
    session = entry.get("session") if isinstance(entry.get("session"), dict) else {}
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    return {
        "cwd": _first_str(entry.get("cwd"), entry.get("workingDirectory"), session.get("cwd"), metadata.get("cwd")),
        "git_branch": _first_str(
            entry.get("gitBranch"),
            entry.get("git_branch"),
            entry.get("branch"),
            session.get("gitBranch"),
            session.get("git_branch"),
            session.get("branch"),
            metadata.get("gitBranch"),
            metadata.get("git_branch"),
            metadata.get("branch"),
        ),
        "session_id": _first_str(
            entry.get("session_id"),
            entry.get("sessionId"),
            entry.get("id") if entry.get("type") == "session" else None,
            session.get("id"),
            metadata.get("session_id"),
            metadata.get("sessionId"),
        ),
    }


def _clean_prompt_text(text: str) -> str:
    cleaned = text.strip()
    while True:
        wrapper = _PROMPT_WRAPPER_RE.match(cleaned)
        if not wrapper:
            break
        cleaned = wrapper.group("body").strip()
    return cleaned.strip('"').strip("'")


def _text_from_parts(parts: Any) -> list[str]:
    if isinstance(parts, str):
        return [parts]
    if not isinstance(parts, list):
        return []
    output: list[str] = []
    for part in parts:
        if isinstance(part, str):
            output.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in _REASONING_PART_TYPES:
            continue
        if part_type == "text" and isinstance(part.get("text"), str):
            output.append(part["text"])
    return output


def _message_text(message: dict[str, Any]) -> str:
    return "\n\n".join(text.strip() for text in _text_from_parts(message.get("content")) if text.strip()).strip()


def _apply_patch_paths(patch_text: str) -> set[str]:
    paths: set[str] = set()
    last_update_path = None
    for line in patch_text.splitlines():
        match = _APPLY_PATCH_PATH_RE.match(line.strip())
        if not match:
            continue
        marker, path = match.groups()
        if marker == "Move to":
            if last_update_path:
                paths.discard(last_update_path)
            paths.add(path)
            last_update_path = None
        else:
            paths.add(path)
            last_update_path = path if marker == "Update File" else None
    return paths


def _tool_path(arguments: dict[str, Any]) -> str | None:
    return _first_str(arguments.get("file_path"), arguments.get("filePath"), arguments.get("path"))


def _collect_tool_call_files(tool: str, arguments: Any) -> tuple[set[str], set[str]]:
    files_edited: set[str] = set()
    files_read: set[str] = set()
    if not isinstance(arguments, dict):
        arguments = {}
    normalized_tool = tool.lower()
    if normalized_tool == "apply_patch":
        files_edited.update(_apply_patch_paths(str(arguments.get("patchText") or arguments.get("patch_text") or "")))
        return files_edited, files_read
    path = _tool_path(arguments)
    if not path:
        return files_edited, files_read
    if normalized_tool in _EDIT_TOOLS:
        files_edited.add(path)
    elif normalized_tool in _READ_TOOLS:
        files_read.add(path)
    return files_edited, files_read


def _collect_tool_files(message: dict[str, Any]) -> tuple[set[str], set[str]]:
    files_edited: set[str] = set()
    files_read: set[str] = set()
    content = message.get("content")
    if not isinstance(content, list):
        return files_edited, files_read
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "toolCall":
            continue
        tool = str(part.get("name") or part.get("tool") or "")
        arguments = part.get("arguments") or part.get("input") or {}
        edited, read = _collect_tool_call_files(tool, arguments)
        files_edited.update(edited)
        files_read.update(read)
    return files_edited, files_read


def _final_message(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("type") in _MESSAGE_RECORD_TYPES and isinstance(entry.get("message"), dict):
        return entry["message"]
    return None


def parse_transcript(transcript_path: str):
    """Parse a Pi JSONL transcript into the standard session metadata dict."""
    user_count = 0
    assistant_count = 0
    files_edited: set[str] = set()
    files_read: set[str] = set()
    cwd = None
    git_branch = None
    session_id = None
    first_user_prompt = None
    last_assistant_text = None

    for entry in _read_records(transcript_path):
        metadata = _extract_metadata(entry)
        cwd = cwd or metadata["cwd"]
        git_branch = git_branch or metadata["git_branch"]
        session_id = session_id or metadata["session_id"]

        if entry.get("type") == "tool_execution_start":
            edited, read = _collect_tool_call_files(str(entry.get("toolName") or ""), entry.get("args") or {})
            files_edited.update(edited)
            files_read.update(read)
            continue

        message = _final_message(entry)
        if message is None:
            continue
        role = str(message.get("role") or "").lower()
        if role == "user":
            user_count += 1
            if first_user_prompt is None:
                text = _message_text(message)
                if text:
                    cleaned = _clean_prompt_text(text)
                    if cleaned:
                        first_user_prompt = sanitize_secrets(cleaned[:200])
        elif role == "assistant":
            assistant_count += 1
            text = _message_text(message)
            if text:
                last_assistant_text = text
            edited, read = _collect_tool_files(message)
            files_edited.update(edited)
            files_read.update(read)

    last_outcome = None
    if last_assistant_text:
        last_assistant_text = sanitize_secrets(last_assistant_text)
        dot = last_assistant_text.find(".")
        if 0 < dot < 150:
            last_outcome = last_assistant_text[: dot + 1]
        else:
            last_outcome = last_assistant_text[:100]
            if len(last_assistant_text) > 100:
                last_outcome += "..."

    return {
        "cwd": cwd,
        "git_branch": git_branch,
        "exchange_count": min(user_count, assistant_count),
        "user_messages": user_count,
        "files_edited": sorted(files_edited),
        "files_read": sorted(files_read),
        "first_prompt": first_user_prompt,
        "last_outcome": last_outcome,
        "session_id": session_id,
    }


def _format_tool_call(name: Any, arguments: Any) -> str:
    name = name or "tool"
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    path = _tool_path(arguments)
    if path:
        return f"Assistant tool {name}: {path}"
    return f"Assistant tool {name}: {json.dumps(arguments, ensure_ascii=False)[:1000]}"


def _render_tool_call(part: dict[str, Any]) -> str:
    return _format_tool_call(part.get("name") or part.get("tool"), part.get("arguments") or part.get("input") or {})


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item, str):
                    chunks.append(item)
            if chunks:
                return "\n".join(chunks)
        if isinstance(payload.get("text"), str):
            return payload["text"]
    return json.dumps(payload, ensure_ascii=False)


def _cap_tool_result(text: str, per_tool_cap: int) -> str:
    suffix = "\n[tool result truncated]" if len(text) > per_tool_cap else ""
    return f"{text[:per_tool_cap]}{suffix}"


def _render_tool_result(part: dict[str, Any], per_tool_cap: int) -> str:
    text = _payload_text(part.get("text") or part.get("content") or "")
    return f"Tool: {_cap_tool_result(text, per_tool_cap)}"


def _render_text_part(role: str, text: str, per_tool_cap: int) -> str | None:
    if not text.strip():
        return None
    if role.lower().startswith("tool"):
        return f"{role}: {_cap_tool_result(text, per_tool_cap)}"
    return f"{role}: {text}"


def _render_message_parts(role: str, parts: Any, per_tool_cap: int) -> list[str]:
    if isinstance(parts, str):
        rendered = _render_text_part(role, parts, per_tool_cap)
        return [rendered] if rendered else []
    if not isinstance(parts, list):
        return []
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text = _render_text_part(role, part, per_tool_cap)
            if text:
                rendered.append(text)
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in _REASONING_PART_TYPES:
            continue
        if part_type == "text" and isinstance(part.get("text"), str):
            text = _render_text_part(role, part["text"], per_tool_cap)
            if text:
                rendered.append(text)
        elif part_type == "toolCall":
            rendered.append(_render_tool_call(part))
        elif part_type in {"toolResult", "tool_result"}:
            rendered.append(_render_tool_result(part, per_tool_cap))
        elif part_type in {"image", "input_image"}:
            rendered.append(f"{role}: [image omitted]")
    return rendered


def _render_tool_execution(entry: dict[str, Any], per_tool_cap: int) -> str | None:
    name = entry.get("toolName") or "tool"
    entry_type = entry.get("type")
    if entry_type == "tool_execution_start":
        return _format_tool_call(name, entry.get("args") or {})
    if entry_type == "tool_execution_end":
        result = _payload_text(entry.get("result") or "")
        return f"Tool result {name}: {_cap_tool_result(result, per_tool_cap)}"
    return None


def _finalized_tool_call_ids(records: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for entry in records:
        message = _final_message(entry)
        if message is None:
            continue
        tool_call_id = _first_str(message.get("toolCallId"), message.get("tool_call_id"))
        if tool_call_id:
            ids.add(tool_call_id)
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "toolCall":
                continue
            tool_call_id = _first_str(part.get("id"), part.get("toolCallId"), part.get("tool_call_id"))
            if tool_call_id:
                ids.add(tool_call_id)
    return ids


def render_transcript_text(transcript_path: str, session_id: str | None = None, per_tool_cap: int = 3000):
    """Render a Pi JSONL transcript as prompt-safe text for shared triage.

    ``session_id`` is accepted for dispatcher symmetry; Pi transcript files are
    already per-session, so no selection is needed. Streaming ``message_update``
    records are intentionally ignored because they can contain private thinking
    deltas; finalized message and tool execution records carry the durable signal.
    """
    del session_id
    lines: list[str] = []
    records = _read_records(transcript_path)
    finalized_tool_call_ids = _finalized_tool_call_ids(records)
    for entry in records:
        entry_type = entry.get("type")
        message = _final_message(entry)
        if message is not None:
            raw_role = str(message.get("role") or "message")
            raw_role_lower = raw_role.lower()
            if raw_role_lower == "custom":
                role = f"Custom {message.get('customType') or 'message'}"
            elif raw_role_lower in {"tool", "toolresult", "tool_result"}:
                tool_name = _first_str(message.get("toolName"), message.get("tool"), message.get("name"))
                role = f"Tool result {tool_name}" if tool_name else "Tool"
            else:
                role = raw_role.title()
            lines.extend(_render_message_parts(role, message.get("content"), per_tool_cap))
        elif entry_type in {"tool_execution_start", "tool_execution_end"}:
            tool_call_id = _first_str(entry.get("toolCallId"), entry.get("tool_call_id"))
            if tool_call_id and tool_call_id in finalized_tool_call_ids:
                continue
            rendered = _render_tool_execution(entry, per_tool_cap)
            if rendered:
                lines.append(rendered)
        elif entry_type == "custom_message":
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                custom_type = entry.get("customType") or "message"
                lines.append(f"Custom {custom_type}: {content}")
    return sanitize_secrets("\n".join(lines))
