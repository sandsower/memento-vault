"""Capture-time dedup/merge via embedding similarity.

``find_merge_target`` checks if an incoming note is a near-duplicate of an
existing note (vector similarity above threshold).  ``merge_into_canonical``
appends the incoming content into the canonical note, unions frontmatter
(tags, certainty), and preserves the access-log signal on the canonical path.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memento.config import get_config, get_vault, slugify
from memento.store import _render_note_markdown, load_access_log_stats, write_access_log_stats

logger = logging.getLogger(__name__)

# Sentinel section marker used in merged notes.
_MERGED_SECTION_HEADER = "## Merged from"


@dataclass
class MergeTarget:
    """A canonical note that an incoming note should be merged into."""

    path: str  # vault-relative path like "notes/redis-cache-guidance.md"
    similarity: float
    title: str
    contract: dict[str, Any] = field(default_factory=dict)


def _get_backend():
    """Get the active search backend (lazy, with circular-import guard)."""
    from memento.search_backend import get_backend

    return get_backend()


def _index_note(rel_path: str) -> None:
    """Re-index a note in the search backend (best-effort)."""
    try:
        from memento.search_backend import get_backend as _get_b

        backend = _get_b()
        if hasattr(backend, "index_note"):
            backend.index_note(rel_path)
    except Exception:
        logger.debug("Could not re-index %s", rel_path, exc_info=True)


def _update_project_index(vault: Path, project_slug: str | None, note_stem: str, summary: str) -> None:
    """Update the project index file (best-effort)."""
    if not project_slug:
        return
    try:
        from memento.store import update_project_index

        update_project_index(vault, project_slug, note_stem, summary)
    except Exception:
        logger.debug("Could not update project index for %s", note_stem, exc_info=True)


def _is_embedding_backend(backend) -> bool:
    """Check whether *backend* is a vector-capable embedded search backend."""
    name = type(backend).__name__
    if name == "EmbeddedSearchBackend":
        return getattr(backend, "_vec_available", False)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_merge_target(
    title: str,
    body: str,
    *,
    tags: list[str] | None = None,
    threshold: float | None = None,
) -> MergeTarget | None:
    """Search the vault for a near-duplicate note using embedding similarity.

    Returns a ``MergeTarget`` when the best match's vector-similarity score
    is at or above *threshold*.  Returns ``None`` when:
    - no vector-capable backend is available
    - every candidate scores below the threshold
    - the backend search fails

    Callers should fall through to existing token-overlap logic when this
    returns ``None``.
    """
    backend = _get_backend()
    if not _is_embedding_backend(backend):
        return None

    if threshold is None:
        config = get_config()
        threshold = float(config.get("dedup_embed_threshold", 0.86))

    query = f"{title}\n{body[:2000]}"

    try:
        results = backend.search(
            query,
            collection="memento",
            limit=1,
            semantic=True,
            timeout=10,
            min_score=threshold,
            concrete=False,
        )
    except Exception:
        logger.debug("Embedding search in dedup_merge failed", exc_info=True)
        return None

    if not results:
        return None

    best = results[0]
    score = float(best.get("score", 0.0))
    if score < threshold:
        return None

    # Title-token overlap gate (disabled when threshold is 0)
    config = get_config()
    min_title_overlap = int(config.get("dedup_embed_min_title_overlap", 0))
    if min_title_overlap > 0:
        candidate_title = str(best.get("title", ""))
        cand_tokens = set(re.findall(r"[a-z0-9]+", candidate_title.lower()))
        inc_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
        if cand_tokens and inc_tokens:
            overlap = len(cand_tokens & inc_tokens) / max(len(inc_tokens), 1)
            if overlap < min_title_overlap:
                logger.debug("Title overlap below threshold: %.2f < %d", overlap, min_title_overlap)
                return None

    # Load canonical note's contract for the MergeTarget
    vault = get_vault()
    candidate_path = str(best.get("path", ""))
    contract: dict[str, Any] = {}

    if candidate_path:
        try:
            raw_text = (vault / candidate_path).read_text(encoding="utf-8", errors="replace")
            parts = raw_text.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                # Extract key contract fields from frontmatter
                contract = {
                    "title": _fm_value(frontmatter, "title") or "",
                    "note_type": _fm_value(frontmatter, "type") or "discovery",
                    "tags": _fm_tags(frontmatter),
                    "certainty": _fm_int(frontmatter, "certainty"),
                    "supersedes": _fm_value(frontmatter, "supersedes"),
                    "project": _fm_value(frontmatter, "project"),
                    "branch": _fm_value(frontmatter, "branch"),
                    "session_id": _fm_value(frontmatter, "session_id"),
                }
        except Exception:
            logger.debug("Could not read candidate note %s for contract", candidate_path, exc_info=True)

    return MergeTarget(
        path=candidate_path,
        similarity=score,
        title=str(best.get("title", "")),
        contract=contract,
    )


def merge_into_canonical(
    vault: Path,
    canonical_path: str,
    title: str,
    body: str,
    *,
    note_type: str = "discovery",
    tags: list[str] | None = None,
    certainty: int | None = None,
    source: str = "mcp",
    origin: str | None = None,
    validity_context: str | None = None,
    supersedes: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Merge *title*/*body* into an existing canonical note.

    Reads the canonical note, appends the incoming content under a labelled
    ``## Merged from`` section, unions tags, takes the higher certainty,
    writes the result atomically, re-indexes the canonical, and re-keys any
    access-log events from an orphaned old path to the canonical stem.

    Returns a dict with ``canonical_path``, ``merged`` (bool), and ``reason``.
    ``merged=False`` with ``reason="already_merged"`` is the idempotent no-op
    response (same incoming payload already present).
    """
    tags = tags or []

    # --- Parse canonical note ---
    canonical_abs = vault / canonical_path
    try:
        raw_text = canonical_abs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"canonical_path": canonical_path, "merged": False, "reason": "canonical_not_found"}

    parts = raw_text.split("---", 2)
    if len(parts) < 3:
        return {"canonical_path": canonical_path, "merged": False, "reason": "malformed_frontmatter"}

    frontmatter_raw = parts[1]
    canonical_body = parts[2]

    # --- Idempotency check: same incoming body already merged? ---
    if _already_merged(canonical_body, title, body):
        return {"canonical_path": canonical_path, "merged": False, "reason": "already_merged"}

    # --- Extract canonical contract fields ---
    canonical_title = _fm_value(frontmatter_raw, "title") or canonical_abs.stem
    canonical_type = _fm_value(frontmatter_raw, "type") or "discovery"
    canonical_tags = _fm_tags(frontmatter_raw) or []
    canonical_certainty = _fm_int(frontmatter_raw, "certainty")
    canonical_supersedes = _fm_value(frontmatter_raw, "supersedes")
    canonical_project = _fm_value(frontmatter_raw, "project")
    canonical_branch = _fm_value(frontmatter_raw, "branch")
    canonical_session = _fm_value(frontmatter_raw, "session_id")

    # --- Union tags ---
    merged_tags = list(dict.fromkeys(canonical_tags + list(tags)))
    merged_tags.sort()

    # --- Max certainty ---
    merged_certainty = max(
        (c for c in (canonical_certainty, certainty) if c is not None),
        default=None,
    )

    # --- Build merged body ---
    merged_body = canonical_body.rstrip() + "\n\n---\n\n"
    merged_body += f"{_MERGED_SECTION_HEADER} {title}\n\n"
    merged_body += body
    merged_body += "\n"

    # --- Render the updated note ---
    merged_markdown = _render_note_markdown(
        canonical_title,
        merged_body,
        note_type=canonical_type or note_type,
        tags=merged_tags,
        certainty=merged_certainty,
        source=source,
        origin=origin,
        validity_context=validity_context or None,
        supersedes=canonical_supersedes or supersedes or None,
        project=canonical_project or project or None,
        branch=canonical_branch or branch or None,
        session_id=canonical_session or session_id or None,
    )

    # --- Atomic write ---
    slug = slugify(canonical_title)
    tmp = canonical_abs.with_name(f".tmp-{slug}.md")
    try:
        tmp.write_text(merged_markdown)
        os.replace(tmp, canonical_abs)
    except OSError as exc:
        return {"canonical_path": canonical_path, "merged": False, "reason": f"write_error: {exc}"}

    # --- Re-index the canonical note ---
    _index_note(canonical_path)

    # --- Update project index ---
    if project or canonical_project:
        slug_val = project or canonical_project or ""
        project_slug = slugify(Path(slug_val).name)
        if project_slug:
            _update_project_index(
                vault,
                project_slug,
                canonical_abs.stem,
                f"Merged: {title[:80]}",
            )

    # --- Re-key access log (if paths changed) ---
    _maybe_rekey_access_log(canonical_path, canonical_path)

    return {
        "canonical_path": canonical_path,
        "merged": True,
        "reason": "ok",
        "canonical_title": canonical_title,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _already_merged(canonical_body: str, incoming_title: str, incoming_body: str) -> bool:
    """Return True if *incoming_body* already appears under a merged section."""
    # Fast path: check for the exact header string + title
    header = f"{_MERGED_SECTION_HEADER} {incoming_title}"
    if header not in canonical_body:
        return False

    # For a more robust check, see if the incoming body text is present
    # anywhere in the canonical body after any merged section.
    stripped = incoming_body.strip()
    if not stripped:
        return False

    # Check if the incoming body appears verbatim after the header
    sections = canonical_body.split(header)
    if len(sections) > 1:
        # The section after the header contains this merge's text
        if stripped in sections[1]:
            return True

    return False


def _parse_frontmatter_lines(frontmatter: str) -> dict[str, str]:
    """Parse key:value lines from a raw frontmatter block."""
    result = {}
    for line in frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(\w[\w-]*):\s*(.*)", line)
        if match:
            result[match.group(1)] = match.group(2).strip()
    return result


def _fm_value(frontmatter: str, key: str) -> str | None:
    """Extract a single-line frontmatter value."""
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", frontmatter, re.MULTILINE)
    if not match:
        return None
    val = match.group(1).strip()
    # Strip quotes
    if val.startswith('"') and val.endswith('"'):
        val = val[1:-1]
    elif val.startswith("'") and val.endswith("'"):
        val = val[1:-1]
    return val if val else None


def _fm_int(frontmatter: str, key: str) -> int | None:
    """Extract an integer frontmatter value."""
    val = _fm_value(frontmatter, key)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _fm_tags(frontmatter: str) -> list[str]:
    """Extract tags list from frontmatter."""
    match = re.search(r"^tags:\s*\[([^\]]*)\]", frontmatter, re.MULTILINE)
    if not match:
        return []
    return [item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()]


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _maybe_rekey_access_log(old_path: str, canonical_path: str) -> None:
    """Re-key access-log stats from *old_path* to *canonical_path* if they differ.

    In the primary use case (preventing a new note from being created), the
    old and canonical paths are the same so this is a no-op.  When an actual
    supersede has happened (a note existed and was replaced by another stem),
    this moves those access-log events so the canonical retains the signal.
    """
    if old_path == canonical_path:
        return

    try:
        stats = load_access_log_stats()
    except Exception:
        logger.debug("Could not load access log stats for re-key", exc_info=True)
        return

    if old_path not in stats:
        return

    old_events = stats.pop(old_path, {"events": []}).get("events", [])
    if not old_events:
        return

    canonical_events = stats.get(canonical_path, {"events": []})
    merged_events = canonical_events.get("events", []) + old_events
    stats[canonical_path] = {"events": merged_events}

    try:
        write_access_log_stats(stats)
    except Exception:
        logger.debug("Could not write re-keyed access log stats", exc_info=True)
