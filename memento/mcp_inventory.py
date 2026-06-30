"""Generated MCP tool inventory helpers.

The inventory in this module is the docs-facing source of truth for the
Memento MCP server tool table. Tests compare it against the actual
``@mcp.tool`` registrations in :mod:`memento.mcp_server` so docs drift is
caught when tools are added or removed.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

START_MARKER = "<!-- memento-mcp-tools:start -->"
END_MARKER = "<!-- memento-mcp-tools:end -->"


@dataclass(frozen=True)
class McpToolInventoryItem:
    """Docs-facing metadata for one registered MCP tool."""

    name: str
    category: str
    summary: str
    when_to_use: str


MCP_TOOL_INVENTORY: tuple[McpToolInventoryItem, ...] = (
    McpToolInventoryItem(
        "memento_search",
        "Read",
        "Search vault notes with BM25, optional semantic search, hybrid ranking, temporal decay, PageRank/access-log boosts, and `concrete: auto|true|false` for exact identifiers and quoted phrases.",
        "Use before answering questions about past decisions, prior fixes, project history, session context, recurring patterns, or exact identifiers. Do not use it to read a known note path.",
    ),
    McpToolInventoryItem(
        "memento_query",
        "Read",
        "Run typed metadata filters, counts, date buckets, and recent-session listings over note frontmatter without reading full note bodies.",
        "Use for count/list/filter questions by project, type, tag, certainty, source, date, branch, or session_id; use `memento_search` instead for topical recall or semantic retrieval.",
    ),
    McpToolInventoryItem(
        "memento_contradictions",
        "Read",
        "Inspect a topic for disagreements, stale conclusions, supersession chains, and opposite-language hints.",
        "Use when comparing competing notes about the same topic or when you need explicit superseded notes marked alongside their source paths and certainty/date context.",
    ),
    McpToolInventoryItem(
        "memento_briefing",
        "Lifecycle",
        "Build a compact session-start briefing payload for host adapters.",
        "Host-adapter primitive for automatic injection; not a general user-answering tool.",
    ),
    McpToolInventoryItem(
        "memento_recall",
        "Lifecycle",
        "Build prompt-time recall context for host adapters.",
        "Host-adapter primitive for automatic injection before an agent turn; not a general user-answering tool.",
    ),
    McpToolInventoryItem(
        "memento_tool_context",
        "Lifecycle",
        "Build read-tool context for a concrete file path.",
        "Host-adapter primitive for automatic read-tool injection; not for explicit recall/search requests.",
    ),
    McpToolInventoryItem(
        "memento_session_context",
        "Lifecycle",
        "Build a one-call budgeted session context packet that can include status, recent context, recall, and tool-context preview metadata.",
        "Host-adapter primitive that replaces separate briefing/recall/status calls when a host wants one compact payload.",
    ),
    McpToolInventoryItem(
        "memento_get",
        "Read",
        "Read full note content by name or path.",
        "Use after `memento_search` when a returned path needs full content, or directly when the user already supplied an exact note path/name.",
    ),
    McpToolInventoryItem(
        "memento_status",
        "Operational",
        "Report vault health/status, note counts, project counts, and safe config summary.",
        "Use for operational checks and setup debugging; not for recall, project history, or note content.",
    ),
    McpToolInventoryItem(
        "memento_list",
        "Sync",
        "List notes with optional content hashes for lightweight inventory/sync.",
        "Use for sync/inventory clients; not for topical recall or reading notes.",
    ),
    McpToolInventoryItem(
        "memento_store",
        "Write",
        "Write a single knowledge note with managed frontmatter and project indexing.",
        "Low-level write primitive for sync/automation or agents without skill support; interactive Claude/Codex sessions should prefer `/memento` or local hooks.",
    ),
    McpToolInventoryItem(
        "memento_store_smart",
        "Write",
        "Search for close matches before writing, and return duplicate/update/supersede suggestions.",
        "Use when you want a write decision with candidate paths/reasons before creating a note; it avoids obvious duplicates by default.",
    ),
    McpToolInventoryItem(
        "memento_daily_snapshot",
        "Write",
        "Write a deterministic `notes/daily-<date>-<repo-slug>.md` snapshot with an append-only supersede chain.",
        "Low-level structured daily-snapshot primitive for path-controlled integrations; do not use for ordinary notes, interactive memory capture, or session triage.",
    ),
    McpToolInventoryItem(
        "memento_capture",
        "Write",
        "End-of-session triage from a transcript path or structured summary; writes fleeting session state and optionally an atomic note.",
        "Low-level SessionEnd equivalent for agents without hook support; not a replacement for interactive `/memento` workflows.",
    ),
    McpToolInventoryItem(
        "memento_preserve",
        "Write",
        "Archive a file or directory bundle under `archive/<slug>/` with a manifest, a lightweight index note, and optional project-linking.",
        "Use for evidence packets, screenshots, handoff bundles, or other artifacts that should stay intact; copy by default, move only when explicitly requested. Do not use it for ordinary knowledge capture or atomic notes.",
    ),
    McpToolInventoryItem(
        "memento_reindex",
        "Maintenance",
        "Rebuild the search index from all markdown files after out-of-band changes.",
        "Use after bulk adds, git pull, Obsidian sync, or stale-index evidence; not as the default response to a broad/empty search miss.",
    ),
)


def inventory_tool_names() -> list[str]:
    """Return inventory tool names in docs order."""

    return [item.name for item in MCP_TOOL_INVENTORY]


def registered_tool_names(source_path: Path | None = None) -> list[str]:
    """Return names of functions decorated with ``@mcp.tool()``.

    This intentionally uses static AST parsing instead of importing
    ``memento.mcp_server`` so the drift check stays cheap and side-effect-free.
    """

    if source_path is None:
        source_path = Path(__file__).with_name("mcp_server.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if _is_mcp_tool_decorator(decorator):
                names.append(node.name)
                break
    return names


def _is_mcp_tool_decorator(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "tool"
        and isinstance(func.value, ast.Name)
        and func.value.id == "mcp"
    )


def render_mcp_tool_markdown() -> str:
    """Render the generated README table for registered MCP tools."""

    lines = [
        START_MARKER,
        f"The MCP server currently registers **{len(MCP_TOOL_INVENTORY)} tools**. This table is generated from `memento.mcp_inventory.MCP_TOOL_INVENTORY`; refresh/check it with `memento-vault tools --markdown` or `memento-vault tools --check`.",
        "",
        "| Tool | Category | What it does | When to use it |",
        "|------|----------|--------------|----------------|",
    ]
    for item in MCP_TOOL_INVENTORY:
        lines.append(
            "| "
            f"`{item.name}` | "
            f"{_markdown_cell(item.category)} | "
            f"{_markdown_cell(item.summary)} | "
            f"{_markdown_cell(item.when_to_use)} |"
        )
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_markdown_block(markdown: str) -> str:
    """Replace the generated MCP inventory block in markdown content."""

    start = markdown.find(START_MARKER)
    end = markdown.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError("MCP tool inventory markers not found")
    end += len(END_MARKER)
    return f"{markdown[:start]}{render_mcp_tool_markdown()}{markdown[end:]}"


def _markdown_cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render or check Memento MCP tool inventory docs.")
    parser.add_argument("--markdown", action="store_true", help="print the generated Markdown inventory block")
    parser.add_argument("--check", action="store_true", help="fail if inventory names differ from registered MCP tools")
    args = parser.parse_args(argv)

    if args.check:
        inventory = inventory_tool_names()
        registered = registered_tool_names()
        inventory_counts = Counter(inventory)
        registered_counts = Counter(registered)
        if inventory_counts != registered_counts:
            print("MCP tool inventory drift detected")
            print(f"inventory:  {inventory}")
            print(f"registered: {registered}")
            print(f"missing from inventory: {sorted((registered_counts - inventory_counts).elements())}")
            print(f"extra in inventory:     {sorted((inventory_counts - registered_counts).elements())}")
            return 1
        print(f"MCP tool inventory covers {len(inventory)} registered tools.")
        return 0

    # Default to Markdown to keep `memento-vault tools` useful in scripts.
    print(render_mcp_tool_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
