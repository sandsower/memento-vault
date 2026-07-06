"""Smart-store candidate analysis for Memento writes."""

from __future__ import annotations

import re
from pathlib import Path

from memento.config import get_vault
from memento.contradictions import inspect_contradictions
from memento.dedup_merge import find_merge_target, merge_into_canonical
from memento.search import has_qmd, qmd_search_with_extras, resolve_concrete_mode
from memento.store import (
    acquire_vault_write_lock,
    find_dedup_candidates,
    normalize_note_contract,
    owns_vault_write_lock,
    release_vault_write_lock,
    split_frontmatter,
    update_project_index,
    write_note,
)
from memento.utils import sanitize_secrets

_CLOSE_MATCH_THRESHOLD = 0.45
_ALREADY_COVERED_THRESHOLD = 0.82
_SUPERSEDE_CUES = (
    "supersede",
    "replace",
    "replaces",
    "replaced",
    "instead",
    "now use",
    "no longer",
    "deprecated",
    "deprecate",
    "switch to",
    "migration",
    "updated guidance",
    "new guidance",
    "prefer",
)
_UPDATE_CUES = (
    "add",
    "additional",
    "clarification",
    "clarify",
    "update",
    "updated",
    "expand",
    "amend",
    "append",
    "follow-up",
    "more detail",
    "more context",
)


def _strip_related_placeholder(body: str) -> str:
    body = (body or "").strip()
    while body.endswith("## Related"):
        body = body[: -len("## Related")].rstrip()
    return body


def _split_note_text(text: str) -> tuple[str, str]:
    """Split a note into (frontmatter, body).

    Only a LEADING ``---`` block counts as frontmatter; ``---`` lines inside the
    body must never fabricate one or truncate the body (audit M6).
    """
    frontmatter, body = split_frontmatter(text or "")
    return frontmatter, _strip_related_placeholder(body)


