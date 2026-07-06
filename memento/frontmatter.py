"""Shared frontmatter parsing/serialization for Memento vault notes (MEM-166).

Before this module, at least three hand-rolled frontmatter parsers coexisted
and drifted: :mod:`memento.graph`'s line-prefix matcher, :mod:`memento.query`'s
``split(":", 1)`` scanner, and :mod:`memento.contradictions`'s ad-hoc tag
splitting -- plus :mod:`memento.store`'s regex-based typed accessors
(``split_frontmatter``, ``_frontmatter_scalar``/``_frontmatter_int``/
``_frontmatter_bool``) and :mod:`memento.smart_store`'s own copies of the
same regexes. This module is the single place that subset grammar lives;
every consumer above now reads through it (``memento.store`` keeps thin
delegating wrappers for backward compatibility -- see its module docstring).

**This is NOT a full YAML parser.** It implements exactly the subset that
``docs/frontmatter-schema.md`` (and ``scripts/check_frontmatter_schema.py``,
the schema drift-lock counterpart, see :data:`KNOWN_FIELDS`) documents:

- A frontmatter block is ONLY a **leading** ``---`` ... ``---`` fence. A
  ``---`` line inside the body never fabricates or truncates one
  (:func:`split_frontmatter`).
- Top-level keys match ``[A-Za-z0-9_-]+:`` at column 0. Everything after the
  colon on that same line is the key's inline value.
- **Scalars**: the rest of the line, quote-stripped (``'`` or ``"`` on either
  end). Scalars are returned as raw strings -- typed conversion (int/bool/
  date) is the job of the ``get_*`` accessors below, never of the parser
  itself.
- **Inline lists**: ``key: [a, b, "c d"]`` -- a single line wrapped in
  ``[...]``, comma-split, each item quote-stripped.
- **Block-style lists**:
  ``key:``
  ``  - a``
  ``  - b``
  -- indented ``- item`` lines immediately following a key with no inline
  value. This is the one gap the three legacy parsers all shared: they only
  understood the inline form, so block-style tags (and any other
  block-style list field) were silently invisible to graph/query/quality
  layers. :func:`parse` and :func:`get_list` both understand this form.
- Unknown/unmanaged keys are preserved by :func:`unmanaged_lines` verbatim
  (including any indented continuation lines), so rewrite paths never drop
  hand-added or future-schema fields they don't recognize.

Deliberately hand-rolled, not backed by PyYAML: this vault's frontmatter is a
small, fully-specified subset, and staying dependency-free keeps note reads
and writes portable across every environment Memento runs in (no optional
parser dependency for a read-mostly hot path).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# --- Splitting -------------------------------------------------------------

_LEADING_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split note text into ``(frontmatter, body)``.

    Only a LEADING ``---`` block counts as frontmatter -- ``---`` lines
    inside the body must never fabricate one (audit M6). Returns
    ``("", text)`` when the text does not start with a closed frontmatter
    block.
    """
    match = _LEADING_FRONTMATTER_RE.match(text or "")
    if not match:
        return "", text or ""
    return match.group(1), match.group(2)


# --- Scalar/list decoding shared by parse() and the get_* accessors --------

_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")
_BLOCK_LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")


def _clean_scalar(value: str) -> str:
    return (value or "").strip().strip("\"'")


def _decode_inline_value(value: str) -> str | list[str]:
    stripped = value.strip()
    # A wikilink scalar (`supersedes: [[older-note]]`) is doubly-bracketed
    # and must NOT be mistaken for a single-bracketed inline list -- no
    # schema list field's items are themselves `[...]`-wrapped, so this
    # exclusion is unambiguous.
    if (
        stripped.startswith("[")
        and stripped.endswith("]")
        and not (stripped.startswith("[[") and stripped.endswith("]]"))
    ):
        inner = stripped[1:-1]
        return [_clean_scalar(item) for item in inner.split(",") if _clean_scalar(item)]
    return _clean_scalar(stripped)


def _scan_fields(frontmatter: str) -> dict[str, str | list[str]]:
    """Decode every top-level key in a frontmatter blob into scalars/lists.

    Shared core for :func:`parse` (whole-document) and :func:`get_list`
    (single-key lookup) so both understand inline AND block-style lists
    identically.
    """
    fields: dict[str, str | list[str]] = {}
    lines = (frontmatter or "").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1] in (" ", "\t"):
            # Continuation line with no owning key on this scan pass (e.g. a
            # multi-line scalar on an unmanaged key) -- nothing safe to
            # decode into a field value, so skip it. unmanaged_lines() is
            # what preserves these verbatim for round-tripping.
            i += 1
            continue
        match = _KEY_LINE_RE.match(line)
        if not match:
            i += 1
            continue
        key, inline_value = match.group(1), match.group(2)
        i += 1
        if inline_value.strip():
            fields[key] = _decode_inline_value(inline_value)
            continue
        # No inline value -- look for block-style list items.
        items: list[str] = []
        while i < n and lines[i][:1] in (" ", "\t"):
            item_match = _BLOCK_LIST_ITEM_RE.match(lines[i].strip())
            if item_match:
                items.append(_clean_scalar(item_match.group(1)))
            i += 1
        fields[key] = items if items else ""
    return fields


