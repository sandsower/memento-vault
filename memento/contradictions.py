"""Contradiction and supersession inspection helpers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from memento.graph import read_note_metadata
from memento.search import has_qmd, miss_envelope, qmd_search_with_extras, resolve_concrete_mode
from memento.store import log_retrieval
from memento.config import get_vault

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
    """Inspect notes for disagreements, stale conclusions, and supersession chains."""
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
