"""State, logging, note writing, and vault write coordination."""

import hashlib
import math
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import memento.frontmatter as frontmatter_module
from memento.config import RUNTIME_DIR, get_config, get_vault_id, repo_slug_from_path, slugify

RETRIEVAL_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "retrieval.jsonl",
)

TRIAGE_HEALTH_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "triage-health.jsonl",
)

AUTOMATION_MEMORY_HEALTH_LOG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "automation-memory-health.jsonl",
)

ACCESS_LOG_PATH = os.path.join(RUNTIME_DIR, "access-log.jsonl")
ACCESS_LOG_STATS_PATH = os.path.join(RUNTIME_DIR, "access-log-stats.json")

# Citation-staleness review queue (MEM-162): verify-at-use appends here when
# a cited anchor no longer matches, fold_stale_citations_into_frontmatter
# drains it into durable `citation_stale: true` frontmatter.
STALE_CITATIONS_QUEUE_PATH = os.path.join(RUNTIME_DIR, "stale-citations.jsonl")

INCEPTION_STATE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
    "memento-vault",
    "inception-state.json",
)

INCEPTION_LOCK_PATH = os.path.join(RUNTIME_DIR, "inception.lock")
VAULT_WRITE_LOCK_PATH = os.path.join(RUNTIME_DIR, "vault-write.lock")


def _should_log():
    """Check if retrieval logging is enabled (config or env var)."""
    if os.environ.get("MEMENTO_DEBUG"):
        return True
    return get_config().get("retrieval_log", False)


def _append_jsonl(path, entry, warn_attr):
    try:
        log_dir = os.path.dirname(path)
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:
        if not getattr(_append_jsonl, warn_attr, False):
            import sys as _sys

            print(f"[memento] warning: cannot write log {path}: {exc}", file=_sys.stderr)
            setattr(_append_jsonl, warn_attr, True)


def log_retrieval(hook, action, **kwargs):
    """Append a structured log entry to the retrieval log."""
    if not _should_log():
        return

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hook": hook,
        "action": action,
    }
    entry.update(kwargs)
    _append_jsonl(RETRIEVAL_LOG_PATH, entry, "_retrieval_warned")


def _sanitize_health_error(error):
    try:
        from memento.utils import sanitize_secrets

        sanitized = sanitize_secrets(str(error))
    except Exception:
        sanitized = str(error)
    if len(sanitized) > 500:
        return sanitized[:500] + "..."
    return sanitized


def log_triage_health(action, hook="triage", **kwargs):
    """Append minimal always-on health telemetry.

    Primary callers use this for SessionEnd extraction health, but the same
    durable log also carries Pi bridge failure records (hook="pi-bridge") so
    health surfaces can show bridge regressions even when the Python adapter is
    unavailable.
    """
    safe_kwargs = dict(kwargs)
    if "error" in safe_kwargs:
        safe_kwargs["error"] = _sanitize_health_error(safe_kwargs["error"])
    for key, value in list(safe_kwargs.items()):
        if isinstance(value, str):
            safe_kwargs[key] = _sanitize_health_error(value)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hook": hook,
        "action": action,
    }
    entry.update(safe_kwargs)
    _append_jsonl(TRIAGE_HEALTH_LOG_PATH, entry, "_triage_health_warned")


def log_automation_memory_health(action, **kwargs):
    """Append minimal always-on automation memory readiness telemetry.

    This is operational health data, not a run ledger: no cwd, prompt text,
    note content, session transcript, or run evidence is recorded.
    """
    safe_kwargs = dict(kwargs)
    if "error" in safe_kwargs:
        safe_kwargs["error"] = _sanitize_health_error(safe_kwargs["error"])
    for key, value in list(safe_kwargs.items()):
        if isinstance(value, str):
            safe_kwargs[key] = _sanitize_health_error(value)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hook": "automation-memory",
        "action": action,
    }
    entry.update(safe_kwargs)
    _append_jsonl(AUTOMATION_MEMORY_HEALTH_LOG_PATH, entry, "_automation_memory_health_warned")


_ACCESS_LOG_CACHE = {"signature": None, "stats": {}}
_ACCESS_LOG_EVENT_CAP = 200

# Below this many cached events for a candidate, apply_access_log_boost() also
# checks the note's own frontmatter (resurfaced_count/last_resurfaced) so a
# purged runtime-dir cache (or one that never caught up on old history) does
# not silently reset the resurfacing signal. Notes that already have a
# healthy cached history skip the extra frontmatter read entirely.
_ACCESS_LOG_SEED_THRESHOLD = 3


def _should_track_access():
    return get_config().get("access_log_enabled", True)


def _current_vault_id():
    try:
        vault_id = get_vault_id()
        if vault_id:
            return str(vault_id)
    except Exception:
        pass

    try:
        vault_path = str(get_config().get("vault_path") or "")
        if vault_path:
            return hashlib.sha256(vault_path.encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass

    return "unknown"


def _access_log_query_summary(query):
    if query is None:
        return ""
    try:
        from memento.utils import sanitize_secrets

        summary = sanitize_secrets(" ".join(str(query).split()))
    except Exception:
        summary = " ".join(str(query).split())
    return summary[:160]


def _access_log_query_hash(query_summary):
    if not query_summary:
        return ""
    return hashlib.sha256(query_summary.encode("utf-8")).hexdigest()


def _parse_access_log_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_access_path(path):
    text = str(path or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    try:
        vault = Path(get_config()["vault_path"]).expanduser().resolve()
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved == vault:
                return ""
            try:
                return str(resolved.relative_to(vault)).replace(os.sep, "/")
            except ValueError:
                return normalized
    except Exception:
        pass
    return normalized


def _read_access_log_stats_file():
    try:
        with open(ACCESS_LOG_STATS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"vaults": {}}
    if not isinstance(data, dict):
        return {"vaults": {}}
    vaults = data.get("vaults")
    if not isinstance(vaults, dict):
        data["vaults"] = {}
    return data


def _write_access_log_stats_file(data):
    os.makedirs(os.path.dirname(ACCESS_LOG_STATS_PATH), exist_ok=True)
    tmp = ACCESS_LOG_STATS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, ACCESS_LOG_STATS_PATH)


def _trim_access_events(events):
    if len(events) > _ACCESS_LOG_EVENT_CAP:
        return events[-_ACCESS_LOG_EVENT_CAP:]
    return events


def _events_from_raw_access_log(vault_id):
    stats = {}
    try:
        with open(ACCESS_LOG_PATH) as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("vault_id") or "") != vault_id:
                    continue
                path = str(entry.get("path") or "").strip()
                if not path:
                    continue
                ts = _parse_access_log_ts(entry.get("ts"))
                if ts is None:
                    continue
                try:
                    rank = max(1, int(entry.get("rank") or 1))
                except (TypeError, ValueError):
                    rank = 1
                bucket = stats.setdefault(path, {"events": []})
                bucket["events"].append({"ts": ts, "rank": rank})
    except OSError:
        return {}

    for bucket in stats.values():
        bucket["events"] = _trim_access_events(bucket["events"])
    return stats


def _update_access_log_stats(vault_id, entries):
    if not entries:
        return

    try:
        data = _read_access_log_stats_file()
        vaults = data.setdefault("vaults", {})
        vault_entry = vaults.setdefault(vault_id, {"paths": {}, "updated_at": None})
        paths = vault_entry.setdefault("paths", {})

        for entry in entries:
            path = entry["path"]
            bucket = paths.setdefault(path, {"events": []})
            bucket_events = bucket.setdefault("events", [])
            bucket_events.append({"ts": entry["ts"], "rank": entry["rank"]})
            bucket["events"] = _trim_access_events(bucket_events)

        vault_entry["updated_at"] = entries[-1]["ts"]
        _write_access_log_stats_file(data)
    except OSError:
        pass


