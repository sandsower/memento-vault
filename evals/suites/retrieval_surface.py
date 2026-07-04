"""Retrieval surface evals: day-to-day recall UX, local CLI, and backend contracts.

These checks are deterministic and read-only. They complement retrieval_accuracy:
that suite grades answer quality, while this suite guards the user-facing paths
that must route agents into the production retrieval/indexing machinery even
when MCP is unavailable.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from evals.common import CheckResult, FAIL, PASS
from memento.retrieval_policy import ExplicitSearchRequest, ExplicitSearchRuntime, normalized_natural_query
from memento.search_backend import GrepBackend, QMDBackend, SearchBackend

SUITE = "retrieval_surface"
REPO_ROOT = Path(__file__).resolve().parents[2]
CONCIERGE = REPO_ROOT / "skills" / "generic" / "concierge" / "SKILL.md"


def _check(ok: bool, check_id: str, title: str, details: list[str] | None = None, remediation: str = ""):
    return CheckResult(
        id=f"{SUITE}.{check_id}",
        suite=SUITE,
        title=title,
        status=PASS if ok else FAIL,
        details=details or [],
        remediation=remediation,
    )


def _concierge_text() -> str:
    try:
        return CONCIERGE.read_text(encoding="utf-8").lower()
    except OSError:
        return ""


def _search_metadata_probe() -> tuple[bool, dict]:
    vault = REPO_ROOT / "evals" / "golden" / "fixtures" / "vault"

    runtime = ExplicitSearchRuntime(
        vault_loader=lambda: vault,
        has_backend=lambda: True,
        qmd_search=lambda *_args, **_kwargs: [
            {"path": "notes/redis.md", "title": "Redis", "score": 0.9, "snippet": "ttl"}
        ],
        enhance_results=lambda results, **_kwargs: results,
    )
    payload = runtime.search(ExplicitSearchRequest(query="redis cache", limit=1))
    metadata = payload.get("metadata", {})
    required = {"backend", "backend_index", "semantic_used", "concrete_enabled"}
    return required.issubset(metadata), metadata


class _EvalBackend(SearchBackend):
    def __init__(self):
        self.reindex_calls = []

    def is_available(self):
        return True

    def search(self, query, collection, limit=5, semantic=False, timeout=10, min_score=0.0, concrete=False):
        return []

    def get(self, path, collection=None, timeout=5):
        return None

    def reindex(self, collection, embed=True):
        self.reindex_calls.append((collection, embed))
        return True


def _backend_index_hook_probe() -> tuple[bool, list[str]]:
    details = []
    generic = _EvalBackend()
    details.append(f"generic={generic.index_note('notes/a.md', collection='memento')} calls={generic.reindex_calls}")

    grep = GrepBackend()
    details.append(f"grep={grep.index_note('notes/a.md', collection='memento')}")

    qmd = QMDBackend()
    qmd_calls = []
    qmd.reindex = lambda collection, embed=True: qmd_calls.append((collection, embed)) or True  # type: ignore[method-assign]
    details.append(f"qmd={qmd.index_note('notes/a.md', collection='memento')} calls={qmd_calls}")

    embedded_ok = False
    try:
        from memento.embedded_search import EmbeddedSearchBackend

        with tempfile.TemporaryDirectory(prefix="memento-eval-embedded-") as tmp:
            vault = Path(tmp) / "vault"
            notes = vault / "notes"
            notes.mkdir(parents=True)
            (notes / "a.md").write_text("---\ntitle: A\n---\n\nalpha beta")
            backend = EmbeddedSearchBackend(vault_path=vault, db_path=vault / ".search" / "search.db")
            embedded_ok = backend.index_note("notes/a.md")
            details.append(f"embedded={embedded_ok}")
    except Exception as exc:
        details.append(f"embedded=error:{type(exc).__name__}:{exc}")

    ok = (
        generic.reindex_calls == [("memento", False)]
        and grep.index_note("notes/b.md") is True
        and qmd_calls == [("memento", False)]
        and embedded_ok
    )
    return ok, details


def run(context) -> list[CheckResult]:
    results: list[CheckResult] = []
    text = _concierge_text()

    trigger_phrases = [
        "do you remember",
        "have we seen",
        "what did we decide",
        "what did we learn",
        "where was this implemented",
        "find prior context",
        "check memory",
        "search the vault",
    ]
    missing_triggers = [phrase for phrase in trigger_phrases if phrase not in text]
    results.append(
        _check(
            not missing_triggers,
            "concierge_day_to_day_triggers",
            "Concierge skill advertises day-to-day recall trigger phrases",
            details=[f"missing: {', '.join(missing_triggers)}"]
            if missing_triggers
            else ["all expected triggers present"],
            remediation="Broaden skills/generic/concierge/SKILL.md so agents route ordinary recall questions through memory.",
        )
    )

    fallback_needles = [
        "memento-vault search",
        "python3 -m memento search",
        "memento-vault recall",
        "grep",
        "last-resort",
    ]
    missing_fallback = [needle for needle in fallback_needles if needle not in text]
    results.append(
        _check(
            not missing_fallback,
            "concierge_non_mcp_fallback",
            "Concierge skill has a production local fallback before grep",
            details=[f"missing: {', '.join(missing_fallback)}"]
            if missing_fallback
            else ["local CLI fallback documented"],
            remediation="Route non-MCP users through the local production search/recall CLI before QMD or grep.",
        )
    )

    variant_examples = {
        "how should we store bearer tokens that appear in URLs": "store bearer token url",
        "how to publish an mcp server to the registry": "publish mcp server registry",
        "memento installer remembering flags between upgrades": "memento installer remember flag upgrade",
    }
    bad_variants = [
        f"{query!r} -> {normalized_natural_query(query)!r} (expected {expected!r})"
        for query, expected in variant_examples.items()
        if normalized_natural_query(query) != expected
    ]
    results.append(
        _check(
            not bad_variants,
            "natural_query_normalization",
            "Natural question prompts normalize into durable lexical search terms",
            details=bad_variants or ["all variants matched"],
            remediation="Update normalized_natural_query() so production recall can use lexical backends before semantic fallback.",
        )
    )

    ok_metadata, metadata = _search_metadata_probe()
    results.append(
        _check(
            ok_metadata,
            "search_metadata_backend_contract",
            "Explicit search envelopes expose backend/index/semantic/concrete metadata",
            details=[str(metadata)],
            remediation="Search envelopes must tell agents whether they used indexed search or a degraded fallback.",
        )
    )

    ok_hooks, hook_details = _backend_index_hook_probe()
    results.append(
        _check(
            ok_hooks,
            "backend_index_hooks_cover_all_backends",
            "Official write indexing hook is available across generic, QMD, Embedded, and Grep backends",
            details=hook_details,
            remediation="All official write paths should notify the active backend, even when it must fall back to a conservative collection update or no-op grep visibility.",
        )
    )

    proc = subprocess.run(
        [sys.executable, "-m", "memento", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=20,
    )
    help_text = (proc.stdout + proc.stderr).lower()
    results.append(
        _check(
            proc.returncode == 0 and "search|recall|reindex" in help_text,
            "local_cli_retrieval_help",
            "Local python -m memento exposes search/recall/reindex subcommands",
            details=[help_text.strip()[:300]],
            remediation="Keep a non-MCP retrieval surface available for hookless/tool-limited agents.",
        )
    )

    return results
