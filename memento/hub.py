"""Project hub regeneration and the two-tier vault map (MEM-160).

``projects/<slug>.md`` hubs used to grow by free-text append
(:func:`memento.store.append_project_session_line`, formerly called from
:func:`memento.store.update_project_index` on every MCP store/replace/capture)
with no cap and no structural guarantee. Left running long enough, a hub turns
into exactly what MEM-160 found in the real vault: a 300+ line file with
duplicate ``## Sessions`` headers, truncated entries, and stray agent-output
fragments -- nothing curated it, and nothing navigated from it.

This module replaces that append-only growth with a mechanical, idempotent
*regeneration*: :func:`regenerate_project_hub` rebuilds ``projects/<slug>.md``
from scratch on every call, using only frontmatter and the wikilink graph --
it never reads or parses the previous hub file, so whatever corruption
accumulated there is discarded outright rather than patched around.
:func:`vault_map` layers a small cross-project section on top for briefing
injection, capped the same way.

Both outputs use a plain-text one-liner-per-note format (title only, no
snippets) so the whole hub or vault map stays well under its byte cap even at
a few hundred notes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from memento.config import get_config
from memento.graph import extract_wikilinks, load_or_build_graph
from memento.store import _frontmatter_int, _frontmatter_scalar, _write_text_atomic, split_frontmatter

HUB_MAX_BYTES_DEFAULT = 25_000
VAULT_MAP_MAX_BYTES_DEFAULT = 25_000

TOP_NOTES_LIMIT = 10
RECENT_DECISIONS_LIMIT = 10
RECENT_DECISIONS_WINDOW_DAYS = 30
RECENT_ACTIVITY_LIMIT = 10
CROSS_PROJECT_TOP_NOTES_LIMIT = 10

# Fixed, always-present section order -- regeneration never omits a section,
# it only ever renders an explicit "nothing here" placeholder or an overflow
# count. This is what makes truncation never silent (contract MEM-160 #2).
HUB_SECTION_HEADINGS = ("## Top notes", "## Recent decisions", "## Recent activity", "## Overflow")


def _search_hint(project_slug: str) -> str:
    return f"use memento_search --project {project_slug}"


_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse_date(raw):
    """Parse a frontmatter ``date`` scalar's leading ``YYYY-MM-DD`` into a UTC datetime, or None."""
    if not raw:
        return None
    match = _DATE_PREFIX_RE.match(str(raw).strip())
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _date_sort_key(meta):
    return _parse_date(meta.get("date")) or datetime.min.replace(tzinfo=timezone.utc)


def _within_window(meta, now, window_days):
    dt = _parse_date(meta.get("date"))
    if dt is None:
        return False
    age_days = (now - dt).total_seconds() / 86400
    return 0 <= age_days <= window_days


