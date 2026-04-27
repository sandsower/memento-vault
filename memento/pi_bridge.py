"""JSON CLI adapter for pi's TypeScript extension.

The pi runtime loads TypeScript/JavaScript extensions, so the extension calls
this module as a short-lived Python process. Lifecycle policy remains in
memento.lifecycle; this module only translates CLI JSON to LifecycleResult JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import re
import traceback
from pathlib import Path
from typing import Any

from memento.config import detect_project, get_config, get_vault
from memento.lifecycle import build_briefing, build_recall, build_tool_context, strip_injection
from memento.search import enhance_results, has_qmd, qmd_get, qmd_search_with_extras
from memento.remote_client import get as remote_get
from memento.remote_client import is_remote, search as remote_search, status as remote_status
from memento.store import write_note


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _error_payload(source: str, exc: Exception) -> dict[str, Any]:
    return {
        "should_inject": False,
        "content": "",
        "source": source,
        "results": [],
        "reason": "error",
        "metadata": {
            "error": str(exc),
            "error_type": type(exc).__name__,
        },
    }


def _run_lifecycle(source: str, fn, *args: Any) -> int:
    try:
        return _emit(fn(*args).to_dict())
    except Exception as exc:  # pragma: no cover - traceback branch asserted by payload shape
        traceback.print_exc(file=sys.stderr)
        return _emit(_error_payload(source, exc))


def _status(cwd: str = "") -> dict[str, Any]:
    vault = get_vault()
    project_slug, _ticket = detect_project(cwd, None) if cwd else ("unknown", None)
    notes_dir = vault / "notes"
    projects_dir = vault / "projects"
    remote_available = False
    remote_error = None
    if is_remote():
        try:
            remote = remote_status()
            remote_available = bool(remote and "error" not in remote)
            remote_error = remote.get("error") if isinstance(remote, dict) else None
        except Exception as exc:
            remote_error = str(exc)
    return {
        "vault_path": str(vault),
        "vault_exists": vault.exists(),
        "project_slug": project_slug,
        "qmd_available": has_qmd(),
        "remote_configured": is_remote(),
        "remote_available": remote_available,
        "remote_error": remote_error,
        "note_count": len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0,
        "project_count": len(list(projects_dir.glob("*.md"))) if projects_dir.exists() else 0,
        "lifecycle": {
            "briefing": get_config().get("session_briefing", True),
            "prompt_recall": get_config().get("prompt_recall", True),
            "tool_context": get_config().get("tool_context", True),
            "auto_capture": False,
        },
    }


def _search(query: str, limit: int, cwd: str = "") -> dict[str, Any]:
    if not query.strip():
        return {"results": [], "reason": "empty-query"}
    if not has_qmd():
        if is_remote():
            results = remote_search(query=query, limit=limit, cwd=cwd)
            return {"results": results, "source": "remote"} if results else {"results": [], "reason": "no-results"}
        return {"results": [], "reason": "qmd-unavailable"}
    limit = max(1, min(int(limit), 20))
    results = qmd_search_with_extras(query, limit=limit, semantic=False, timeout=10, min_score=0.0)
    results = enhance_results(results, cwd=cwd or None) if results else []
    sanitized = []
    for result in results[:limit]:
        sanitized.append(
            {
                "path": result.get("path", ""),
                "title": strip_injection(result.get("title", "")),
                "score": round(result.get("score", 0.0), 4),
                "snippet": strip_injection(result.get("snippet", "")),
            }
        )
    return {"results": sanitized}


def _get(path: str) -> dict[str, Any]:
    if not path.strip():
        return {"error": "path is required"}
    vault = get_vault()
    note_path = path.strip()
    if not note_path.endswith(".md"):
        note_path = f"notes/{note_path}.md"
    elif not note_path.startswith("notes/") and "/" not in note_path:
        note_path = f"notes/{note_path}"

    full_path = (vault / note_path).resolve()
    vault_resolved = vault.resolve()
    if full_path != vault_resolved and vault_resolved not in full_path.parents:
        return {"error": "Invalid path: traversal outside vault"}
    if full_path.exists():
        content = full_path.read_text(errors="replace")
        title = Path(note_path).stem
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"').strip("'")
        return {"path": note_path, "title": strip_injection(title), "content": strip_injection(content)}

    result = qmd_get(note_path)
    if result:
        return {
            "path": result.get("path", note_path),
            "title": strip_injection(result.get("title", "")),
            "content": strip_injection(result.get("content", "")),
        }
    if is_remote():
        remote_result = remote_get(note_path)
        if remote_result:
            return {
                "path": remote_result.get("path", note_path),
                "title": strip_injection(remote_result.get("title", "")),
                "content": strip_injection(remote_result.get("content", "")),
                "source": "remote",
            }
    return {"error": f"Note not found: {note_path}"}


def _capture(title: str, body: str, cwd: str, session_id: str) -> dict[str, Any]:
    if not title.strip():
        return {"error": "title is required"}
    if not body.strip():
        return {"error": "body is required"}
    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}
    project_slug, _ticket = detect_project(cwd, None) if cwd else ("unknown", None)
    note_path = write_note(
        vault,
        title.strip(),
        body.strip(),
        "session",
        ["pi", project_slug] if project_slug != "unknown" else ["pi"],
        source="pi",
        project=project_slug if project_slug != "unknown" else None,
        session_id=session_id if session_id != "unknown" else None,
    )
    return {"path": str(note_path.relative_to(vault)), "title": title.strip(), "queued": False}


def _run_json(source: str, fn, *args: Any) -> int:
    try:
        return _emit(fn(*args))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        traceback.print_exc(file=sys.stderr)
        return _emit({"error": str(exc), "source": source, "reason": "error", "error_type": type(exc).__name__})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Memento pi lifecycle JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    briefing = sub.add_parser("briefing", help="Build first-turn/session briefing context")
    briefing.add_argument("--cwd", default="")
    briefing.add_argument("--session-id", default="unknown")

    recall = sub.add_parser("recall", help="Build prompt recall context")
    recall.add_argument("--prompt", default="")
    recall.add_argument("--cwd", default="")
    recall.add_argument("--session-id", default="unknown")

    tool_context = sub.add_parser("tool-context", help="Build read-tool context")
    tool_context.add_argument("--tool-name", default="")
    tool_context.add_argument("--file-path", default="")
    tool_context.add_argument("--cwd", default="")
    tool_context.add_argument("--session-id", default="unknown")

    status = sub.add_parser("status", help="Show memento status")
    status.add_argument("--cwd", default="")

    search = sub.add_parser("search", help="Search memento notes")
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--cwd", default="")

    get = sub.add_parser("get", help="Read a memento note")
    get.add_argument("--path", default="")

    capture = sub.add_parser("capture", help="Manually capture a memento note")
    capture.add_argument("--title", default="")
    capture.add_argument("--body", default="")
    capture.add_argument("--cwd", default="")
    capture.add_argument("--session-id", default="unknown")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "briefing":
        return _run_lifecycle("briefing", build_briefing, args.cwd, args.session_id)
    if args.command == "recall":
        return _run_lifecycle("recall", build_recall, args.prompt, args.cwd, args.session_id)
    if args.command == "tool-context":
        return _run_lifecycle(
            "tool-context",
            build_tool_context,
            args.tool_name,
            args.file_path,
            args.cwd,
            args.session_id,
        )
    if args.command == "status":
        return _run_json("status", _status, args.cwd)
    if args.command == "search":
        return _run_json("search", _search, args.query, args.limit, args.cwd)
    if args.command == "get":
        return _run_json("get", _get, args.path)
    if args.command == "capture":
        return _run_json("capture", _capture, args.title, args.body, args.cwd, args.session_id)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