def record_access(paths, *, hook="unknown", tool="unknown", query=None, session_id=None, result_count=None):
    """Append derived access-log entries for successful retrievals.

    The access log lives in runtime state, not the vault, so it can be rebuilt
    or discarded without touching Markdown notes or git history.
    """
    if not _should_track_access():
        return

    if isinstance(paths, (str, Path)):
        path_list = [paths]
    else:
        path_list = list(paths or [])

    path_list = [_normalize_access_path(path) for path in path_list]
    path_list = [path for path in path_list if path]
    if not path_list:
        return

    vault_id = _current_vault_id()
    query_summary = _access_log_query_summary(query)
    query_hash = _access_log_query_hash(query_summary)
    entry_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats_entries = []

    for rank, path in enumerate(path_list, start=1):
        entry = {"ts": entry_ts, "path": path, "hook": hook, "tool": tool, "rank": rank, "vault_id": vault_id}
        if query_summary:
            entry["query_summary"] = query_summary
            entry["query_hash"] = query_hash
        if session_id:
            entry["session_id"] = session_id
        if result_count is not None:
            entry["result_count"] = result_count
        _append_jsonl(ACCESS_LOG_PATH, entry, "_access_log_warned")
        stats_entries.append({"path": path, "ts": entry_ts, "rank": rank})

    _update_access_log_stats(vault_id, stats_entries)