def _read_note(path: Path):
    """Read the frontmatter fields and body wikilinks hub regeneration needs from one note.

    Deliberately independent of :func:`memento.graph.read_note_metadata` /
    :func:`memento.config.get_vault` so this module can be pointed at any
    vault path (a temp vault in tests, the real vault in production) without
    monkeypatching global config -- it takes ``path`` directly and reuses
    only the vault-path-agnostic parsing helpers (:func:`memento.store.split_frontmatter`,
    :func:`memento.graph.extract_wikilinks`).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    frontmatter, body = split_frontmatter(text)
    return {
        "stem": path.stem,
        "title": _frontmatter_scalar(frontmatter, "title") or path.stem,
        "date": _frontmatter_scalar(frontmatter, "date"),
        "type": _frontmatter_scalar(frontmatter, "type"),
        "project": _frontmatter_scalar(frontmatter, "project"),
        "certainty": _frontmatter_int(frontmatter, "certainty"),
        "links": extract_wikilinks(body),
    }


def _scan_notes(vault: Path) -> dict:
    """Read every ``notes/*.md`` file once. Returns ``{stem: metadata}``."""
    notes_dir = Path(vault) / "notes"
    if not notes_dir.is_dir():
        return {}
    notes = {}
    for md in sorted(notes_dir.glob("*.md")):
        meta = _read_note(md)
        if meta is not None:
            notes[meta["stem"]] = meta
    return notes


def _inbound_link_counts(all_notes: dict) -> dict:
    """Plain-text inbound wikilink count per stem -- the networkx-free fallback ranking.

    Counts only links between notes that both exist in ``all_notes`` (mirrors
    :func:`memento.graph.build_wikilink_graph`'s "edges only where the target
    exists" rule), so a dangling ``[[typo]]`` link never inflates a score.
    """
    counts = {stem: 0 for stem in all_notes}
    for stem, meta in all_notes.items():
        for target in meta.get("links", []):
            if target in counts and target != stem:
                counts[target] += 1
    return counts


def _rank_scores(vault: Path, all_notes: dict) -> dict:
    """Return ``{stem: score}`` centrality for every note in ``all_notes``.

    Prefers PageRank from the shared wikilink graph (:func:`memento.graph.load_or_build_graph`,
    reused as-is); degrades to the plain inbound-link-count fallback above
    when networkx is unavailable (empty graph/pagerank) so hub regeneration
    never hard-depends on the optional dependency.
    """
    pagerank = {}
    try:
        _graph, pagerank = load_or_build_graph(vault_path=str(vault))
    except Exception:
        pagerank = {}

    if pagerank:
        return {stem: float(pagerank.get(stem, 0.0)) for stem in all_notes}

    return {stem: float(count) for stem, count in _inbound_link_counts(all_notes).items()}


def _note_line(meta) -> str:
    return f"- [[{meta['stem']}]] {meta.get('title') or meta['stem']}"


def _render_hub(project_slug, note_count, generated_at, top_notes, decisions, activity, overflow, max_bytes):
    """Render the fixed hub schema, trimming lowest-priority sections first to fit ``max_bytes``.

    Trim order (lowest priority first): Recent activity, then Recent
    decisions, then Top notes -- Recent activity is the bounded replacement
    for the old unbounded ``## Sessions`` append, so it is the cheapest to
    shed first. Every trim is recorded in ``overflow`` so the rendered
    ``## Overflow`` section always states an explicit count; nothing is ever
    silently dropped.

    Returns ``(content, shown, overflow, trimmed_sections)`` -- ``shown`` is a
    ``{"top_notes": [...], "recent_decisions": [...], "recent_activity": [...]}``
    dict of the post-trim note lists actually rendered, ``overflow`` is the
    final (post-trim) count dict, and ``trimmed_sections`` lists which section
    a trim came from, in trim order.
    """
    top_notes = list(top_notes)
    decisions = list(decisions)
    activity = list(activity)
    overflow = dict(overflow)
    trimmed = []

    def build():
        lines = [
            "---",
            f"title: {project_slug}",
            f"project: {project_slug}",
            "---",
            "",
            f"# {project_slug}",
            "",
            f"{note_count} notes | generated {generated_at}",
            "",
            "## Top notes",
            "",
        ]
        if top_notes:
            lines.extend(_note_line(m) for m in top_notes)
        else:
            lines.append("_(no notes yet)_")
        lines.append("")
        lines.append("## Recent decisions")
        lines.append("")
        if decisions:
            lines.extend(_note_line(m) for m in decisions)
        else:
            lines.append(f"_(none in the last {RECENT_DECISIONS_WINDOW_DAYS} days)_")
        lines.append("")
        lines.append("## Recent activity")
        lines.append("")
        if activity:
            lines.extend(_note_line(m) for m in activity)
        else:
            lines.append("_(no activity yet)_")
        lines.append("")
        lines.append("## Overflow")
        lines.append("")
        overflow_lines = []
        if overflow["top_notes"]:
            overflow_lines.append(
                f"- {overflow['top_notes']} more notes not shown in Top notes; {_search_hint(project_slug)}"
            )
        if overflow["recent_decisions"]:
            overflow_lines.append(
                f"- {overflow['recent_decisions']} more decisions not shown; {_search_hint(project_slug)}"
            )
        if overflow["recent_activity"]:
            overflow_lines.append(
                f"- {overflow['recent_activity']} more notes not shown in Recent activity; {_search_hint(project_slug)}"
            )
        lines.extend(overflow_lines if overflow_lines else ["_(nothing omitted)_"])
        lines.append("")
        return "\n".join(lines)

    content = build()
    while len(content.encode("utf-8")) > max_bytes and (activity or decisions or top_notes):
        if activity:
            activity.pop()
            overflow["recent_activity"] += 1
            trimmed.append("recent_activity")
        elif decisions:
            decisions.pop()
            overflow["recent_decisions"] += 1
            trimmed.append("recent_decisions")
        else:
            top_notes.pop()
            overflow["top_notes"] += 1
            trimmed.append("top_notes")
        content = build()

    shown = {"top_notes": top_notes, "recent_decisions": decisions, "recent_activity": activity}
    return content, shown, overflow, trimmed


def regenerate_project_hub(vault, project_slug, config=None, *, now=None) -> dict:
    """Rebuild ``projects/<project_slug>.md`` from scratch. Idempotent -- never appends.

    Reads only ``notes/*.md`` frontmatter and the wikilink graph; the existing
    hub file's content (however corrupted) is never parsed, only overwritten.
    Calling this twice with the same vault state and the same ``now`` produces
    byte-identical output.

    Section schema (fixed order, always present):

    - ``# <project>`` header with note count + generated-at timestamp.
    - ``## Top notes``: up to :data:`TOP_NOTES_LIMIT` notes ranked by
      PageRank (or inbound-link count without networkx).
    - ``## Recent decisions``: ``type: decision`` notes dated within
      :data:`RECENT_DECISIONS_WINDOW_DAYS` days, newest first, up to
      :data:`RECENT_DECISIONS_LIMIT`.
    - ``## Recent activity``: the most recently dated notes, up to
      :data:`RECENT_ACTIVITY_LIMIT` -- the bounded replacement for the old
      unbounded ``## Sessions``/``## Activity log`` append.
    - ``## Overflow``: explicit "N notes not shown; use memento_search
      --project <slug>" counts. Never a silent truncation.

    The whole file is capped at ``hub_max_bytes`` (config, default
    :data:`HUB_MAX_BYTES_DEFAULT`); sections are trimmed in reverse priority
    order (activity first) to fit, and every trim is folded into the
    ``## Overflow`` counts above.

    Returns a report dict: ``{path, project, note_count, generated_at, bytes,
    top_notes, recent_decisions, recent_activity, overflow, trimmed_for_size}``
    (the list fields are note stems; ``overflow``/``trimmed_for_size`` mirror
    the rendered Overflow section).
    """
    vault = Path(vault)
    if config is None:
        config = get_config()
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    max_bytes = int(config.get("hub_max_bytes", HUB_MAX_BYTES_DEFAULT))

    all_notes = _scan_notes(vault)
    project_notes = {stem: meta for stem, meta in all_notes.items() if meta.get("project") == project_slug}
    note_count = len(project_notes)

    scores = _rank_scores(vault, all_notes)

    ranked = sorted(project_notes.values(), key=lambda meta: (-scores.get(meta["stem"], 0.0), meta["stem"]))
    decisions = sorted(
        (
            meta
            for meta in project_notes.values()
            if meta.get("type") == "decision" and _within_window(meta, now, RECENT_DECISIONS_WINDOW_DAYS)
        ),
        key=_date_sort_key,
        reverse=True,
    )
    activity = sorted(project_notes.values(), key=_date_sort_key, reverse=True)

    top_shown = ranked[:TOP_NOTES_LIMIT]
    decisions_shown = decisions[:RECENT_DECISIONS_LIMIT]
    activity_shown = activity[:RECENT_ACTIVITY_LIMIT]

    overflow = {
        "top_notes": len(ranked) - len(top_shown),
        "recent_decisions": len(decisions) - len(decisions_shown),
        "recent_activity": len(activity) - len(activity_shown),
    }

    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    content, shown, final_overflow, trimmed = _render_hub(
        project_slug, note_count, generated_at, top_shown, decisions_shown, activity_shown, overflow, max_bytes
    )

    project_dir = vault / "projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir / f"{project_slug}.md"
    _write_text_atomic(target, content)

    return {
        "path": f"projects/{project_slug}.md",
        "project": project_slug,
        "note_count": note_count,
        "generated_at": generated_at,
        "bytes": len(content.encode("utf-8")),
        "top_notes": [m["stem"] for m in shown["top_notes"]],
        "recent_decisions": [m["stem"] for m in shown["recent_decisions"]],
        "recent_activity": [m["stem"] for m in shown["recent_activity"]],
        "overflow": final_overflow,
        "trimmed_for_size": trimmed,
    }


def regenerate_stale_hubs(vault, config=None, *, now=None) -> dict:
    """Regenerate hubs for every project with a note newer than its hub file.

    Gated by ``hub_regeneration_enabled`` (config, default ``False``): a
    no-op that returns immediately when disabled, matching the
    ``archive_sweep_enabled``/``fleeting_lifecycle_enabled`` convention in
    :mod:`memento.archive`. Intended to run from ``hooks/memento-sweeper.py``'s
    periodic sweep, after the existing fold/archive/fleeting sweeps.

    Returns a report dict: ``{enabled, regenerated, skipped}`` -- ``regenerated``
    and ``skipped`` are lists of project slugs (``skipped`` entries note the
    reason: already current, or ``"<slug> (error: ...)"`` on a regeneration
    failure for that one project, which never blocks the others).
    """
    vault = Path(vault)
    if config is None:
        config = get_config()

    enabled = bool(config.get("hub_regeneration_enabled", False))
    report = {"enabled": enabled, "regenerated": [], "skipped": []}
    if not enabled:
        return report

    notes_dir = vault / "notes"
    if not notes_dir.is_dir():
        return report

    newest_mtime_by_project = {}
    for md in notes_dir.glob("*.md"):
        meta = _read_note(md)
        if not meta or not meta.get("project"):
            continue
        try:
            mtime = md.stat().st_mtime
        except OSError:
            continue
        slug = meta["project"]
        if mtime > newest_mtime_by_project.get(slug, 0.0):
            newest_mtime_by_project[slug] = mtime

    for slug, newest_mtime in newest_mtime_by_project.items():
        hub_path = vault / "projects" / f"{slug}.md"
        try:
            hub_mtime = hub_path.stat().st_mtime if hub_path.exists() else 0.0
        except OSError:
            hub_mtime = 0.0

        if hub_mtime >= newest_mtime:
            report["skipped"].append(slug)
            continue

        try:
            regenerate_project_hub(vault, slug, config=config, now=now)
            report["regenerated"].append(slug)
        except Exception as exc:
            report["skipped"].append(f"{slug} (error: {exc})")

    return report


def vault_map(vault, project_slug, config=None) -> str:
    """Build the capped two-tier vault map: this project's hub plus cross-project top notes.

    Tier 1 is the regenerated project hub (always freshly rebuilt via
    :func:`regenerate_project_hub`, so the map is never built from stale or
    corrupted disk content). Tier 2 is up to :data:`CROSS_PROJECT_TOP_NOTES_LIMIT`
    of the highest-centrality notes from *other* projects, giving cross-project
    awareness without pulling in full note bodies -- everything else stays
    read-on-demand via search/get.

    Capped at ``vault_map_max_bytes`` (config, default
    :data:`VAULT_MAP_MAX_BYTES_DEFAULT`): the cross-project section is trimmed
    first, and a hard byte-truncation is the last-resort safety net if the
    hub alone is already at the cap.
    """
    vault = Path(vault)
    if config is None:
        config = get_config()
    max_bytes = int(config.get("vault_map_max_bytes", VAULT_MAP_MAX_BYTES_DEFAULT))

    regenerate_project_hub(vault, project_slug, config=config)
    hub_path = vault / "projects" / f"{project_slug}.md"
    try:
        hub_text = hub_path.read_text(encoding="utf-8").rstrip()
    except OSError:
        hub_text = f"# {project_slug}\n\n0 notes"

    all_notes = _scan_notes(vault)
    scores = _rank_scores(vault, all_notes)
    cross = sorted(
        (meta for meta in all_notes.values() if meta.get("project") and meta.get("project") != project_slug),
        key=lambda meta: (-scores.get(meta["stem"], 0.0), meta["stem"]),
    )
    cross_lines = [f"{_note_line(m)} (project: {m['project']})" for m in cross[:CROSS_PROJECT_TOP_NOTES_LIMIT]]

    def build(lines):
        parts = [hub_text, "", "## Cross-project top notes", ""]
        parts.extend(lines if lines else ["_(none)_"])
        return "\n".join(parts) + "\n"

    text = build(cross_lines)
    while len(text.encode("utf-8")) > max_bytes and cross_lines:
        cross_lines.pop()
        text = build(cross_lines)

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")

    return text
