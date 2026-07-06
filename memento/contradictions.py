"""Contradiction and supersession inspection helpers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sys

from memento.graph import read_note_metadata
from memento.search import has_qmd, miss_envelope, qmd_search_with_extras, resolve_concrete_mode
from memento.store import (
    _frontmatter_int,
    _frontmatter_scalar,
    _unmanaged_frontmatter_lines,
    _write_text_atomic,
    acquire_vault_write_lock,
    log_retrieval,
    owns_vault_write_lock,
    release_vault_write_lock,
    split_frontmatter,
)
from memento.config import get_config, get_vault

_POSITIVE_PHRASES = (
    "use",
    "prefer",
    "enabled",
    "enable",
    "on",
    "required",
    "allow",
    "keep",
    "true",
    "should",
    "must",
)

_NEGATIVE_PHRASES = (
    "do not use",
    "don't use",
    "avoid",
    "disabled",
    "disable",
    "off",
    "optional",
    "disallow",
    "block",
    "remove",
    "false",
    "should not",
    "must not",
)

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "which",
        "who",
        "what",
        "when",
        "where",
        "how",
        "not",
        "no",
        "and",
        "or",
        "but",
        "if",
        "than",
        "then",
        "so",
        "very",
    }
)


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(token) >= 3 and token not in _STOPWORDS
    ]


def _normalize_note_ref(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().strip('"').strip("'")
    text = text.lstrip(":").strip().strip('"').strip("'")
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
    text = text.split("|", 1)[0].strip()
    if not text:
        return None
    stem = Path(text).stem
    return stem or text.replace(" ", "-")


def _parse_note_text(note_path: Path) -> tuple[str, str]:
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return note_path.stem, ""

    title_match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip().strip('"').strip("'") if title_match else note_path.stem

    body_match = re.match(r"(?s)^---\n.*?\n---\n?(.*)$", text)
    body = body_match.group(1).strip() if body_match else text.strip()
    return title, body


def _note_polarity(text: str) -> tuple[str, list[str]]:
    lower = (text or "").lower()
    positive_hits = [phrase for phrase in _POSITIVE_PHRASES if phrase in lower]
    negative_hits = [phrase for phrase in _NEGATIVE_PHRASES if phrase in lower]
    if positive_hits and negative_hits:
        return "mixed", positive_hits + negative_hits
    if positive_hits:
        return "positive", positive_hits
    if negative_hits:
        return "negative", negative_hits
    return "neutral", []


def _is_relevant(entry: dict, min_certainty: int) -> bool:
    certainty = entry.get("certainty")
    if certainty is not None and certainty < min_certainty:
        return bool(entry.get("supersedes") or entry.get("superseded_by") or entry.get("_topic_tokens"))
    return bool(entry.get("_topic_tokens") or entry.get("supersedes") or entry.get("superseded_by"))


def inspect_contradictions(topic: str, limit: int = 20, min_certainty: int = 2) -> dict:
    """Inspect notes for disagreements, stale conclusions, and supersession chains.

    Default mode (MEM-163) reports validity chains: each chain walks
    ``note -> invalidated_by -> ...`` from the oldest note to the still-valid
    tail, with dates, instead of the old lexical polarity-guessing report.
    Set ``contradictions_lexical_fallback: true`` in config to restore the
    pre-MEM-163 lexical/contradictions/groups output shape instead.
    """
    config = get_config()
    if config.get("contradictions_lexical_fallback", False):
        return _inspect_contradictions_lexical(topic, limit=limit, min_certainty=min_certainty)
    return _inspect_validity_chains(topic, limit=limit, min_certainty=min_certainty)


def _inspect_contradictions_lexical(topic: str, limit: int = 20, min_certainty: int = 2) -> dict:
    """Pre-MEM-163 lexical polarity-matching contradiction/supersession report.

    Kept behind ``contradictions_lexical_fallback`` (config, default false)
    rather than deleted outright -- see :func:`inspect_contradictions`.
    """
    if not topic or not str(topic).strip():
        miss = miss_envelope("query_too_broad", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    try:
        min_certainty = max(1, min(int(min_certainty), 5))
    except (TypeError, ValueError):
        min_certainty = 2

    topic_tokens = set(_tokenize(topic))
    if not topic_tokens:
        miss = miss_envelope("query_too_broad", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    vault = get_vault()
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        miss = miss_envelope("empty_vault", details={"vault": str(vault)})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    notes: dict[str, dict] = {}
    reverse_supersedes: dict[str, list[str]] = defaultdict(list)

    for note_path in sorted(notes_dir.glob("*.md")):
        if note_path.name.startswith("."):
            continue

        stem = note_path.stem
        meta = read_note_metadata(stem) or {}
        title, body = _parse_note_text(note_path)
        tags = [str(tag).strip().strip('"').strip("'") for tag in (meta.get("tags") or []) if str(tag).strip()]

        certainty = meta.get("certainty")
        if certainty is not None:
            try:
                certainty = int(certainty)
            except (TypeError, ValueError):
                certainty = None

        date = meta.get("date")
        supersedes = _normalize_note_ref(meta.get("supersedes"))
        title_tokens = set(_tokenize(title))
        tag_tokens = set(_tokenize(" ".join(tags)))
        body_tokens = set(_tokenize(body[:800]))
        core_tokens = title_tokens | tag_tokens
        topic_tokens_matched = sorted((core_tokens | body_tokens) & topic_tokens)
        title_overlap = sorted(title_tokens & topic_tokens)
        tag_overlap = sorted(tag_tokens & topic_tokens)
        body_overlap = sorted(body_tokens & topic_tokens)
        polarity, signals = _note_polarity(f"{title}\n{body}")

        score = float(len(topic_tokens_matched))
        if title_overlap:
            score += 1.5
        if tag_overlap:
            score += 0.75
        if body_overlap:
            score += 0.25
        if supersedes:
            score += 0.15
        if certainty is not None and certainty >= min_certainty:
            score += 0.1

        notes[stem] = {
            "stem": stem,
            "path": str(note_path.relative_to(vault)),
            "title": title,
            "score": score,
            "certainty": certainty,
            "date": date,
            "tags": tags,
            "supersedes": supersedes,
            "superseded_by": [],
            "status": "active",
            "match_reasons": [],
            "signals": signals,
            "polarity": polarity,
            "snippet": body[:240].replace("\n", " ").strip(),
            "_tokens": core_tokens,
            "_topic_tokens": topic_tokens_matched,
        }

        if supersedes:
            reverse_supersedes[supersedes].append(stem)

    for target_stem, newer_stems in reverse_supersedes.items():
        if target_stem in notes:
            notes[target_stem]["superseded_by"] = sorted(set(newer_stems))

    def note_relevant(entry: dict) -> bool:
        return _is_relevant(entry, min_certainty)

    candidate_stems: list[str] = []
    if has_qmd():
        concrete_enabled, _ = resolve_concrete_mode("auto", topic)
        raw_results = qmd_search_with_extras(
            topic,
            limit=max(limit * 2, 8),
            semantic=False,
            timeout=10,
            min_score=0.0,
            concrete=concrete_enabled,
        )
        candidate_stems = [Path(result.get("path", "")).stem for result in raw_results if result.get("path")]
        candidate_stems = [stem for stem in candidate_stems if stem in notes]
        initial_scores = {
            Path(result.get("path", "")).stem: float(result.get("score", 0.0))
            for result in raw_results
            if result.get("path")
        }
        for stem in candidate_stems:
            notes[stem]["score"] = max(notes[stem]["score"], initial_scores.get(stem, 0.0))
            notes[stem]["match_reasons"].append("search hit")

    if not candidate_stems:
        ranked_local = sorted(
            (entry for entry in notes.values() if note_relevant(entry)),
            key=lambda item: (item["score"], item["certainty"] or 0, item["date"] or ""),
            reverse=True,
        )
        candidate_stems = [entry["stem"] for entry in ranked_local[: max(limit * 2, 8)]]
        for stem in candidate_stems:
            notes[stem]["match_reasons"].append("topic overlap")

    queue = list(candidate_stems)
    seen = set(candidate_stems)
    while queue and len(seen) < limit * 4:
        stem = queue.pop(0)
        note = notes.get(stem)
        if note is None:
            continue
        related: list[str] = []
        if note.get("supersedes"):
            related.append(note["supersedes"])
        related.extend(note.get("superseded_by") or [])
        for related_stem in related:
            if related_stem in notes and related_stem not in seen:
                seen.add(related_stem)
                queue.append(related_stem)
                notes[related_stem]["score"] = max(notes[related_stem]["score"], note["score"] + 0.15)
                notes[related_stem]["match_reasons"].append("supersession chain")

    selected = [notes[stem] for stem in seen if stem in notes and note_relevant(notes[stem])]
    if not selected:
        miss = miss_envelope("no_exact_match", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    selected.sort(key=lambda item: (item["score"], item["certainty"] or 0, item["date"] or ""), reverse=True)
    if len(selected) > limit:
        selected = selected[:limit]

    def finalize_note(entry: dict) -> dict:
        status_parts = []
        if entry.get("supersedes"):
            status_parts.append("superseding")
        if entry.get("superseded_by"):
            status_parts.append("superseded")
        if not status_parts:
            status_parts.append("active")
        return {
            "path": entry["path"],
            "title": entry["title"],
            "score": round(float(entry["score"]), 4),
            "certainty": entry["certainty"],
            "date": entry["date"],
            "tags": entry["tags"],
            "status": "+".join(status_parts),
            "supersedes": entry["supersedes"],
            "superseded_by": entry["superseded_by"] or None,
            "match_reasons": entry["match_reasons"] or ["topic overlap"],
            "signals": sorted(set(entry["signals"])),
            "polarity": entry["polarity"],
            "snippet": entry["snippet"],
        }

    result_notes = [finalize_note(entry) for entry in selected]

    adjacency: dict[str, set[str]] = {Path(item["path"]).stem: set() for item in result_notes}
    for stem, entry in notes.items():
        if stem not in adjacency:
            continue
        related = []
        if entry.get("supersedes"):
            related.append(entry["supersedes"])
        related.extend(entry.get("superseded_by") or [])
        for related_stem in related:
            if related_stem in adjacency:
                adjacency[stem].add(related_stem)
                adjacency[related_stem].add(stem)

    stems = list(adjacency)
    for i, left in enumerate(stems):
        for right in stems[i + 1 :]:
            if notes[left]["_tokens"] & notes[right]["_tokens"]:
                adjacency[left].add(right)
                adjacency[right].add(left)
            elif notes[left]["_topic_tokens"] and notes[right]["_topic_tokens"]:
                adjacency[left].add(right)
                adjacency[right].add(left)

    groups = []
    visited: set[str] = set()
    for stem in sorted(adjacency):
        if stem in visited:
            continue
        component: list[str] = []
        stack = [stem]
        visited.add(stem)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency.get(current, set())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)

        component_notes = [notes[item] for item in component]
        shared_terms = (
            sorted(set.intersection(*(set(item["_tokens"]) | set(item["_topic_tokens"]) for item in component_notes)))
            if component_notes
            else []
        )

        group_contradictions = []
        group_supersession = []
        for i, left_stem in enumerate(component):
            for right_stem in component[i + 1 :]:
                left_entry = notes[left_stem]
                right_entry = notes[right_stem]
                if left_entry.get("supersedes") == right_stem or right_entry.get("supersedes") == left_stem:
                    if left_entry.get("supersedes") == right_stem:
                        older, newer = right_stem, left_stem
                    else:
                        older, newer = left_stem, right_stem
                    pair = {
                        "older_path": notes[older]["path"],
                        "newer_path": notes[newer]["path"],
                        "older_title": notes[older]["title"],
                        "newer_title": notes[newer]["title"],
                    }
                    if pair not in group_supersession:
                        group_supersession.append(pair)

                left_positive = left_entry["polarity"] in {"positive", "mixed"}
                left_negative = left_entry["polarity"] in {"negative", "mixed"}
                right_positive = right_entry["polarity"] in {"positive", "mixed"}
                right_negative = right_entry["polarity"] in {"negative", "mixed"}
                opposite = (left_positive and right_negative) or (right_positive and left_negative)
                shared = sorted(
                    (set(left_entry["signals"]) | set(right_entry["signals"]))
                    or (notes[left_stem]["_tokens"] & notes[right_stem]["_tokens"])
                )
                if opposite and (
                    shared or left_entry.get("supersedes") == right_stem or right_entry.get("supersedes") == left_stem
                ):
                    contradiction = {
                        "kind": "opposite-language",
                        "paths": [notes[left_stem]["path"], notes[right_stem]["path"]],
                        "titles": [notes[left_stem]["title"], notes[right_stem]["title"]],
                        "terms": shared[:4],
                    }
                    if contradiction not in group_contradictions:
                        group_contradictions.append(contradiction)

        theme_entry = max(component_notes, key=lambda item: (item["score"], item["certainty"] or 0, item["date"] or ""))
        groups.append(
            {
                "theme": theme_entry["title"],
                "shared_terms": shared_terms[:5],
                "note_paths": [
                    notes[item]["path"]
                    for item in sorted(
                        component, key=lambda s: (notes[s]["score"], notes[s]["certainty"] or 0), reverse=True
                    )
                ],
                "summary": (
                    f"{len(component_notes)} notes, "
                    f"{sum(1 for item in component_notes if item['status'] != 'active')} marked, "
                    f"{len(group_contradictions)} contradiction(s)"
                ),
                "contradictions": group_contradictions,
                "supersession": group_supersession,
            }
        )

    contradictions = []
    supersession = []
    for group_index, group in enumerate(groups):
        for item in group["contradictions"]:
            contradictions.append({**item, "group_index": group_index})
        for item in group["supersession"]:
            if item not in supersession:
                supersession.append(item)

    superseded_paths = {item["older_path"] for item in supersession}
    superseding_paths = {item["newer_path"] for item in supersession}

    def _merge_status(current: str, flag: str) -> str:
        parts = [part for part in current.split("+") if part]
        if flag not in parts:
            parts.append(flag)
        return "+".join(parts) if parts else flag

    path_to_result = {item["path"]: item for item in result_notes}
    for item in result_notes:
        if item["path"] in superseded_paths:
            item["status"] = _merge_status(item["status"], "superseded")
        if item["path"] in superseding_paths:
            item["status"] = _merge_status(item["status"], "superseding")

    for group in groups:
        group_marked = sum(1 for path in group["note_paths"] if path_to_result.get(path, {}).get("status") != "active")
        group["summary"] = (
            f"{len(group['note_paths'])} notes, {group_marked} marked, {len(group['contradictions'])} contradiction(s)"
        )

    summary_parts = [f"{len(result_notes)} notes"]
    marked = sum(1 for item in result_notes if item["status"] != "active")
    if marked:
        summary_parts.append(f"{marked} marked supersession note(s)")
    if contradictions:
        summary_parts.append(f"{len(contradictions)} possible contradiction(s)")
    else:
        summary_parts.append("no obvious contradictions")

    payload = {
        "topic": topic,
        "results": result_notes,
        "groups": groups,
        "contradictions": contradictions,
        "supersession": supersession,
        "summary": "; ".join(summary_parts),
    }
    log_retrieval("search", "contradictions", topic=topic, results=len(result_notes), groups=len(groups))
    return payload


# --- Bitemporal supersession (MEM-163) ---
#
# `valid_from` (optional ISO date, defaults to a note's `date` when absent --
# never backfilled onto the file) and `invalidated_by` (optional note-ref
# stem) replace polarity-guessing with deterministic validity chains: a note
# is invalid once `invalidated_by` is set, and its replacement is named
# explicitly rather than inferred from opposite-language heuristics.


def _scan_notes_for_validity(vault: Path) -> dict[str, dict]:
    """Scan ``notes/*.md`` into a validity-chain-ready record per stem.

    Reads frontmatter directly via :mod:`memento.store`'s helpers (title,
    date, certainty, ``valid_from``, ``supersedes``, ``invalidated_by``) plus
    a lightweight topic-overlap token set computed by the caller. Returns an
    empty dict when ``notes/`` does not exist.
    """
    notes_dir = vault / "notes"
    notes: dict[str, dict] = {}
    if not notes_dir.exists():
        return notes

    for note_path in sorted(notes_dir.glob("*.md")):
        if note_path.name.startswith("."):
            continue
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        stem = note_path.stem
        frontmatter, body = split_frontmatter(text)
        title = _frontmatter_scalar(frontmatter, "title") or stem
        date = _frontmatter_scalar(frontmatter, "date")
        certainty = _frontmatter_int(frontmatter, "certainty")
        valid_from = _frontmatter_scalar(frontmatter, "valid_from") or date
        supersedes = _normalize_note_ref(_frontmatter_scalar(frontmatter, "supersedes"))
        invalidated_by = _normalize_note_ref(_frontmatter_scalar(frontmatter, "invalidated_by"))
        raw_tags = _frontmatter_scalar(frontmatter, "tags") or ""
        tags = [t.strip().strip("\"'") for t in raw_tags.strip("[]").split(",") if t.strip()]

        notes[stem] = {
            "stem": stem,
            "path": str(note_path.relative_to(vault)),
            "title": title,
            "date": date,
            "certainty": certainty,
            "valid_from": valid_from,
            "supersedes": supersedes,
            "invalidated_by": invalidated_by,
            "tags": tags,
            "_title_tokens": set(_tokenize(title)),
            "_tag_tokens": set(_tokenize(" ".join(tags))),
            "_body_tokens": set(_tokenize(body[:800])),
            "snippet": body[:240].replace("\n", " ").strip(),
        }

    return notes


def _select_validity_candidates(
    notes: dict[str, dict], topic_tokens: set[str], limit: int, min_certainty: int
) -> list[str]:
    """Rank notes by topic-token overlap, requiring a chain edge below ``min_certainty``."""
    scored = []
    for stem, entry in notes.items():
        core_tokens = entry["_title_tokens"] | entry["_tag_tokens"]
        matched = (core_tokens | entry["_body_tokens"]) & topic_tokens
        score = float(len(matched))
        if entry["_title_tokens"] & topic_tokens:
            score += 1.5
        if entry["_tag_tokens"] & topic_tokens:
            score += 0.75
        if not matched:
            score = 0.0
        has_chain_edge = bool(entry["supersedes"] or entry["invalidated_by"])
        certainty = entry["certainty"]
        relevant = bool(matched) or has_chain_edge
        if certainty is not None and certainty < min_certainty and not has_chain_edge:
            relevant = False
        if relevant and (matched or has_chain_edge):
            scored.append((stem, score, certainty or 0, entry["date"] or ""))

    scored.sort(key=lambda item: (item[1], item[2], item[3]), reverse=True)
    return [item[0] for item in scored[: max(limit * 2, 8)]]


def _inspect_validity_chains(topic: str, limit: int = 20, min_certainty: int = 2) -> dict:
    """Report validity chains (``note -> invalidated_by -> ...`` with dates) for a topic.

    This is the MEM-163 default output shape for :func:`inspect_contradictions`:
    instead of guessing contradictions from opposite-language polarity, it
    walks the deterministic ``invalidated_by`` links a note carries once the
    sweeper backlink pass (or contradiction adjudication auto-apply) has set
    them, grouping connected notes into chains from oldest to the current
    (still-valid) tail.
    """
    if not topic or not str(topic).strip():
        miss = miss_envelope("query_too_broad", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    try:
        min_certainty = max(1, min(int(min_certainty), 5))
    except (TypeError, ValueError):
        min_certainty = 2

    topic_tokens = set(_tokenize(topic))
    if not topic_tokens:
        miss = miss_envelope("query_too_broad", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    vault = get_vault()
    notes = _scan_notes_for_validity(vault)
    if not notes:
        miss = miss_envelope("empty_vault", details={"vault": str(vault)})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    candidate_stems = _select_validity_candidates(notes, topic_tokens, limit, min_certainty)
    if not candidate_stems:
        miss = miss_envelope("no_exact_match", details={"topic": topic})
        log_retrieval("search", "contradictions_miss", topic=topic, reason=miss["miss"]["reason"])
        return miss

    # Undirected adjacency over invalidated_by edges (every note pointing at
    # or pointed at by another via invalidated_by), spanning the WHOLE vault
    # -- not just matched candidates -- so a chain's earlier/later members
    # are included even when the topic search only matched one link of it.
    adjacency: dict[str, set[str]] = defaultdict(set)
    for stem, entry in notes.items():
        target = entry.get("invalidated_by")
        if target and target in notes:
            adjacency[stem].add(target)
            adjacency[target].add(stem)

    visited: set[str] = set()
    components: list[list[str]] = []
    for stem in sorted(adjacency):
        if stem in visited:
            continue
        stack = [stem]
        visited.add(stem)
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(component)

    matched_set = set(candidate_stems)

    def _node(stem: str) -> dict:
        entry = notes[stem]
        return {
            "path": entry["path"],
            "title": entry["title"],
            "date": entry["date"],
            "valid_from": entry["valid_from"],
            "certainty": entry["certainty"],
            "invalidated_by": entry["invalidated_by"],
            "status": "invalidated" if entry["invalidated_by"] else "current",
        }

    chains = []
    chain_member_stems: set[str] = set()
    for component in components:
        if not (set(component) & matched_set):
            continue
        ordered = sorted(component, key=lambda s: (notes[s]["date"] or "", s))
        nodes = [_node(s) for s in ordered]
        current_nodes = [n for n in nodes if n["status"] == "current"]
        chains.append(
            {
                "nodes": nodes,
                "current_path": current_nodes[-1]["path"] if current_nodes else None,
            }
        )
        chain_member_stems.update(component)

    standalone = [_node(stem) for stem in candidate_stems if stem not in chain_member_stems]

    if len(standalone) > limit:
        standalone = standalone[:limit]
    if len(chains) > limit:
        chains = chains[:limit]

    invalidated_count = sum(1 for chain in chains for node in chain["nodes"] if node["status"] == "invalidated")
    summary_parts = [f"{len(chains)} validity chain(s)"]
    if standalone:
        summary_parts.append(f"{len(standalone)} standalone note(s)")
    if invalidated_count:
        summary_parts.append(f"{invalidated_count} invalidated note(s)")
    else:
        summary_parts.append("no invalidated notes")

    payload = {
        "topic": topic,
        "chains": chains,
        "standalone": standalone,
        "summary": "; ".join(summary_parts) + f" for '{topic}'",
    }
    log_retrieval("search", "contradictions", topic=topic, results=len(chains) + len(standalone), groups=len(chains))
    return payload


def apply_invalidation(vault, rel_path, invalidated_by: str) -> bool:
    """Set ``invalidated_by`` on one note via a surgical single-field frontmatter rewrite.

    Preserves every other frontmatter line -- managed or not -- untouched,
    the same round-trip contract :func:`memento.store._fold_note_frontmatter`
    uses for the resurfacing fields. Idempotent: returns ``False`` (no write)
    when the note already carries this exact ``invalidated_by`` value or has
    no frontmatter block to rewrite. Uses :func:`memento.store._write_text_atomic`.
    """
    vault = Path(vault)
    candidate = Path(rel_path)
    note_path = candidate if candidate.is_absolute() else vault / candidate
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return False

    preserved = _unmanaged_frontmatter_lines(frontmatter, managed_keys={"invalidated_by"})
    new_frontmatter = "\n".join([*preserved, f"invalidated_by: {invalidated_by}"])
    new_text = f"---\n{new_frontmatter}\n---\n{body}"
    if new_text == text:
        return False
    _write_text_atomic(note_path, new_text)
    return True


def apply_supersession_backlinks(vault, *, dry_run: bool = False) -> dict:
    """Backlink ``supersedes`` edges into deterministic ``invalidated_by`` fields (MEM-163).

    For every note Y with ``supersedes: X`` where X is a note in this vault
    and X does not yet carry ``invalidated_by``, sets ``X.invalidated_by = Y``
    (Y's stem). Idempotent: an edge whose target already carries the correct
    ``invalidated_by`` is reported under ``already_set`` and never rewritten;
    a target that already carries a DIFFERENT ``invalidated_by`` value is left
    alone and reported under ``skipped`` (never overwritten -- a prior
    deterministic write, whether from this sweep or from MEM-163's
    contradiction-adjudication auto-apply, wins).

    Mutations happen under the vault write lock, re-entrant like
    :func:`memento.archive.sweep_archive_candidates` -- acquires only if not
    already held, never releases a lock a caller holds.

    Returns a report dict: ``{dry_run, checked, candidates, applied,
    already_set, skipped}``. ``candidates`` lists every supersession edge
    that still needs a backlink (populated regardless of ``dry_run``);
    ``applied`` is populated only for edges actually written.
    """
    vault = Path(vault)
    notes = _scan_notes_for_validity(vault)
    report: dict = {
        "dry_run": dry_run,
        "checked": len(notes),
        "candidates": [],
        "applied": [],
        "already_set": [],
        "skipped": [],
    }

    to_apply: list[tuple[str, str]] = []
    for stem, entry in notes.items():
        target_stem = entry.get("supersedes")
        if not target_stem or target_stem not in notes:
            continue
        target = notes[target_stem]
        if target.get("invalidated_by"):
            if target["invalidated_by"] == stem:
                report["already_set"].append({"path": target["path"], "invalidated_by": stem})
            else:
                report["skipped"].append(
                    {
                        "path": target["path"],
                        "reason": f"invalidated_by already set to {target['invalidated_by']!r}, not overwriting",
                    }
                )
            continue
        report["candidates"].append({"path": target["path"], "invalidated_by": stem})
        to_apply.append((target_stem, stem))

    if dry_run or not to_apply:
        return report

    already_held = owns_vault_write_lock()
    if not already_held and not acquire_vault_write_lock():
        for target_stem, newer_stem in to_apply:
            report["skipped"].append({"path": notes[target_stem]["path"], "reason": "vault write lock unavailable"})
        return report

    try:
        for target_stem, newer_stem in to_apply:
            target = notes[target_stem]
            try:
                applied = apply_invalidation(vault, target["path"], newer_stem)
            except OSError as exc:
                report["skipped"].append({"path": target["path"], "reason": f"write failed: {exc}"})
                continue
            report["applied"].append({"path": target["path"], "invalidated_by": newer_stem})
            if applied:
                print(
                    f"[memento] invalidated_by set: {target_stem} -> {newer_stem} (supersession backlink)",
                    file=sys.stderr,
                )
    finally:
        if not already_held:
            release_vault_write_lock()

    return report
