"""Shared helpers for the memento quality evals.

Design goals, in priority order:
1. Deterministic and dependency-free: stdlib only, no LLM, no network.
2. Understandable by any agent: every check produces a plain-English
   title, value, threshold, and remediation hint.
3. Safe: read-only against the vault and telemetry logs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_STATUS_ORDER = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}


@dataclass
class CheckResult:
    """One graded metric. Everything a reader needs to act on it."""

    id: str
    suite: str
    title: str
    status: str
    value: object = None
    unit: str = ""
    threshold: str = ""
    details: list = field(default_factory=list)
    remediation: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "suite": self.suite,
            "title": self.title,
            "status": self.status,
            "value": self.value,
            "unit": self.unit,
            "threshold": self.threshold,
            "details": self.details[:20],
            "remediation": self.remediation,
        }


_NOW_OVERRIDE: datetime | None = None


def parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts a trailing 'Z' as shorthand for +00:00. Naive input (no offset)
    is assumed to already be UTC; aware input is converted to UTC.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def set_now(value) -> None:
    """Freeze the eval clock returned by now().

    Accepts an aware datetime, an ISO-8601 string, or None. None clears the
    override, so a subsequent now() falls back to MEMENTO_EVAL_NOW or the
    real UTC clock.
    """
    global _NOW_OVERRIDE
    if value is None:
        _NOW_OVERRIDE = None
    elif isinstance(value, str):
        _NOW_OVERRIDE = parse_iso_utc(value)
    elif value.tzinfo is None:
        _NOW_OVERRIDE = value.replace(tzinfo=timezone.utc)
    else:
        _NOW_OVERRIDE = value.astimezone(timezone.utc)


def now() -> datetime:
    """The eval suite's notion of 'now'.

    Resolution order: the clock frozen via set_now()/--now, then the
    MEMENTO_EVAL_NOW environment variable, then the real UTC clock. Every
    eval suite must call this instead of datetime.now()/date.today() so a
    frozen clock makes two runs of the same suite byte-for-byte
    reproducible regardless of calendar drift. Always returns an
    aware UTC datetime.
    """
    if _NOW_OVERRIDE is not None:
        return _NOW_OVERRIDE
    env = os.environ.get("MEMENTO_EVAL_NOW")
    if env:
        return parse_iso_utc(env)
    return datetime.now(timezone.utc)


def grade(value, warn, fail, higher_is_better=True):
    """Grade a numeric value against warn/fail thresholds."""
    if value is None:
        return SKIP
    if higher_is_better:
        if value < fail:
            return FAIL
        if value < warn:
            return WARN
        return PASS
    if value > fail:
        return FAIL
    if value > warn:
        return WARN
    return PASS


# ---------------------------------------------------------------- thresholds


def _parse_scalar(raw):
    raw = raw.strip()
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw.strip("\"'")


def load_yaml_subset(path: Path) -> dict:
    """Parse the two-level `suite:\n  key: value` YAML subset used by
    thresholds.yml and the golden query files. Lists use `- item` lines.
    Falls back gracefully: unknown shapes are skipped, never fatal."""
    data: dict = {}
    if not path.exists():
        return data
    stack = [(0, data)]
    last_key_at = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip() if not raw_line.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent < stack[-1][0]:
            stack.pop()
        container = stack[-1][1] if stack else data
        stripped = line.strip()
        if stripped.startswith("- "):
            parent_key = last_key_at.get(stack[-1][0] if stack else 0)
            if isinstance(container, dict) and parent_key is not None:
                lst = container.setdefault(parent_key, [])
                if isinstance(lst, dict) and not lst:
                    lst = container[parent_key] = []
                if isinstance(lst, list):
                    item = stripped[2:].strip()
                    if ":" in item and not item.startswith(("'", '"')):
                        k, v = item.split(":", 1)
                        lst.append({k.strip(): _parse_scalar(v)})
                    else:
                        lst.append(_parse_scalar(item))
            continue
        if ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            if isinstance(container, dict):
                container[key] = _parse_scalar(rest)
        else:
            child: dict = {}
            if isinstance(container, dict):
                container[key] = child
            stack.append((indent + 2, child))
        last_key_at[indent] = key
    return data


_THRESHOLDS_CACHE = None


def thresholds() -> dict:
    global _THRESHOLDS_CACHE
    if _THRESHOLDS_CACHE is None:
        _THRESHOLDS_CACHE = load_yaml_subset(EVALS_DIR / "thresholds.yml")
    return _THRESHOLDS_CACHE


def threshold(suite: str, metric: str, kind: str, default):
    """Look up thresholds[suite][metric][kind], falling back to default."""
    node = thresholds().get(suite, {})
    if isinstance(node, dict):
        node = node.get(metric, {})
        if isinstance(node, dict) and kind in node:
            return node[kind]
    return default


# ------------------------------------------------------------------- vault


def find_vault() -> Path | None:
    env = os.environ.get("MEMENTO_VAULT_PATH")
    if env and Path(env).expanduser().is_dir():
        return Path(env).expanduser()
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from memento.config import get_vault  # type: ignore

        vault = Path(get_vault())
        if vault.is_dir():
            return vault
    except Exception:
        pass
    fallback = Path.home() / "memento"
    if fallback.is_dir():
        return fallback
    return None


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)


def parse_note(path: Path):
    """Return (frontmatter_dict, body) for a note. Frontmatter parsing is
    line-based on purpose: it must never crash on malformed notes, and the
    managed writer only emits simple `key: value` lines."""
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    fm: dict = {}
    for line in match.group(1).splitlines():
        if not line or line.startswith((" ", "\t", "-", "#")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fm[key.strip()] = value.strip()
    return fm, text[match.end() :]


def iter_notes(vault: Path, subdir: str = "notes"):
    root = vault / subdir
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        if path.name.startswith("."):
            continue
        yield path


def parse_note_date(fm: dict):
    raw = (fm or {}).get("date", "")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len(now().strftime(fmt))], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- telemetry


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "memento-vault"


def iter_jsonl(path: Path, since: datetime | None = None):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if since is not None:
                ts = parse_event_ts(event)
                if ts is not None and ts < since:
                    continue
            yield event


def parse_event_ts(event: dict):
    raw = str(event.get("ts", ""))
    raw = raw.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def window_start(days: int) -> datetime:
    return now() - timedelta(days=days)


def pct(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / denominator, 4)