def _frontmatter_value(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def _frontmatter_tags(frontmatter: str) -> list[str]:
    match = re.search(r"^tags:\s*\[([^\]]*)\]", frontmatter, re.MULTILINE)
    if not match:
        return []
    return [item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()]


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _contains_cue(text: str, cues: tuple[str, ...]) -> list[str]:
    lower = (text or "").lower()
    return [cue for cue in cues if cue in lower]


def _candidate_details(raw: dict, vault: Path, query_title: str, query_body: str, query_tags: list[str]) -> dict | None:
    rel_path = str(raw.get("path") or "").strip()
    if not rel_path:
        return None

    candidate_path = vault / rel_path
    try:
        raw_text = candidate_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw_text = str(raw.get("content") or "")

    frontmatter, candidate_body = _split_note_text(raw_text)
    candidate_title = _frontmatter_value(frontmatter, "title") or raw.get("title") or candidate_path.stem
    candidate_type = _frontmatter_value(frontmatter, "type") or ""
    candidate_tags = _frontmatter_tags(frontmatter)
    candidate_contract = {
        "note_type": candidate_type or None,
        "tags": candidate_tags,
        "certainty": _frontmatter_value(frontmatter, "certainty"),
        "source": _frontmatter_value(frontmatter, "source"),
        "origin": _frontmatter_value(frontmatter, "origin"),
        "validity_context": _frontmatter_value(frontmatter, "validity-context"),
        "supersedes": _frontmatter_value(frontmatter, "supersedes"),
        "project": _frontmatter_value(frontmatter, "project"),
        "branch": _frontmatter_value(frontmatter, "branch"),
        "session_id": _frontmatter_value(frontmatter, "session_id"),
    }

    title_tokens = _tokenize(candidate_title)
    body_tokens = _tokenize(candidate_body)
    tag_tokens = _tokenize(" ".join(candidate_tags))
    candidate_tokens = title_tokens | body_tokens | tag_tokens

    query_title_tokens = _tokenize(query_title)
    query_body_tokens = _tokenize(query_body)
    query_tag_tokens = _tokenize(" ".join(query_tags))

    title_ratio = len(title_tokens & query_title_tokens) / max(1, len(query_title_tokens) or len(title_tokens) or 1)
    body_ratio = len(body_tokens & query_body_tokens) / max(1, len(query_body_tokens) or len(body_tokens) or 1)
    tag_ratio = len(tag_tokens & query_tag_tokens) / max(1, len(query_tag_tokens) or len(tag_tokens) or 1)
    score = round((0.35 * title_ratio) + (0.5 * body_ratio) + (0.15 * tag_ratio), 4)

    reasons = []
    if title_tokens & query_title_tokens:
        reasons.append(f"{len(title_tokens & query_title_tokens)} title token(s) overlap")
    if body_tokens & query_body_tokens:
        reasons.append(f"{len(body_tokens & query_body_tokens)} body token(s) overlap")
    if tag_tokens & query_tag_tokens:
        reasons.append(f"{len(tag_tokens & query_tag_tokens)} tag token(s) overlap")
    if candidate_contract["supersedes"]:
        reasons.append(f"supersedes {candidate_contract['supersedes']}")

    return {
        "path": rel_path,
        "title": candidate_title,
        "score": score,
        "reasons": reasons or ["topic overlap"],
        "tokens": candidate_tokens,
        "body": candidate_body,
        "contract": candidate_contract,
        "status": None,
    }


def _matches_exact_payload(candidate: dict, *, title: str, body: str, contract: dict, explicit_origin: bool) -> bool:
    if candidate["title"] != title:
        return False
    if candidate["body"] != body:
        return False

    existing = candidate["contract"]
    comparisons = [
        (existing.get("note_type") or "discovery") == contract["note_type"],
        existing.get("tags") == contract["tags"],
    ]

    optional_fields = {
        "certainty": str(contract["certainty"]) if contract["certainty"] is not None else None,
        "source": contract["source"],
        "project": contract["project"],
        "branch": contract["branch"],
        "validity_context": contract["validity_context"],
        "supersedes": contract["supersedes"],
        "session_id": contract["session_id"],
        "origin": contract["origin"] if explicit_origin else None,
    }
    for key, expected in optional_fields.items():
        if expected is None:
            continue
        comparisons.append(existing.get(key) == expected)

    return all(comparisons)


def _combine_candidate_sources(vault: Path, title: str, body: str, tags: list[str], candidate_limit: int) -> list[dict]:
    search_text = f"{title}\n{body[:2000]}"
    candidate_paths: list[str] = []
    seen: set[str] = set()

    def add_path(path: str) -> None:
        normalized = str(path or "").strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidate_paths.append(normalized)

    if has_qmd():
        concrete_enabled, _ = resolve_concrete_mode("auto", search_text)
        results = qmd_search_with_extras(
            search_text,
            limit=max(10, candidate_limit * 2),
            semantic=False,
            timeout=10,
            min_score=0.0,
            concrete=concrete_enabled,
        )
        for result in results:
            add_path(result.get("path", ""))

    try:
        contradiction_payload = inspect_contradictions(search_text, limit=max(10, candidate_limit * 2), min_certainty=2)
    except Exception:
        contradiction_payload = {}

    for entry in contradiction_payload.get("results", []):
        add_path(entry.get("path", ""))
    for entry in contradiction_payload.get("supersession", []):
        add_path(entry.get("older_path", ""))
        add_path(entry.get("newer_path", ""))
    for group in contradiction_payload.get("groups", []):
        for path in group.get("note_paths", []) or []:
            add_path(path)

    if not candidate_paths:
        fallback_paths = find_dedup_candidates(vault, title, tags or list(_tokenize(title)), limit=candidate_limit * 2)
        for note_path in fallback_paths:
            try:
                add_path(str(note_path.relative_to(vault)))
            except ValueError:
                add_path(str(note_path))

    contradiction_status: dict[str, str] = {}
    for entry in contradiction_payload.get("results", []):
        path = str(entry.get("path") or "").strip()
        if path:
            contradiction_status[path] = str(entry.get("status") or "")
    for entry in contradiction_payload.get("supersession", []):
        if entry.get("older_path"):
            contradiction_status[str(entry["older_path"])] = "superseded"
        if entry.get("newer_path"):
            contradiction_status[str(entry["newer_path"])] = "superseding"

    rows: list[dict] = []
    for path in candidate_paths:
        candidate = _candidate_details({"path": path}, vault, title, body, tags)
        if candidate is None:
            continue
        candidate["status"] = contradiction_status.get(candidate["path"])
        rows.append(candidate)

    rows.sort(key=lambda item: (-item["score"], item["path"]))
    return rows[:candidate_limit]


def suggest_store_action(
    *,
    title: str,
    body: str,
    note_type: str = "discovery",
    tags: list[str] | None = None,
    certainty: int | None = None,
    project: str | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    validity_context: str | None = None,
    supersedes: str | None = None,
    origin: str | None = None,
    source: str = "mcp",
    candidate_limit: int = 5,
) -> dict:
    """Return a write decision for a smart-store request.

    The returned payload is intentionally write-free unless it says
    ``decision == "created"``. Callers should use the candidate paths/reasons
    to decide whether to append to an existing note or explicitly supersede it.
    """
    if not title or not str(title).strip():
        return {"error": "title is required"}
    if not body or not str(body).strip():
        return {"error": "body is required"}

    vault = get_vault()
    if not vault.exists():
        return {"error": f"Vault not found at {vault}"}

    title = title.strip()
    sanitized_body = sanitize_secrets(body).strip()
    tags = tags or []
    contract = normalize_note_contract(
        note_type=note_type,
        tags=tags,
        certainty=certainty,
        source=source,
        origin=origin or "mcp_store",
        validity_context=validity_context,
        supersedes=supersedes,
        project=project,
        branch=branch,
        session_id=session_id,
    )

    # Fast exact-duplicate check: if a note with the same title/body/contract
    # already exists, return already_covered without running the embed gate.
    candidates = _combine_candidate_sources(vault, title, sanitized_body, tags, candidate_limit)
    if not candidates:
        return {
            "decision": "created",
            "created": True,
            "path": None,
            "title": title,
            "candidates": [],
            "reason": "no close match found",
            "write": {
                "title": title,
                "body": sanitized_body,
                "note_type": contract["note_type"],
                "tags": contract["tags"],
                "certainty": contract["certainty"],
                "project": contract["project"],
                "project_path": contract["project_path"],
                "branch": contract["branch"],
                "session_id": contract["session_id"],
                "validity_context": contract["validity_context"],
                "supersedes": contract["supersedes"],
                "origin": contract["origin"],
            },
        }

    search_text = f"{title}\n{sanitized_body[:2000]}"
    search_tokens = _tokenize(search_text)
    supersede_cues = _contains_cue(search_text, _SUPERSEDE_CUES)
    update_cues = _contains_cue(search_text, _UPDATE_CUES)

    for candidate in candidates:
        if _matches_exact_payload(
            candidate,
            title=title,
            body=_strip_related_placeholder(sanitized_body),
            contract=contract,
            explicit_origin=origin is not None,
        ):
            return {
                "decision": "already_covered",
                "created": False,
                "path": candidate["path"],
                "title": title,
                "candidates": candidates,
                "reason": "exact duplicate already exists",
            }

    # Embed-match gate: check for near-duplicate via vector similarity.
    # Only runs after the exact-duplicate check above so exact matches
    # are never routed through the merge path.
    if candidate_limit > 0:
        try:
            merge_target = find_merge_target(title, sanitized_body, tags=tags, threshold=None)
        except Exception:
            merge_target = None
        if merge_target is not None:
            return {
                "decision": "merged_into",
                "created": False,
                "path": merge_target.path,
                "title": title,
                "candidates": [],
                "reason": (
                    f"embed match (similarity={merge_target.similarity:.3f})"
                    " suggests merging into existing note instead of creating a new one"
                ),
                "best_candidate": {
                    "path": merge_target.path,
                    "title": merge_target.title,
                    "score": merge_target.similarity,
                    "contract": merge_target.contract,
                    "reasons": [f"embed vector similarity {merge_target.similarity:.3f} meets threshold"],
                },
            }

    best = max(candidates, key=lambda item: (item["score"], len(item["reasons"]), item["path"]))
    best_unique_ratio = len(search_tokens - best["tokens"]) / max(1, len(search_tokens))
    best_reasons = list(best["reasons"])
    if best.get("status"):
        best_reasons.append(f"contradiction status: {best['status']}")
    if supersede_cues:
        best_reasons.append(f"supersede cue(s): {', '.join(supersede_cues)}")
    if update_cues:
        best_reasons.append(f"update cue(s): {', '.join(update_cues)}")

    if best["score"] >= _ALREADY_COVERED_THRESHOLD and best_unique_ratio < 0.2:
        return {
            "decision": "already_covered",
            "created": False,
            "path": best["path"],
            "title": title,
            "candidates": candidates,
            "reason": "close match already covers this note",
            "best_candidate": {**best, "reasons": best_reasons},
        }

    supersede_like = bool(supersede_cues) or (best.get("status") in {"superseded", "superseding"})
    if supersede_like and best["score"] >= _CLOSE_MATCH_THRESHOLD:
        return {
            "decision": "supersedes_suggested",
            "created": False,
            "path": best["path"],
            "title": title,
            "candidates": candidates,
            "reason": "new content looks like a replacement for an existing note",
            "best_candidate": {**best, "reasons": best_reasons},
        }

    if best["score"] >= _CLOSE_MATCH_THRESHOLD:
        return {
            "decision": "candidate_update",
            "created": False,
            "path": best["path"],
            "title": title,
            "candidates": candidates,
            "reason": "similar note exists; consider appending or revising it instead",
            "best_candidate": {**best, "reasons": best_reasons},
        }

    return {
        "decision": "created",
        "created": True,
        "path": None,
        "title": title,
        "candidates": candidates,
        "reason": "no close match found",
        "write": {
            "title": title,
            "body": sanitized_body,
            "note_type": contract["note_type"],
            "tags": contract["tags"],
            "certainty": contract["certainty"],
            "project": contract["project"],
            "project_path": contract["project_path"],
            "branch": contract["branch"],
            "session_id": contract["session_id"],
            "validity_context": contract["validity_context"],
            "supersedes": contract["supersedes"],
            "origin": contract["origin"],
        },
    }


def write_smart_store_note(
    *,
    title: str,
    body: str,
    note_type: str = "discovery",
    tags: list[str] | None = None,
    certainty: int | None = None,
    project: str | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    validity_context: str | None = None,
    supersedes: str | None = None,
    origin: str | None = None,
    source: str = "mcp",
    candidate_limit: int = 5,
) -> dict:
    """Smart-store helper that writes only when no close match exists.

    The dedup check and the write (create or append-merge) run atomically under
    the vault write lock so two concurrent same-payload writers cannot both pass
    the check and create duplicates (audit M6). Re-entrant: callers that already
    hold the lock (MCP server, automated run lessons) keep it — it is neither
    re-acquired nor released on their behalf.
    """
    already_held = owns_vault_write_lock()
    if not already_held and not acquire_vault_write_lock():
        return {"error": "Could not acquire vault write lock (another write in progress)"}

    try:
        decision = suggest_store_action(
            title=title,
            body=body,
            note_type=note_type,
            tags=tags,
            certainty=certainty,
            project=project,
            branch=branch,
            session_id=session_id,
            validity_context=validity_context,
            supersedes=supersedes,
            origin=origin,
            source=source,
            candidate_limit=candidate_limit,
        )
        if decision.get("error"):
            return decision

        # merged_into: perform the append-merge instead of creating a new note
        if decision.get("decision") == "merged_into":
            vault = get_vault()
            bc = decision.get("best_candidate", {})
            sanitized_body = sanitize_secrets(body).strip()
            merge_result = merge_into_canonical(
                vault,
                bc.get("path", decision["path"]),
                title=title,
                body=sanitized_body,
                note_type=note_type,
                tags=tags,
                certainty=certainty,
                source=source,
                origin=origin or "mcp_store",
                validity_context=validity_context,
                supersedes=supersedes,
                project=project,
                branch=branch,
                session_id=session_id,
            )
            return {
                **decision,
                "merged": merge_result.get("merged", True),
                "canonical_path": merge_result.get("canonical_path", decision["path"]),
            }

        if decision.get("decision") != "created":
            return decision

        payload = decision["write"]
        vault = get_vault()
        path = write_note(
            vault,
            title=payload["title"],
            body=payload["body"],
            note_type=payload["note_type"],
            tags=payload["tags"],
            certainty=payload["certainty"],
            source=source,
            origin=payload["origin"],
            validity_context=payload["validity_context"],
            supersedes=payload["supersedes"],
            project=payload["project"],
            project_path=payload["project_path"],
            branch=payload["branch"],
            session_id=payload["session_id"],
        )

        if payload["project"]:
            # payload["project"] is already the normalized slug from
            # normalize_note_contract (MEM-164) - no need to re-derive it.
            summary = f"MCP smart-store: {payload['title'][:80]}"
            update_project_index(vault, payload["project"], path.stem, summary)

        return {
            **decision,
            "path": str(path.relative_to(vault)),
            "created": True,
        }
    finally:
        if not already_held:
            release_vault_write_lock()