def load_access_log_stats():
    """Load aggregated access-log events keyed by note path for this vault."""
    vault_id = _current_vault_id()
    try:
        stat = os.stat(ACCESS_LOG_STATS_PATH)
    except OSError:
        signature = (vault_id, None, None)
        if _ACCESS_LOG_CACHE.get("signature") == signature:
            return _ACCESS_LOG_CACHE.get("stats", {})
        stats = _events_from_raw_access_log(vault_id)
        _ACCESS_LOG_CACHE["signature"] = signature
        _ACCESS_LOG_CACHE["stats"] = stats
        return stats

    signature = (vault_id, stat.st_mtime_ns, stat.st_size)
    if _ACCESS_LOG_CACHE.get("signature") == signature:
        return _ACCESS_LOG_CACHE.get("stats", {})

    data = _read_access_log_stats_file()
    vault_stats = data.get("vaults", {}).get(vault_id, {}) if isinstance(data.get("vaults"), dict) else {}
    stats = {}
    for path, bucket in (vault_stats.get("paths") or {}).items():
        events = []
        for event in (bucket.get("events") or [])[-_ACCESS_LOG_EVENT_CAP:]:
            ts = _parse_access_log_ts(event.get("ts"))
            if ts is None:
                continue
            try:
                rank = max(1, int(event.get("rank") or 1))
            except (TypeError, ValueError):
                rank = 1
            events.append({"ts": ts, "rank": rank})
        if events:
            stats[path] = {"events": events}

    if not stats:
        stats = _events_from_raw_access_log(vault_id)
        if stats:
            data.setdefault("vaults", {})[vault_id] = {
                "paths": {
                    path: {
                        "events": [{"ts": event["ts"].isoformat(), "rank": event["rank"]} for event in bucket["events"]]
                    }
                    for path, bucket in stats.items()
                },
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _write_access_log_stats_file(data)
            stat = os.stat(ACCESS_LOG_STATS_PATH)
            signature = (vault_id, stat.st_mtime_ns, stat.st_size)

    _ACCESS_LOG_CACHE["signature"] = signature
    _ACCESS_LOG_CACHE["stats"] = stats
    return stats


def write_access_log_stats(stats: dict) -> None:
    """Replace access-log aggregated stats for this vault's paths.

    *stats* should be in the same ``{path: {"events": [{"ts": ..., "rank": ...}]}}``
    shape returned by ``load_access_log_stats``.  The cache is invalidated so
    subsequent reads re-read the persisted file.
    """
    vault_id = _current_vault_id()
    data = _read_access_log_stats_file()
    data.setdefault("vaults", {})[vault_id] = {
        "paths": {
            path: {
                "events": [
                    {
                        "ts": event.get("ts").isoformat()
                        if hasattr(event.get("ts"), "isoformat")
                        else str(event.get("ts", "")),
                        "rank": int(event.get("rank", 1)),
                    }
                    for event in (bucket.get("events", []) or [])[-_ACCESS_LOG_EVENT_CAP:]
                ]
            }
            for path, bucket in stats.items()
        },
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_access_log_stats_file(data)
    # Invalidate the in-memory cache so next load re-reads.
    _ACCESS_LOG_CACHE["signature"] = (vault_id, None, None)


def apply_access_log_boost(results, config=None, now=None):
    """Boost scores for notes that have been repeatedly and recently accessed."""
    if config is None:
        config = get_config()
    if not config.get("access_log_enabled", True):
        return results

    try:
        weight = float(config.get("access_log_boost_weight", 0.12))
    except (TypeError, ValueError):
        weight = 0.12
    if weight <= 0 or not results:
        return results

    try:
        half_life_days = float(config.get("access_log_half_life_days", 30))
    except (TypeError, ValueError):
        half_life_days = 30.0
    if half_life_days < 0:
        half_life_days = 0.0

    current = now or datetime.now(timezone.utc)
    stats = load_access_log_stats()

    vault_path = str(config.get("vault_path") or "")
    vault = None
    if vault_path:
        try:
            vault = Path(vault_path).expanduser().resolve()
        except OSError:
            vault = None

    if not stats and vault is None:
        return results

    for result in results:
        path = str(result.get("path") or "")
        events = list(stats.get(path, {}).get("events", []))

        # A cache wipe (or a note whose access history predates the cache)
        # leaves few or no events here even though the note's own frontmatter
        # remembers being resurfaced. Seed/merge from that durable signal so
        # the boost below still sees it -- the decay formula itself is
        # unchanged, it just gets a richer event list to sum over.
        if vault is not None and path and len(events) < _ACCESS_LOG_SEED_THRESHOLD:
            fm_count, fm_last = _frontmatter_resurfacing_signal(vault, path)
            if fm_count > len(events) and fm_last is not None:
                events = events + [{"ts": fm_last, "rank": 1} for _ in range(fm_count - len(events))]

        if not events:
            continue

        signal = 0.0
        for event in events[-_ACCESS_LOG_EVENT_CAP:]:
            event_ts = event.get("ts")
            if not isinstance(event_ts, datetime):
                continue
            age_days = max((current - event_ts).total_seconds() / 86400.0, 0.0)
            decay = 1.0 if half_life_days <= 0 else 0.5 ** (age_days / half_life_days)
            rank = max(1, int(event.get("rank") or 1))
            signal += decay / rank

        if signal <= 0:
            continue
        boost = 1.0 + weight * math.log1p(signal)
        result["score"] = round(float(result.get("score", 0.0)) * boost, 4)

    results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return results


def _frontmatter_resurfacing_signal(vault, rel_path):
    """Read ``(resurfaced_count, last_resurfaced)`` from a note's frontmatter.

    These two fields are the durable side of the resurfacing signal, kept in
    sync with the (purgeable) access-log cache by
    :func:`fold_access_log_into_frontmatter`. Returns ``(0, None)`` for any
    missing note, unreadable file, absent frontmatter block, or note that has
    never been folded -- callers treat that as "no durable signal to seed
    from," never as an error.
    """
    try:
        note_path = (vault / rel_path).resolve()
        note_path.relative_to(vault)
    except (OSError, ValueError):
        return 0, None
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, None

    frontmatter, _ = split_frontmatter(text)
    last_ts = _parse_access_log_ts(_frontmatter_scalar(frontmatter, "last_resurfaced"))
    if last_ts is None:
        return 0, None
    return _frontmatter_int(frontmatter, "resurfaced_count") or 0, last_ts


# Default window (days) for the "hot" durability tier -- configurable via
# get_config()["durability_hot_window_days"] (MEM-150).
DEFAULT_DURABILITY_HOT_WINDOW_DAYS = 30

# Ordered so callers can treat earlier tiers as "more durable" if useful.
DURABILITY_TIERS = ("pinned", "hot", "warm", "cold")


def durability_tier(frontmatter, now=None, *, hot_window_days=None):
    """Derive a note's durability tier from its raw frontmatter text (MEM-150).

    Certainty keeps meaning "how sure this is true"; it no longer confers
    decay immunity on its own. This is the one pure, side-effect-free
    computation that decides what *does*: :func:`memento.search.apply_temporal_decay`
    treats ``pinned``/``hot`` as decay-immune and lets ``warm``/``cold`` decay
    normally (a certainty-5 note nobody has looked at in 90 days sinks like
    any other). MEM-152's archive sweep is expected to reuse this same
    function rather than re-deriving the tier.

    Tiers, most to least durable:
    - ``"pinned"``: frontmatter has ``pinned: true`` (manual, permanent).
    - ``"hot"``: ``last_resurfaced`` is within ``hot_window_days`` of ``now``
      (default :data:`DEFAULT_DURABILITY_HOT_WINDOW_DAYS`, 30).
    - ``"warm"``: ``resurfaced_count`` > 0 at some point, but not within the
      hot window.
    - ``"cold"``: never resurfaced.

    ``now`` defaults to the current UTC time. A naive ``now`` (or a naive
    ``last_resurfaced``, which should not happen post-MEM-148 but is handled
    defensively) is treated as UTC, matching :func:`_parse_access_log_ts`'s
    convention elsewhere in this module.

    Pure: no file I/O and no config lookups -- callers resolve the
    frontmatter text and the hot-window override themselves (see
    :func:`read_durability_tier` for the file-reading convenience wrapper).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if hot_window_days is None:
        hot_window_days = DEFAULT_DURABILITY_HOT_WINDOW_DAYS

    if _frontmatter_bool(frontmatter, "pinned"):
        return "pinned"

    last_ts = _parse_access_log_ts(_frontmatter_scalar(frontmatter, "last_resurfaced"))
    if last_ts is not None:
        age_days = (now - last_ts).total_seconds() / 86400.0
        if age_days <= hot_window_days:
            return "hot"

    count = _frontmatter_int(frontmatter, "resurfaced_count") or 0
    if count > 0:
        return "warm"

    return "cold"


def read_durability_tier(vault, rel_path, config=None, now=None):
    """Read a note's frontmatter off disk and compute its durability tier.

    Thin file-I/O wrapper around the pure :func:`durability_tier` so callers
    (``memento.search.apply_temporal_decay``, and MEM-152's archive sweep)
    don't each duplicate the read. ``vault`` may be a path-like or ``Path``;
    ``rel_path`` is vault-relative (e.g. ``"notes/example.md"``). Tolerates
    the same missing/unreadable/traversal cases
    :func:`_frontmatter_resurfacing_signal` does -- returns ``"cold"``
    (never an error) since "no signal to read" and "never resurfaced"
    collapse to the same tier.
    """
    if config is None:
        config = get_config()
    hot_window_days = config.get("durability_hot_window_days", DEFAULT_DURABILITY_HOT_WINDOW_DAYS)

    try:
        vault_path = Path(vault).resolve()
        note_path = (vault_path / rel_path).resolve()
        note_path.relative_to(vault_path)
    except (OSError, ValueError):
        return "cold"
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "cold"

    frontmatter, _ = split_frontmatter(text)
    return durability_tier(frontmatter, now=now, hot_window_days=hot_window_days)


def load_inception_state(state_path=None):
    """Load Inception state from disk. Returns defaults if missing/corrupt."""
    path = state_path or INCEPTION_STATE_PATH
    defaults = {
        "last_run_iso": None,
        "last_run_note_count": 0,
        "runs": [],
        "processed_notes": [],
    }
    try:
        with open(path) as f:
            state = json.load(f)
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state
    except FileNotFoundError:
        return dict(defaults)
    except (json.JSONDecodeError, KeyError):
        bak = path + ".bak"
        try:
            os.rename(path, bak)
        except OSError:
            pass
        return dict(defaults)


def save_inception_state(state, state_path=None):
    """Persist Inception state. Keeps only last 10 runs."""
    path = state_path or INCEPTION_STATE_PATH
    state["runs"] = state.get("runs", [])[-10:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _acquire_pid_lock(path):
    """Acquire an exclusive pid lock file, breaking stale locks."""
    lock_path = Path(path)
    os.makedirs(lock_path.parent, exist_ok=True)

    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age < 600:
                try:
                    pid = int(lock_path.read_text().strip())
                    os.kill(pid, 0)
                    return False
                except (ValueError, OSError):
                    pass
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def acquire_inception_lock(lock_path=None):
    """File-based lock for Inception. Returns True if acquired."""
    return _acquire_pid_lock(lock_path or INCEPTION_LOCK_PATH)


def release_inception_lock(lock_path=None):
    """Release the Inception lock file."""
    path = Path(lock_path or INCEPTION_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def owns_vault_write_lock(lock_file=None):
    """Return True when the vault write lock exists and is held by this process.

    The lock file stores the owner's pid, so write paths that may be called both
    standalone and from callers that already hold the lock (e.g. the MCP server)
    can stay re-entrant: acquire only when the lock is not already ours, and
    never release a lock the caller owns.
    """
    path = Path(lock_file or VAULT_WRITE_LOCK_PATH)
    try:
        return int(path.read_text().strip()) == os.getpid()
    except (OSError, ValueError):
        return False


def acquire_vault_write_lock(lock_file=None, timeout=5.0, poll_interval=0.05, *, lock_path=None):
    """Acquire a short-lived vault write lock, polling until timeout.

    Args:
        lock_file: Path to the lock file. Defaults to ``VAULT_WRITE_LOCK_PATH``.
        timeout: Seconds to wait for the lock before giving up.
        poll_interval: Seconds between acquisition retries.
        lock_path: Deprecated alias for ``lock_file``. Kept for backward
            compatibility — prefer ``lock_file`` in new code.
    """
    if lock_path is not None:
        if lock_file is not None:
            raise TypeError("acquire_vault_write_lock(): specify either lock_file or lock_path, not both")
        lock_file = lock_path
    deadline = time.monotonic() + timeout
    path = lock_file or VAULT_WRITE_LOCK_PATH
    while True:
        if _acquire_pid_lock(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def release_vault_write_lock(lock_file=None, *, lock_path=None):
    """Release the vault write lock file.

    Args:
        lock_file: Path to the lock file. Defaults to ``VAULT_WRITE_LOCK_PATH``.
        lock_path: Deprecated alias for ``lock_file``. Kept for backward
            compatibility — prefer ``lock_file`` in new code.
    """
    if lock_path is not None:
        if lock_file is not None:
            raise TypeError("release_vault_write_lock(): specify either lock_file or lock_path, not both")
        lock_file = lock_path
    path = Path(lock_file or VAULT_WRITE_LOCK_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def append_fleeting_session(
    vault_path,
    session_id,
    *,
    cwd=None,
    branch=None,
    agent=None,
    files_edited=None,
    now=None,
):
    """Append a one-line marker for a session to today's fleeting log.

    The fleeting log is a per-UTC-day markdown file at
    ``<vault>/fleeting/<YYYY-MM-DD>.md`` that records *"a session occurred"*
    even when the agent's transcript wasn't substantive enough to triage into
    an atomic note. It is the canonical breadcrumb other harnesses (OpenCode,
    Codex, Cursor) should write to so the vault has consistent activity data
    regardless of which agent ran.

    The caller is responsible for acquiring :func:`acquire_vault_write_lock`
    when there may be concurrent writers — this helper deliberately stays
    composable and does not take the lock itself.

    Args:
        vault_path: Path-like pointing at the vault root.
        session_id: Stable identifier for the session. Used for deduplication;
            the helper will not append a second line for the same id on the
            same UTC day.
        cwd: Working directory the session ran in. Optional.
        branch: Git branch name. Optional.
        agent: Agent name (e.g. ``"claude"``, ``"opencode"``). Optional;
            empty string when omitted.
        files_edited: List of file paths edited in the session. Only the
            count is recorded.
        now: Override clock for deterministic tests. Pass a ``datetime``;
            defaults to ``datetime.now(timezone.utc)``.

    Returns:
        Dict with ``fleeting`` (str, relative path to the fleeting file) and
        ``already_logged`` (bool, True when ``session_id`` was already
        present and no line was appended).
    """
    vault = Path(vault_path)
    if now is None:
        moment = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=timezone.utc)
    else:
        moment = now.astimezone(timezone.utc)
    today = moment.strftime("%Y-%m-%d")
    hhmm = moment.strftime("%H:%M")

    def _clean(value, default=""):
        text = str(value if value is not None else default)
        text = " ".join(text.split()).replace("`", "'")
        return text[:300]

    safe_session_id = _clean(session_id, "?")
    safe_cwd = _clean(cwd, "?")
    safe_branch = _clean(branch)
    safe_agent = _clean(agent)

    fleeting_dir = vault / "fleeting"
    fleeting_dir.mkdir(parents=True, exist_ok=True)
    fleeting_file = fleeting_dir / f"{today}.md"

    if not fleeting_file.exists():
        fleeting_file.write_text(f"# {today}\n\n")

    existing = fleeting_file.read_text()
    rel = str(fleeting_file.relative_to(vault))
    if f"`{safe_session_id}`" in existing:
        return {"fleeting": rel, "already_logged": True}

    branch_str = f" ({safe_branch})" if safe_branch else ""
    files_count = f", {len(files_edited)} files" if files_edited else ""
    line = f"- {hhmm} `{safe_session_id}` {safe_cwd}{branch_str} — {safe_agent}{files_count}\n"
    with open(fleeting_file, "a") as f:
        f.write(line)
    return {"fleeting": rel, "already_logged": False}


def _safe_yaml_scalar(value):
    """Sanitize a value for safe YAML frontmatter interpolation.

    Strips newlines, carriage returns, and leading YAML syntax chars
    to prevent frontmatter injection via multi-line or structured values.
    """
    if value is None:
        return ""
    s = str(value)
    # Collapse to single line
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Strip leading YAML block indicators
    s = s.lstrip("-|>")
    return s.strip()


def _tokenize_for_match(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _body_has_related_heading(body):
    """Return True when the body already contains a top-level ``## Related`` heading."""
    return any(line.strip() == "## Related" for line in body.splitlines())


def _find_heading_match(content, heading):
    """Return a match for an exact level-2 heading line."""
    return re.search(rf"^{re.escape(heading)}[ \t]*$", content, re.MULTILINE)


def _has_heading(content, heading):
    """Return True when ``content`` contains ``heading`` as its own heading line."""
    return _find_heading_match(content, heading) is not None


def _append_under_heading(content, heading, line):
    """Insert ``line`` at the end of the ``heading`` section's body.

    The section ends at the next level-2 heading or end-of-file. Trailing
    blank lines inside the section are collapsed before the line is added.
    """
    line = line.rstrip("\n")
    heading_match = _find_heading_match(content, heading)
    if heading_match is None:
        return content.rstrip() + "\n\n" + heading + "\n\n" + line + "\n"

    body_start = heading_match.end()
    next_heading_match = re.search(r"^## [^\n]*$", content[body_start:], re.MULTILINE)
    end = len(content) if next_heading_match is None else body_start + next_heading_match.start()

    section = content[body_start:end].rstrip()
    separator = "\n\n" if not section.strip() else "\n"
    new_section = section + separator + line + "\n"
    return content[:body_start] + new_section + content[end:]


def append_project_session_line(content, line):
    """Append an auto-captured session line to the preferred project section.

    Hubs that have opted into ``## Activity log`` keep auto-captures there.
    Older hubs continue to receive entries under ``## Sessions``.

    MEM-160: this unbounded append is the corruption source identified in
    mem-156-through-166-track-the-retrieval-surface-plan -- it is retired
    at :func:`update_project_index` (that call site no longer invokes this
    function at all; ``## Recent activity`` in a regenerated hub,
    :func:`memento.hub.regenerate_project_hub`, is the bounded replacement).
    This function itself is left unchanged, since ``memento/mcp_server.py``'s
    fleeting-only capture path and ``hooks/memento-triage.py``'s
    ``append_session_to_project`` still call it directly for their own
    session markers -- retiring those call sites is a follow-up, not part of
    this slice.
    """
    if _has_heading(content, "## Activity log"):
        return _append_under_heading(content, "## Activity log", line)
    return _append_under_heading(content, "## Sessions", line)


def find_dedup_candidates(vault_path, title, tags, limit=5):
    """Find notes with title/tag overlap likely to cover the same topic."""
    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.exists():
        return []

    title_tokens = _tokenize_for_match(title)
    tag_tokens = {tag.lower() for tag in tags}
    ranked = []

    for note_path in notes_dir.glob("*.md"):
        try:
            text = note_path.read_text()
        except OSError:
            continue

        title_match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
        note_title = title_match.group(1).strip() if title_match else note_path.stem
        note_tokens = _tokenize_for_match(note_title)
        overlap = len(title_tokens & note_tokens)

        tag_match = re.search(r"^tags:\s*\[([^\]]*)\]", text, re.MULTILINE)
        note_tags = set()
        if tag_match:
            note_tags = {
                token.strip().strip('"').strip("'").lower() for token in tag_match.group(1).split(",") if token.strip()
            }
        overlap += len(tag_tokens & note_tags)

        if overlap > 0:
            ranked.append((overlap, note_path))

    ranked.sort(key=lambda item: (-item[0], item[1].name))
    return [path for _, path in ranked[:limit]]


_CERTAINTY_LABELS = {
    "speculation": 1,
    "speculative": 1,
    "uncertain": 2,
    "low": 2,
    "medium": 3,
    "moderate": 3,
    "likely": 3,
    "confirmed": 4,
    "certain": 4,
    "high": 4,
    "proven": 5,
    "verified": 5,
}

_CANONICAL_NOTE_TYPES = {"decision", "discovery", "pattern", "bugfix", "tool", "architecture"}
_LEGACY_NOTE_TYPE_ALIASES = {
    "debug": "bugfix",
    "debugging": "bugfix",
    "fix": "bugfix",
    "bug-fix": "bugfix",
    "session": "discovery",
}


def _normalize_note_type(note_type):
    """Return the canonical note type used by durable atomic notes."""
    raw = _safe_yaml_scalar(note_type or "discovery").lower().replace("_", "-")
    normalized = _LEGACY_NOTE_TYPE_ALIASES.get(raw, raw)
    if normalized in _CANONICAL_NOTE_TYPES:
        return normalized
    return "discovery"


def _normalize_tags(tags):
    """Return stable, deduped, vocabulary-normalized tags for frontmatter.

    Mechanical normalization only: lowercase, trim, spaces collapsed to
    dashes. Merging near-duplicate tags (plurals, synonyms, casing drift) is
    controlled entirely via the ``tag_aliases`` config map - no stemming
    library - so consolidating the long tail is a config change, not a code
    change (MEM-164).
    """
    try:
        aliases = get_config().get("tag_aliases") or {}
    except Exception:
        aliases = {}
    normalized = []
    seen = set()
    for tag in tags or []:
        safe = _safe_yaml_scalar(tag).strip()
        if not safe:
            continue
        key = re.sub(r"\s+", "-", safe.lower()).strip("-")
        if not key:
            continue
        key = aliases.get(key, key)
        if key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    return normalized


def _looks_like_project_path(value):
    return "/" in value or "\\" in value


def _derive_project_fields(project, project_path=None):
    """Split a raw ``project`` write-path value into ``(slug, raw_path)``.

    Callers historically pass the session cwd verbatim as ``project`` (an
    absolute path). Path-like values are collapsed to a stable repo-name
    slug via ``repo_slug_from_path`` (git toplevel basename, so cross-machine
    paths and per-ticket worktree checkouts of the same repo converge on one
    slug) and the original value is preserved verbatim in the separate
    ``project_path`` field so nothing is lost. Bare tokens (an already-derived
    slug, or a legacy bare branch name on some very old notes) are only
    lightly normalized - lowercased and dash-separated - never reinterpreted
    as a path (MEM-164).
    """
    raw_project = str(project).strip() if project else ""
    raw_path = str(project_path).strip() if project_path else ""

    if not raw_project:
        return None, (_safe_yaml_scalar(raw_path) or None if raw_path else None)

    if _looks_like_project_path(raw_project):
        if not raw_path:
            raw_path = raw_project
        slug = repo_slug_from_path(raw_project) or slugify(Path(raw_project).name) or None
    else:
        slug = slugify(raw_project) or None

    return slug, (_safe_yaml_scalar(raw_path) or None if raw_path else None)


_MAX_CITATION_ANCHOR_CHARS = 120
_MAX_CITATION_COMMIT_CHARS = 40


def _normalize_citation_entry(entry):
    """Return a sanitized ``{file, anchor[, commit]}`` citation dict, or ``None``.

    Citations (MEM-162) are code-fact provenance the retrieval path verifies
    cheaply against a live repo before injection, not something a bad
    LLM/tool response should ever be allowed to block capture over -- an
    entry missing a usable ``file``/``anchor`` is dropped, never raised.
    ``anchor`` is truncated (not dropped) past
    :data:`_MAX_CITATION_ANCHOR_CHARS`: a long anchor is still a valid,
    if less precise, substring to verify against. ``commit`` is optional
    provenance only -- ``anchor`` is the verification key.
    """
    if not isinstance(entry, dict):
        return None
    file_value = _safe_yaml_scalar(entry.get("file")).strip()
    anchor_value = _safe_yaml_scalar(entry.get("anchor")).strip()
    if not file_value or not anchor_value:
        return None
    if len(anchor_value) > _MAX_CITATION_ANCHOR_CHARS:
        anchor_value = anchor_value[:_MAX_CITATION_ANCHOR_CHARS]
    citation = {"file": file_value, "anchor": anchor_value}
    commit_value = _safe_yaml_scalar(entry.get("commit")).strip()
    if commit_value:
        citation["commit"] = commit_value[:_MAX_CITATION_COMMIT_CHARS]
    return citation


def _normalize_citations(citations):
    """Return a sanitized citations list, dropping malformed entries (MEM-162).

    Never raises: a non-list ``citations`` (a bad LLM response shape) returns
    ``[]`` rather than iterating character-by-character over a string.
    """
    if not isinstance(citations, list):
        return []
    normalized = []
    for entry in citations:
        cleaned = _normalize_citation_entry(entry)
        if cleaned:
            normalized.append(cleaned)
    return normalized


def normalize_note_contract(
    *,
    note_type="discovery",
    tags=None,
    certainty=None,
    source="session",
    origin=None,
    validity_context=None,
    supersedes=None,
    project=None,
    project_path=None,
    branch=None,
    session_id=None,
    citations=None,
):
    """Normalize metadata to the shared durable-note contract.

    All capture/write paths should pass through this adapter before frontmatter
    is written so Claude triage, Pi capture, Pi curation, and MCP writes use the
    same typed schema. Legacy `type: session` inputs are accepted and written as
    typed discoveries; existing legacy notes are handled in retrieval metadata.

    ``project`` is normalized to a stable slug (MEM-164): path-like values are
    collapsed via ``repo_slug_from_path`` and the original raw value is kept
    verbatim in ``project_path`` so cross-machine/worktree paths never leak
    into the field retrieval filtering compares on.

    ``citations`` (MEM-162) is an optional list of ``{file, anchor[, commit]}``
    dicts asserting a note's claim against specific code; see
    :func:`_normalize_citations` for the defensive parsing rules.
    """
    project_slug, project_path_value = _derive_project_fields(project, project_path)
    return {
        "note_type": _normalize_note_type(note_type),
        "tags": _normalize_tags(tags),
        "certainty": _coerce_certainty(certainty),
        "source": _safe_yaml_scalar(source or "session") or "session",
        "origin": _safe_yaml_scalar(origin) or None,
        "validity_context": _safe_yaml_scalar(validity_context) or None,
        "supersedes": _safe_yaml_scalar(supersedes) or None,
        "project": project_slug,
        "project_path": project_path_value,
        "branch": _safe_yaml_scalar(branch) or None,
        "session_id": _safe_yaml_scalar(session_id) or None,
        "citations": _normalize_citations(citations),
    }


def _coerce_certainty(certainty):
    """Return a schema-valid certainty int, or None for unusable input.

    Out-of-range integers (e.g. a `95`/`97` typo meant to be `5`) are clamped
    into 1-5 with a logged warning rather than dropped (MEM-150) -- capture
    must never hard-fail a write over a bad certainty value. Genuinely
    unusable input (missing, empty, unparseable) still returns None, same as
    before.
    """
    if certainty is None or certainty == "":
        return None
    if isinstance(certainty, str):
        label = certainty.strip().lower()
        if label in _CERTAINTY_LABELS:
            return _CERTAINTY_LABELS[label]
        certainty = label
    try:
        value = int(certainty)
    except (TypeError, ValueError):
        return None
    if 1 <= value <= 5:
        return value
    clamped = max(1, min(5, value))
    import sys as _sys

    print(f"[memento] warning: certainty {value} out of range 1-5, clamped to {clamped}", file=_sys.stderr)
    return clamped


def _render_note_markdown(
    title,
    body,
    note_type,
    tags,
    certainty=None,
    source="session",
    origin=None,
    validity_context=None,
    supersedes=None,
    project=None,
    project_path=None,
    branch=None,
    session_id=None,
    citations=None,
    extra_frontmatter_lines=None,
):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    # Sanitize and normalize all contract fields to prevent frontmatter injection.
    safe_title = _safe_yaml_scalar(title)
    contract = normalize_note_contract(
        note_type=note_type,
        tags=tags,
        certainty=certainty,
        source=source,
        origin=origin,
        validity_context=validity_context,
        supersedes=supersedes,
        project=project,
        project_path=project_path,
        branch=branch,
        session_id=session_id,
        citations=citations,
    )

    lines = [
        "---",
        f"title: {safe_title}",
        f"type: {contract['note_type']}",
        f"tags: {json.dumps(contract['tags'], ensure_ascii=False)}",
        f"source: {contract['source']}",
    ]
    if contract["origin"]:
        lines.append(f"origin: {contract['origin']}")
    if contract["certainty"] is not None:
        lines.append(f"certainty: {contract['certainty']}")
    if contract["validity_context"]:
        lines.append(f"validity-context: {contract['validity_context']}")
    if contract["supersedes"]:
        lines.append(f"supersedes: {json.dumps(contract['supersedes'], ensure_ascii=False)}")
    if contract["citations"]:
        lines.append(f"citations: {json.dumps(contract['citations'], ensure_ascii=False)}")
    if contract["project"]:
        lines.append(f"project: {contract['project']}")
    if contract["project_path"]:
        lines.append(f"project_path: {contract['project_path']}")
    if contract["branch"]:
        lines.append(f"branch: {contract['branch']}")
    lines.append(f"date: {now}")
    if contract["session_id"]:
        lines.append(f"session_id: {contract['session_id']}")
    # Verbatim round-trip of frontmatter keys this renderer does not manage
    # (rewrite paths pass the existing note's unmanaged lines through).
    lines.extend(extra_frontmatter_lines or [])

    # Append the canonical "## Related" placeholder only if the body doesn't
    # already contain one — otherwise callers that include their own ## Related
    # section produce duplicate (often empty) headers.
    body_stripped = body.strip()
    if _body_has_related_heading(body_stripped):
        lines.extend(["---", "", body_stripped, ""])
    else:
        lines.extend(["---", "", body_stripped, "", "## Related", ""])
    return "\n".join(lines)


def _index_written_note(vault_path, target):
    try:
        from memento.search_backend import get_backend

        backend = get_backend()
        rel_path = str(target.relative_to(Path(vault_path)))
        if hasattr(backend, "index_note"):
            backend.index_note(rel_path)
        else:
            backend.reindex("memento", embed=False)
    except Exception:
        pass  # Indexing failure must not block note storage


def _write_text_atomic(target, text):
    """Write ``text`` to ``target`` via a unique same-directory tmp file plus atomic rename.

    The tmp name embeds a random per-writer component (``tempfile.mkstemp``) so
    concurrent writers aimed at the same target can never clobber or steal each
    other's in-flight tmp file, which slug-derived tmp names allowed (audit M6).
    """
    target = Path(target)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".tmp-{target.stem}-", suffix=target.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# split_frontmatter() and the _frontmatter_*() typed accessors below are thin
# delegating wrappers over memento.frontmatter (MEM-166 consolidated the
# hand-rolled parsers that used to live here, in memento.graph, memento.query,
# and memento.contradictions into that one shared module). Re-exported here
# under their original names so every existing call site --
# `from memento.store import split_frontmatter`, `memento.store._frontmatter_scalar`,
# etc. across memento/ and hooks/ -- keeps working unchanged.
split_frontmatter = frontmatter_module.split_frontmatter


# Frontmatter keys owned by _render_note_markdown. Anything else found on an
# existing note must round-trip rewrites unchanged.
#
# ``citations`` (MEM-162) is deliberately NOT in this set even though
# _render_note_markdown writes it at create time: replace_note_at_path (the
# only rewrite path) does not take a ``citations`` argument, so if it were
# "managed" here a rewrite that doesn't pass citations would silently drop an
# existing note's citations line. Leaving it unmanaged means
# _unmanaged_frontmatter_lines() round-trips it like a hand-added key on
# every rewrite -- the same reasoning _RESURFACING_FRONTMATTER_KEYS documents
# below for resurfaced_count/last_resurfaced.
_MANAGED_NOTE_FRONTMATTER_KEYS = {
    "title",
    "type",
    "tags",
    "source",
    "origin",
    "certainty",
    "validity-context",
    "supersedes",
    "project",
    "project_path",
    "branch",
    "date",
    "session_id",
}

# Keys owned by fold_access_log_into_frontmatter(). Kept deliberately out of
# _MANAGED_NOTE_FRONTMATTER_KEYS: from the write path's point of view these
# are just another unmanaged key it round-trips unchanged, which is exactly
# what should happen when e.g. an MCP edit rewrites a note's title/body —
# the resurfacing signal must survive that rewrite untouched.
_RESURFACING_FRONTMATTER_KEYS = {"resurfaced_count", "last_resurfaced"}

# Key owned by fold_stale_citations_into_frontmatter() (MEM-162). Same
# round-trip reasoning as _RESURFACING_FRONTMATTER_KEYS above.
_CITATION_FRONTMATTER_KEYS = {"citation_stale"}


def _frontmatter_scalar(frontmatter, key):
    """Thin delegating wrapper -- see :func:`memento.frontmatter.get_scalar` (MEM-166)."""
    return frontmatter_module.get_scalar(frontmatter, key)


def _frontmatter_int(frontmatter, key):
    """Thin delegating wrapper -- see :func:`memento.frontmatter.get_int` (MEM-166)."""
    return frontmatter_module.get_int(frontmatter, key)


def _frontmatter_bool(frontmatter, key):
    """Thin delegating wrapper -- see :func:`memento.frontmatter.get_bool` (MEM-166)."""
    return frontmatter_module.get_bool(frontmatter, key)


def _unmanaged_frontmatter_lines(frontmatter, managed_keys=None):
    """Return raw frontmatter lines for keys ``managed_keys`` does not cover.

    Thin delegating wrapper over :func:`memento.frontmatter.unmanaged_lines`
    (MEM-166) that keeps this module's historical default: ``managed_keys``
    defaults to ``_MANAGED_NOTE_FRONTMATTER_KEYS`` (the write-path contract
    fields) — this is what ``replace_note_at_path`` uses to round-trip
    unknown keys on rewrite. ``fold_access_log_into_frontmatter`` instead
    passes ``_RESURFACING_FRONTMATTER_KEYS`` so it can drop just the two
    resurfacing lines it is about to rewrite while preserving every other
    key — managed or not — unchanged.

    Each preserved ``key:`` line is kept verbatim together with its indented
    continuation lines so rewrites round-trip multi-line values unchanged.
    """
    if managed_keys is None:
        managed_keys = _MANAGED_NOTE_FRONTMATTER_KEYS
    return frontmatter_module.unmanaged_lines(frontmatter, managed_keys)


def write_note(
    vault_path,
    title,
    body,
    note_type,
    tags,
    certainty=None,
    source="session",
    origin=None,
    validity_context=None,
    supersedes=None,
    project=None,
    project_path=None,
    branch=None,
    session_id=None,
    citations=None,
):
    """Write an atomic note with normalized frontmatter to notes/ using an atomic rename.

    ``citations`` (MEM-162) is an optional list of ``{file, anchor[, commit]}``
    dicts written once at create time; citations accrue on new notes only
    (no backfill). See :func:`_normalize_citations` for how malformed entries
    are dropped without failing the write.
    """
    notes_dir = Path(vault_path) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    slug = slugify(title)
    target = notes_dir / f"{slug}.md"
    if target.exists():
        # Avoid overwriting — append a numeric suffix
        for i in range(2, 100):
            candidate = notes_dir / f"{slug}-{i}.md"
            if not candidate.exists():
                target = candidate
                break
    _write_text_atomic(
        target,
        _render_note_markdown(
            title,
            body,
            note_type,
            tags,
            certainty=certainty,
            source=source,
            origin=origin,
            validity_context=validity_context,
            supersedes=supersedes,
            project=project,
            project_path=project_path,
            branch=branch,
            citations=citations,
            session_id=session_id,
        ),
    )
    _index_written_note(vault_path, target)
    return target


def replace_note_at_path(
    vault_path,
    path,
    title,
    body,
    note_type,
    tags,
    certainty=None,
    source="mcp",
    origin=None,
    validity_context=None,
    supersedes=None,
    project=None,
    project_path=None,
    branch=None,
    session_id=None,
):
    """Replace an existing note at a vault-relative path using managed frontmatter."""
    vault = Path(vault_path).resolve()
    target = (vault / path).resolve()
    if target != vault and vault not in target.parents:
        raise ValueError("Invalid path: traversal outside vault")
    try:
        rel = target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("Invalid path: traversal outside vault") from exc
    if not rel.parts or rel.parts[0] != "notes" or target.suffix != ".md":
        raise ValueError("Invalid path: replacement must target notes/*.md")
    if not target.exists():
        raise FileNotFoundError(str(rel))

    # Round-trip frontmatter keys this write path does not manage (audit M6):
    # sync rewrites must not drop keys added by hand or by other tools.
    existing_frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8", errors="replace"))
    preserved_lines = _unmanaged_frontmatter_lines(existing_frontmatter)

    _write_text_atomic(
        target,
        _render_note_markdown(
            title,
            body,
            note_type,
            tags,
            certainty=certainty,
            source=source,
            origin=origin,
            validity_context=validity_context,
            supersedes=supersedes,
            project=project,
            project_path=project_path,
            branch=branch,
            session_id=session_id,
            extra_frontmatter_lines=preserved_lines,
        ),
    )
    _index_written_note(vault_path, target)
    return target


def _read_vault_access_log_entries(vault_id, log_path=None):
    """Read ordered, parsed access-log entries for one vault, oldest first.

    Unlike :func:`_events_from_raw_access_log` (which buckets by path and
    caps each bucket to the boost's decay window), this keeps a single flat,
    file-order list so the fold cursor in
    :func:`fold_access_log_into_frontmatter` can tell exactly how many
    entries at a given timestamp have already been folded.
    """
    entries = []
    try:
        with open(log_path or ACCESS_LOG_PATH) as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("vault_id") or "") != vault_id:
                    continue
                path = str(entry.get("path") or "").strip()
                if not path:
                    continue
                ts_raw = entry.get("ts")
                ts = _parse_access_log_ts(ts_raw)
                if ts is None:
                    continue
                entries.append({"path": path, "ts": ts, "ts_raw": str(ts_raw)})
    except OSError:
        return []
    return entries


def _fold_note_frontmatter(note_path, new_events, last_ts):
    """Merge newly delivered access events into one note's frontmatter.

    Increments ``resurfaced_count`` by ``new_events`` and advances
    ``last_resurfaced`` to the max of the existing value and ``last_ts``. All
    other frontmatter lines — managed or not — round-trip unchanged, via the
    same ``_unmanaged_frontmatter_lines`` helper ``replace_note_at_path`` uses
    to preserve unknown keys; this only ever touches the two resurfacing
    lines. Returns True when the note was actually rewritten.
    """
    text = note_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return False  # No frontmatter block -- nothing safe to fold into.

    existing_count = _frontmatter_int(frontmatter, "resurfaced_count") or 0
    existing_last = _parse_access_log_ts(_frontmatter_scalar(frontmatter, "last_resurfaced"))

    new_count = existing_count + new_events
    new_last = last_ts if existing_last is None or last_ts > existing_last else existing_last
    new_last_raw = new_last.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    preserved = _unmanaged_frontmatter_lines(frontmatter, managed_keys=_RESURFACING_FRONTMATTER_KEYS)
    new_frontmatter = "\n".join([*preserved, f"resurfaced_count: {new_count}", f"last_resurfaced: {new_last_raw}"])
    new_text = f"---\n{new_frontmatter}\n---\n{body}"
    if new_text == text:
        return False
    _write_text_atomic(note_path, new_text)
    return True


def fold_access_log_into_frontmatter(vault_path, *, lock_file=None):
    """Fold the derived access-log (write-ahead buffer) into note frontmatter.

    The access log lives under the runtime dir so cache cleaners can purge it
    freely, but that means the ``access_log_half_life_days`` boost never
    accumulates real history and never crosses machines (MEM-148). This fold
    makes each note's frontmatter (``resurfaced_count``, ``last_resurfaced``)
    the durable source of truth: it aggregates newly delivered events per
    note path since the last fold and rewrites only the touched notes, under
    the vault write lock, using the same atomic tmp+rename write as the rest
    of this module.

    Idempotent: a per-vault fold cursor (timestamp plus a same-timestamp
    tie-breaker count) is persisted in ``ACCESS_LOG_STATS_PATH`` so re-running
    the fold with no new access-log entries is a no-op, and entries that
    share a timestamp with the cursor are never double-counted.

    Only call this from a periodic trigger (e.g. hooks/memento-sweeper.py's
    ``main()``) — never inline on every ``record_access``, which would
    rewrite notes (and dirty git) on every recall.

    Re-entrant like ``write_smart_store_note``: acquires the vault write lock
    only if not already held by this process, and never releases a lock a
    caller holds.

    Like ``write_note``/``replace_note_at_path``, ``vault_path`` is a required
    positional argument — the caller resolves it (e.g. from config), this
    function does not fall back to ``get_config()`` for it. Vault *identity*
    (for matching this vault's access-log entries) still comes from the
    ambient config via ``_current_vault_id()``, consistent with
    ``record_access``/``apply_access_log_boost`` elsewhere in this module.

    Returns a dict with ``folded_notes`` (int notes touched), ``new_events``
    (int access-log entries folded), and ``error`` (str, only on lock
    contention).
    """
    vault = Path(vault_path).expanduser().resolve()

    vault_id = _current_vault_id()
    entries = _read_vault_access_log_entries(vault_id)
    if not entries:
        return {"folded_notes": 0, "new_events": 0}

    already_held = owns_vault_write_lock(lock_file)
    if not already_held and not acquire_vault_write_lock(lock_file=lock_file):
        return {"folded_notes": 0, "new_events": 0, "error": "lock_unavailable"}

    try:
        data = _read_access_log_stats_file()
        vaults = data.setdefault("vaults", {})
        vault_entry = vaults.setdefault(vault_id, {"paths": {}, "updated_at": None})
        cursor = vault_entry.get("fold_cursor") or {}
        cursor_ts = _parse_access_log_ts(cursor.get("ts"))
        cursor_count_at_ts = int(cursor.get("count_at_ts") or 0)

        new_entries = []
        seen_at_cursor = 0
        for entry in entries:
            if cursor_ts is not None and entry["ts"] < cursor_ts:
                continue
            if cursor_ts is not None and entry["ts"] == cursor_ts:
                seen_at_cursor += 1
                if seen_at_cursor <= cursor_count_at_ts:
                    continue
            new_entries.append(entry)

        if not new_entries:
            return {"folded_notes": 0, "new_events": 0}

        per_path = {}
        for entry in new_entries:
            bucket = per_path.setdefault(entry["path"], {"count": 0, "last_ts": entry["ts"]})
            bucket["count"] += 1
            if entry["ts"] >= bucket["last_ts"]:
                bucket["last_ts"] = entry["ts"]

        folded = 0
        for rel_path, agg in per_path.items():
            try:
                note_path = (vault / rel_path).resolve()
                note_path.relative_to(vault)
            except (OSError, ValueError):
                continue
            if not note_path.is_file():
                continue
            try:
                if _fold_note_frontmatter(note_path, agg["count"], agg["last_ts"]):
                    folded += 1
            except OSError:
                continue

        last_entry = entries[-1]
        vault_entry["fold_cursor"] = {
            "ts": last_entry["ts_raw"],
            "count_at_ts": sum(1 for entry in entries if entry["ts"] == last_entry["ts"]),
        }
        vault_entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_access_log_stats_file(data)

        return {"folded_notes": folded, "new_events": len(new_entries)}
    finally:
        if not already_held:
            release_vault_write_lock(lock_file=lock_file)


def append_stale_citation_review(note_path, citations, *, queue_path=None, checked_at=None):
    """Queue one note for citation-staleness supersession review (MEM-162).

    Called from the inject-time verification path
    (``memento.retrieval_policy``/the tool-context path) when a selected
    note's cited anchor no longer appears in its file. Never rewrites the
    note inline -- this only appends a JSONL line to a runtime-dir queue;
    :func:`fold_stale_citations_into_frontmatter` (run periodically from
    ``hooks/memento-sweeper.py``) is the only writer that turns queued
    entries into durable ``citation_stale: true`` frontmatter.

    Best-effort like ``log_retrieval``: a write failure warns once (via
    ``_append_jsonl``) and never raises, so a broken runtime dir can never
    block recall/tool-context injection.
    """
    entry = {
        "note_path": str(note_path),
        "citations": citations,
        "checked_at": checked_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _append_jsonl(queue_path or STALE_CITATIONS_QUEUE_PATH, entry, "_stale_citation_warned")


def _mark_citation_stale(note_path):
    """Set ``citation_stale: true`` on one note's frontmatter, idempotently.

    Round-trips every other frontmatter line unchanged (same
    ``_unmanaged_frontmatter_lines`` helper the MEM-148 fold uses) and only
    ever touches this one key. Returns False (no rewrite performed) when the
    note has no frontmatter block or is already flagged.
    """
    text = note_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    if not frontmatter:
        return False
    if _frontmatter_bool(frontmatter, "citation_stale"):
        return False  # already flagged -- idempotent no-op
    preserved = _unmanaged_frontmatter_lines(frontmatter, managed_keys=_CITATION_FRONTMATTER_KEYS)
    new_frontmatter = "\n".join([*preserved, "citation_stale: true"])
    new_text = f"---\n{new_frontmatter}\n---\n{body}"
    _write_text_atomic(note_path, new_text)
    return True


def fold_stale_citations_into_frontmatter(vault_path, *, queue_path=None, lock_file=None):
    """Fold queued citation-staleness flags into note frontmatter (MEM-162).

    Verify-at-use appends one JSONL record per stale citation encountered to
    a runtime-dir review queue (:data:`STALE_CITATIONS_QUEUE_PATH`) instead
    of rewriting the note on the hot recall/tool-context injection path.
    This periodic fold is the only writer that turns that queue into durable
    frontmatter (``citation_stale: true``) -- the supersession-review signal
    consumed downstream by MEM-152's archive sweep / MEM-163's supersession
    review, never applied inline.

    Same failure-isolated, lock-scoped pattern as
    ``fold_access_log_into_frontmatter``: re-entrant vault write lock, atomic
    tmp+rename note rewrites, one bad note path never blocks the rest.

    Idempotent: the queue file is drained (read, then truncated) up front,
    so concurrent appends made *during* the fold land in a fresh empty file
    rather than being lost, and notes already marked ``citation_stale: true``
    are simply left unchanged (:func:`_mark_citation_stale` skips the
    rewrite). Re-running with an empty/absent queue is a no-op.

    Returns a dict with ``folded_notes`` (int notes newly flagged),
    ``queued_events`` (int queue lines processed), and ``error`` (str, only
    on lock contention).
    """
    vault = Path(vault_path).expanduser().resolve()
    queue_file = Path(queue_path) if queue_path else Path(STALE_CITATIONS_QUEUE_PATH)

    if not queue_file.exists():
        return {"folded_notes": 0, "queued_events": 0}

    already_held = owns_vault_write_lock(lock_file)
    if not already_held and not acquire_vault_write_lock(lock_file=lock_file):
        return {"folded_notes": 0, "queued_events": 0, "error": "lock_unavailable"}

    try:
        try:
            drained_text = queue_file.read_text(encoding="utf-8")
        except OSError:
            return {"folded_notes": 0, "queued_events": 0}
        try:
            queue_file.write_text("", encoding="utf-8")
        except OSError:
            pass

        rel_paths = set()
        queued_events = 0
        for line in drained_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel_path = str(record.get("note_path") or "").strip()
            if not rel_path:
                continue
            queued_events += 1
            rel_paths.add(rel_path)

        folded = 0
        for rel_path in rel_paths:
            try:
                note_path = (vault / rel_path).resolve()
                note_path.relative_to(vault)
            except (OSError, ValueError):
                continue
            if not note_path.is_file():
                continue
            try:
                if _mark_citation_stale(note_path):
                    folded += 1
            except OSError:
                continue

        return {"folded_notes": folded, "queued_events": queued_events}
    finally:
        if not already_held:
            release_vault_write_lock(lock_file=lock_file)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPO_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DAILY_VERSION_RE = re.compile(r"^daily-\d{4}-\d{2}-\d{2}-[a-z0-9_-]+-v(\d+)\.md$")

_MANAGED_DAILY_KEYS = {
    "title",
    "type",
    "tags",
    "source",
    "certainty",
    "date",
    "repo_slug",
    "supersedes",
}


def _next_daily_version(notes_dir, base_slug):
    """Return the next version number for a daily snapshot supersede chain."""
    highest = 1
    prefix = f"{base_slug}-v"
    for existing in notes_dir.glob(f"{base_slug}-v*.md"):
        match = _DAILY_VERSION_RE.match(existing.name)
        if not match:
            continue
        if not existing.name.startswith(prefix):
            continue
        n = int(match.group(1))
        if n > highest:
            highest = n
    return highest + 1


def write_daily_snapshot(
    vault_path,
    date,
    repo_slug,
    content,
    frontmatter_extra=None,
    supersede=False,
):
    """Write a structured per-repo daily snapshot into notes/.

    Returns a dict with keys:
        path: str, relative to vault_path
        supersedes: str | None (title of the superseded note)
        version: int (1 for first write, n for v<n>)

    Or an error dict with 'error' and 'reason' keys.
    """
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return {"error": "date must be YYYY-MM-DD", "reason": "invalid_date"}
    if not isinstance(repo_slug, str) or not _REPO_SLUG_RE.match(repo_slug):
        return {
            "error": "repo_slug must match [a-z0-9][a-z0-9_-]*",
            "reason": "invalid_repo_slug",
        }
    if not content or not content.strip():
        return {"error": "content is required", "reason": "empty_content"}

    notes_dir = Path(vault_path) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    base_slug = f"daily-{date}-{repo_slug}"
    base_file = notes_dir / f"{base_slug}.md"

    supersedes_title = None
    version = 1
    if base_file.exists():
        if not supersede:
            return {
                "error": f"daily snapshot already exists for {date} {repo_slug}",
                "reason": "already_exists",
                "existing_path": str(base_file.relative_to(Path(vault_path))),
            }
        version = _next_daily_version(notes_dir, base_slug)
        target = notes_dir / f"{base_slug}-v{version}.md"
        supersedes_title = base_slug
    else:
        target = base_file

    # Sanitize body before writing
    from memento.utils import sanitize_secrets

    sanitized = sanitize_secrets(content)

    extras = dict(frontmatter_extra or {})
    for key in _MANAGED_DAILY_KEYS:
        extras.pop(key, None)

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    safe_repo = _safe_yaml_scalar(repo_slug)

    lines = [
        "---",
        f"title: Daily {date} {safe_repo}",
        "type: daily",
        f"tags: [daily, {safe_repo}]",
        "source: orra",
        "certainty: 2",
        f"date: {now_ts}",
        f"repo_slug: {safe_repo}",
    ]
    if supersedes_title:
        lines.append(f'supersedes: "[[{_safe_yaml_scalar(supersedes_title)}]]"')

    for key, value in extras.items():
        if value is None:
            continue
        safe_key = _safe_yaml_scalar(key)
        if not safe_key:
            continue
        if isinstance(value, list):
            safe_items = [_safe_yaml_scalar(v) for v in value]
            lines.append(f"{safe_key}: [{', '.join(safe_items)}]")
        else:
            lines.append(f"{safe_key}: {_safe_yaml_scalar(value)}")

    body = sanitized.strip()
    if _body_has_related_heading(body):
        lines.extend(["---", "", body, ""])
    else:
        lines.extend(["---", "", body, "", "## Related", ""])

    _write_text_atomic(target, "\n".join(lines))

    try:
        from memento.search_backend import get_backend
        from memento.embedded_search import EmbeddedSearchBackend

        backend = get_backend()
        if isinstance(backend, EmbeddedSearchBackend):
            rel_path = str(target.relative_to(Path(vault_path)))
            backend.index_note(rel_path)
    except Exception:
        pass

    return {
        "path": str(target.relative_to(Path(vault_path))),
        "supersedes": supersedes_title,
        "version": version,
    }


def update_project_index(vault_path, project_slug, note_name, session_summary):
    """Ensure a project index exists and record a ``[[note_name]]`` link under ``## Notes``.

    MEM-160: this used to also hand-append a free-text session-summary line
    (formerly under ``## Sessions``, later ``## Activity log`` --
    :func:`append_project_session_line`) on every MCP store/replace/capture.
    That unbounded, ever-growing append -- with no cap and no structural
    guarantee across format drift -- is what corrupted real hubs into
    multi-hundred-line files with duplicate headers, truncated entries, and
    stray fragments. This call site's session-summary append is retired
    outright (``session_summary`` is accepted only for call-site
    compatibility with ``memento/mcp_server.py``, ``memento/smart_store.py``,
    and ``memento/dedup_merge.py``, and is otherwise unused): the bounded
    replacement is :func:`memento.hub.regenerate_project_hub`'s ``## Recent
    activity`` section, mechanically derived from note frontmatter dates on
    every regeneration rather than hand-maintained here.

    ``memento/mcp_server.py``'s fleeting-only capture path and
    ``hooks/memento-triage.py``'s ``append_session_to_project`` still call
    :func:`append_project_session_line` directly for their own session
    markers -- that call path is unchanged and out of scope for this retirement.
    """
    project_dir = Path(vault_path) / "projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_dir / f"{project_slug}.md"

    if project_file.exists():
        content = project_file.read_text()
    else:
        content = "\n".join(
            [
                "---",
                f"title: {project_slug}",
                f"project: {project_slug}",
                "---",
                "",
                "## Notes",
                "",
            ]
        )

    note_line = f"- [[{note_name}]]"
    if note_line not in content:
        if _has_heading(content, "## Notes"):
            content = _append_under_heading(content, "## Notes", note_line)
        else:
            content = content.rstrip() + "\n\n## Notes\n\n" + note_line + "\n"

    _write_text_atomic(project_file, content)