def parse(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Parse note text into ``(fields, body)``.

    ``fields`` maps every top-level frontmatter key to either a scalar
    string or a ``list[str]`` (inline or block-style). Unknown keys are
    preserved in the dict the same way known ones are -- this function does
    not filter by :data:`KNOWN_FIELDS`. Returns ``({}, body)`` when there is
    no leading frontmatter block.
    """
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return {}, body
    return _scan_fields(frontmatter), body


# --- Typed accessors (operate on a raw frontmatter blob + key, matching the
# pre-MEM-166 memento.store._frontmatter_* call convention so those call
# sites move here unchanged) -------------------------------------------------


def get_scalar(frontmatter: str, key: str) -> str | None:
    """Return the raw single-line scalar value for a frontmatter key, or None."""
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", frontmatter or "", re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def get_int(frontmatter: str, key: str) -> int | None:
    """Return a frontmatter scalar as ``int``, or None if absent/unparseable."""
    raw = get_scalar(frontmatter, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


_TRUE_VALUES = {"true", "yes", "1"}


def get_bool(frontmatter: str, key: str) -> bool:
    """Return a frontmatter scalar as ``bool``. Absent/unparseable values are False."""
    raw = get_scalar(frontmatter, key)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES


_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def get_date(frontmatter: str, key: str) -> datetime | None:
    """Parse a frontmatter scalar's leading ``YYYY-MM-DD`` into a UTC datetime, or None.

    Tolerant of the fuller ``date`` shape (``YYYY-MM-DDTHH:MM``) -- only the
    date prefix is significant, matching the existing convention
    :func:`memento.hub._parse_date` established for hub regeneration.
    """
    raw = get_scalar(frontmatter, key)
    if not raw:
        return None
    match = _DATE_PREFIX_RE.match(raw.strip())
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def get_list(frontmatter: str, key: str) -> list[str]:
    """Return a frontmatter key's value as a list, understanding both list forms.

    Returns ``[]`` when the key is absent, or present but not list-shaped
    (a bare scalar under ``key:`` is not coerced into a one-item list --
    matching every legacy parser's existing behavior for that case).
    """
    value = _scan_fields(frontmatter or "").get(key)
    return value if isinstance(value, list) else []


# --- Round-trip: preserving/serializing unmanaged keys ----------------------

_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:")


def unmanaged_lines(frontmatter: str, managed_keys: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Return raw frontmatter lines for keys ``managed_keys`` does not cover.

    This is the MEM-128 ``_unmanaged_frontmatter_lines`` mechanism, moved
    here unchanged: rewrite paths (``replace_note_at_path``,
    ``fold_access_log_into_frontmatter``, stale-citation marking, ...) use
    it to round-trip hand-added or future-schema keys they don't manage.
    Each preserved ``key:`` line is kept verbatim together with its indented
    continuation lines (block lists, multi-line scalars) so rewrites never
    truncate a multi-line unmanaged value.
    """
    preserved: list[str] = []
    keep = False
    for line in (frontmatter or "").splitlines():
        if line[:1] in (" ", "\t"):
            if keep:
                preserved.append(line)
            continue
        match = _FRONTMATTER_KEY_RE.match(line)
        keep = bool(match) and match.group(1) not in managed_keys
        if keep:
            preserved.append(line)
    return preserved


def _encode_value(value: Any) -> str | None:
    """Render one field value as the text that follows ``key: `` on its line.

    Returns None for a value that should be omitted entirely (``None``).
    Lists are rendered as inline ``["a", "b"]`` (JSON-compatible, which is
    also valid for this module's inline-list grammar -- :func:`parse` reads
    it straight back via :func:`_decode_inline_value`).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        import json

        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def serialize(fields: dict[str, Any], body: str, *, key_order: list[str] | None = None) -> str:
    """Render ``(fields, body)`` back into full note text with a frontmatter fence.

    ``key_order`` controls emission order for the keys it names; any field
    present in ``fields`` but not named in ``key_order`` is appended
    afterward in dict-iteration order, so callers that only care about a
    handful of managed keys don't need to enumerate every possible key.
    Keys whose value is ``None`` are omitted from the output entirely
    (absence, not a null scalar) -- this is what lets rewrite paths pass a
    fields dict with optional keys unset without emitting ``key: None``.

    This is the inverse of :func:`parse` for the subset grammar this module
    supports -- round-tripping a value through ``parse(serialize(fields,
    body))`` reproduces ``fields``/``body`` (values are re-derived from
    text, so type distinctions parse() itself doesn't make, like
    scalar-vs-int, are not preserved beyond that subset either).
    """
    ordered_keys: list[str] = []
    seen: set[str] = set()
    for key in key_order or []:
        if key in fields and key not in seen:
            ordered_keys.append(key)
            seen.add(key)
    for key in fields:
        if key not in seen:
            ordered_keys.append(key)
            seen.add(key)

    lines = ["---"]
    for key in ordered_keys:
        encoded = _encode_value(fields[key])
        if encoded is None:
            continue
        lines.append(f"{key}: {encoded}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


# --- Schema drift lock -------------------------------------------------------
#
# scripts/check_frontmatter_schema.py's MANAGED_FIELD_TYPES table is the
# single source of truth for field semantics (it derives expectations from
# the actual writer implementations and compares them against
# docs/frontmatter-schema.md). KNOWN_FIELDS mirrors that table so this
# module's notion of "known managed fields" cannot silently drift from it --
# tests/test_frontmatter_schema.py asserts the two stay equal.
#
# Deliberately a literal copy rather than a live import: scripts/ is a CLI
# entry point, not a package memento/ should import from at runtime, so drift
# is caught by a test instead of a reverse dependency.
KNOWN_FIELDS: dict[str, str] = {
    "title": "string",
    "type": "enum",
    "tags": "list",
    "source": "enum-ish string",
    "origin": "string",
    "certainty": "int 1-5",
    "validity-context": "string",
    "supersedes": "wikilink or title",
    "synthesized_from": "list",
    "project": "string",
    "project_path": "string",
    "branch": "string",
    "date": "datetime",
    "session_id": "uuid/string",
    "repo_slug": "string",
    "citations": "list",
}
