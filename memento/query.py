"""Typed metadata queries over Memento vault notes.

This module intentionally scans Markdown/frontmatter directly instead of
accepting arbitrary query strings. It is a compact metadata surface for agents
that need counts, filters, or recent-session lists without retrieving note
bodies into context.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

ALLOWED_AGGREGATIONS = ("project", "type", "tag", "source", "month", "date", "branch", "session_id")
MISSING_VALUE = "(missing)"


class QueryValidationError(ValueError):
    """Raised when typed query parameters are outside the supported contract."""


def _strip_injection(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", text)
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text, flags=re.MULTILINE)
    text = re.sub(r"</?s>", "", text)
    return text


def _clean_scalar(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _parse_inline_list(value: str) -> list[str]:
    raw = value.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    return [_clean_scalar(item) for item in raw[1:-1].split(",") if _clean_scalar(item)]


def _split_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    frontmatter: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return "\n".join(frontmatter)
        frontmatter.append(line)
    return ""


def _read_note_record(vault: Path, path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    frontmatter = _split_frontmatter(text)
    metadata: dict[str, Any] = {
        "title": path.stem,
        "type": "",
        "tags": [],
        "source": "",
        "certainty": None,
        "date": "",
        "project": "",
        "branch": "",
        "session_id": "",
    }
    if frontmatter:
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, raw_value = stripped.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if key == "tags":
                metadata["tags"] = _parse_inline_list(value)
            elif key == "certainty":
                try:
                    metadata["certainty"] = int(_clean_scalar(value))
                except ValueError:
                    metadata["certainty"] = None
            elif key in {"title", "type", "source", "date", "project", "branch", "session_id"}:
                metadata[key] = _clean_scalar(value)

    rel_path = str(path.resolve().relative_to(vault.resolve())).replace(os.sep, "/")
    return {
        "path": rel_path,
        "title": _strip_injection(str(metadata.get("title") or path.stem)),
        "type": _strip_injection(str(metadata.get("type") or "")),
        "tags": [_strip_injection(str(tag)) for tag in metadata.get("tags", [])],
        "source": _strip_injection(str(metadata.get("source") or "")),
        "certainty": metadata.get("certainty"),
        "date": _strip_injection(str(metadata.get("date") or "")),
        "project": _strip_injection(str(metadata.get("project") or "")),
        "branch": _strip_injection(str(metadata.get("branch") or "")),
        "session_id": _strip_injection(str(metadata.get("session_id") or "")),
    }


def _iter_note_records(vault: Path) -> list[dict[str, Any]]:
    notes_dir = vault / "notes"
    if not notes_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(notes_dir.rglob("*.md")):
        if not path.is_file():
            continue
        record = _read_note_record(vault, path)
        if record is not None:
            records.append(record)
    return records


def _parse_datetime(value: str, *, boundary: str = "note") -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.combine(datetime.fromisoformat(text).date(), time.min)
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        if boundary == "note":
            return None
        raise QueryValidationError(f"{boundary} must be an ISO date or datetime") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_boundary(value: str, *, key: str, end_of_day: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_datetime(text, boundary=key)
    if parsed and end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.combine(parsed.date(), time.max)
    return parsed


def _validate_certainty(value: int | None, *, key: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        certainty = int(value)
    except (TypeError, ValueError) as exc:
        raise QueryValidationError(f"{key} must be an integer from 1 to 5") from exc
    if certainty < 1 or certainty > 5:
        raise QueryValidationError(f"{key} must be an integer from 1 to 5")
    return certainty


def _normalize_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        return 20


def _filters_metadata(
    *,
    project: str,
    note_type: str,
    tag: str,
    source: str,
    certainty_min: int | None,
    certainty_max: int | None,
    date_start: str,
    date_end: str,
    branch: str,
    session_id: str,
) -> dict[str, Any]:
    return {
        "project": project or None,
        "type": note_type or None,
        "tag": tag or None,
        "source": source or None,
        "certainty_min": certainty_min,
        "certainty_max": certainty_max,
        "date_start": date_start or None,
        "date_end": date_end or None,
        "branch": branch or None,
        "session_id": session_id or None,
    }


def _matches(record: dict[str, Any], filters: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    if filters["project"] and record.get("project") != filters["project"]:
        return False
    if filters["type"] and record.get("type") != filters["type"]:
        return False
    if filters["tag"] and filters["tag"] not in record.get("tags", []):
        return False
    if filters["source"] and record.get("source") != filters["source"]:
        return False
    if filters["branch"] and record.get("branch") != filters["branch"]:
        return False
    if filters["session_id"] and record.get("session_id") != filters["session_id"]:
        return False
    certainty = record.get("certainty")
    if filters["certainty_min"] is not None and (certainty is None or certainty < filters["certainty_min"]):
        return False
    if filters["certainty_max"] is not None and (certainty is None or certainty > filters["certainty_max"]):
        return False
    if start is not None or end is not None:
        note_date = _parse_datetime(record.get("date", ""), boundary="note")
        if note_date is None:
            return False
        if start is not None and note_date < start:
            return False
        if end is not None and note_date > end:
            return False
    return True


def _compact_note(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": record["path"],
        "title": record["title"],
        "type": record.get("type") or None,
        "tags": record.get("tags", []),
        "source": record.get("source") or None,
        "certainty": record.get("certainty"),
        "date": record.get("date") or None,
        "project": record.get("project") or None,
        "branch": record.get("branch") or None,
        "session_id": record.get("session_id") or None,
    }


def _aggregation_values(record: dict[str, Any], aggregate_by: str) -> list[str]:
    if aggregate_by == "tag":
        tags = record.get("tags") or []
        return tags or [MISSING_VALUE]
    if aggregate_by == "month":
        note_date = _parse_datetime(record.get("date", ""), boundary="note")
        return [note_date.strftime("%Y-%m") if note_date else MISSING_VALUE]
    if aggregate_by == "date":
        note_date = _parse_datetime(record.get("date", ""), boundary="note")
        return [note_date.date().isoformat() if note_date else MISSING_VALUE]
    key = "type" if aggregate_by == "type" else aggregate_by
    return [str(record.get(key) or MISSING_VALUE)]


def _aggregate(records: list[dict[str, Any]], aggregate_by: str, limit: int) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for record in records:
        for value in _aggregation_values(record, aggregate_by):
            counts[value] += 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _recent_sessions(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        session_id = str(record.get("session_id") or "").strip()
        if not session_id:
            continue
        entry = grouped.setdefault(
            session_id,
            {
                "session_id": session_id,
                "project": record.get("project") or None,
                "latest_date": None,
                "note_count": 0,
                "branches": set(),
                "paths": [],
            },
        )
        entry["note_count"] += 1
        entry["paths"].append(record["path"])
        if record.get("branch"):
            entry["branches"].add(record["branch"])
        note_date = _parse_datetime(record.get("date", ""), boundary="note")
        if note_date is not None and (entry["latest_date"] is None or note_date > entry["latest_date"]):
            entry["latest_date"] = note_date

    sessions = []
    for entry in grouped.values():
        latest = entry["latest_date"]
        sessions.append(
            {
                "session_id": entry["session_id"],
                "project": entry["project"],
                "latest_date": latest.isoformat(timespec="minutes") if latest else None,
                "note_count": entry["note_count"],
                "branches": sorted(entry["branches"]),
                "paths": sorted(entry["paths"]),
            }
        )
    return sorted(sessions, key=lambda item: (item["latest_date"] or "", item["session_id"]), reverse=True)[:limit]


def query_notes(
    vault: Path,
    *,
    project: str = "",
    note_type: str = "",
    tag: str = "",
    source: str = "",
    certainty_min: int | None = None,
    certainty_max: int | None = None,
    date_start: str = "",
    date_end: str = "",
    branch: str = "",
    session_id: str = "",
    aggregate_by: str = "",
    recent_sessions_project: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Run a typed metadata query over vault notes and return compact results."""

    try:
        normalized_limit = _normalize_limit(limit)
        aggregate = str(aggregate_by or "").strip()
        if aggregate and aggregate not in ALLOWED_AGGREGATIONS:
            allowed = ", ".join(ALLOWED_AGGREGATIONS)
            raise QueryValidationError(f"aggregate_by must be one of: {allowed}")
        clean_min = _validate_certainty(certainty_min, key="certainty_min")
        clean_max = _validate_certainty(certainty_max, key="certainty_max")
        if clean_min is not None and clean_max is not None and clean_min > clean_max:
            raise QueryValidationError("certainty_min must be <= certainty_max")
        start = _parse_boundary(date_start, key="date_start")
        end = _parse_boundary(date_end, key="date_end", end_of_day=True)
        if start is not None and end is not None and start > end:
            raise QueryValidationError("date_start must be <= date_end")
    except QueryValidationError as exc:
        return {"error": str(exc), "metadata": {"valid": False}}

    effective_project = recent_sessions_project or project
    filters = _filters_metadata(
        project=effective_project,
        note_type=note_type,
        tag=tag,
        source=source,
        certainty_min=clean_min,
        certainty_max=clean_max,
        date_start=date_start,
        date_end=date_end,
        branch=branch,
        session_id=session_id,
    )
    records = _iter_note_records(vault)
    matched = [record for record in records if _matches(record, filters, start, end)]
    metadata = {
        "valid": True,
        "scanned_notes": len(records),
        "matched_notes": len(matched),
        "filters": filters,
        "aggregate_by": aggregate or None,
        "recent_sessions_project": recent_sessions_project or None,
        "limit": normalized_limit,
    }

    if recent_sessions_project:
        return {"recent_sessions": _recent_sessions(matched, normalized_limit), "metadata": metadata}
    if aggregate:
        return {"aggregations": _aggregate(matched, aggregate, normalized_limit), "metadata": metadata}
    return {"results": [_compact_note(record) for record in matched[:normalized_limit]], "metadata": metadata}
