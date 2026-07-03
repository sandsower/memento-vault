"""Retrieval probe: runs REAL memento retrieval code against either a
hermetic fixture vault (--mode fixture) or the live vault (--mode live).

Run as a subprocess by evals/suites/retrieval_accuracy.py so that
MEMENTO_VAULT_PATH / MEMENTO_SEARCH_BACKEND overrides never leak into the
parent process. Prints one JSON document to stdout.

Fixture mode is fully deterministic: it renders the template vault in
evals/golden/fixtures/vault into a temp dir (substituting date
placeholders), forces the grep backend, and asserts ranking-policy
behavior with the actual functions from memento.search.

Checks marked known_gap=true encode DESIRED behavior the system does not
implement yet. They are reported separately so they inform instead of
alarm; when one starts passing, promote it to a normal check.

Golden ranked-order checks (MEM-133)
-------------------------------------
`fixture_checks()` above is pairwise: it proves individual signals move
scores in the right direction, but it never proves a real query's top-5
*order* is what it was yesterday. A fusion/reranker weight change can
silently reorder real results without failing any pairwise assertion.

`ranked_order_checks()` closes that gap: for a fixed set of queries it runs
the real FTS5/BM25 (`memento.embedded_search.EmbeddedSearchBackend`, no
embedding provider so no vector index and no ONNX/network dependency) +
PRF query expansion + RRF fusion + policy (`memento.search.enhance_results`)
path against the deterministic fixture vault, with every tunable parameter
(RRF k, PRF term/doc counts, temporal decay half-life/floor, quality-signal
factors) pinned explicitly in-process rather than read from the ambient
config. It then asserts the exact top-5 note-path order against a golden
list committed at `evals/golden/ranked_order.json`.

Deliberately excluded from this v0 blocking path, and why:
  - reranker (cross-encoder, ONNX): downloads a model from Hugging Face on
    first use (see hooks/tenet_reranker.py) -- a network dependency with no
    place in a hermetic gate.
  - live embedding / vector search: same class of problem (model weights),
    plus it is the dodge this ticket explicitly calls for. When the
    `embedded` extra is installed locally, `vector_ordering_advisory_checks()`
    below runs the same queries through the embedded backend WITH a real
    embedding provider and reports the resulting order for information
    only -- it never fails the run and never gates CI.
  - PPR/graph expansion, wikilink expansion, access-log boost: the first
    two are no-ops today (networkx isn't a project dependency) and the
    third reads a machine-global runtime log outside the fixture vault, so
    all three are pinned off to keep the check's inputs fully self
    contained.

Golden regeneration is deliberate, not automatic drift-following: set
`MEMENTO_REGEN_GOLDEN=1` to rewrite `evals/golden/ranked_order.json` with
whatever the current pipeline produces. A regenerated golden is a diff to
be read and understood before it is committed -- if you cannot explain
every path that moved, do not accept it. See evals/README.md.

A ranked order pinned here is not a claim that the order is *correct* --
only that it is *current*. Where MEM-135 already found real bugs in the
deep-pipeline gating (`memento/retrieval_policy.py`: vector search never
fuses when BM25 is empty; confidence gating uses an absolute score), the
affected golden entries are pinned to today's (arguably wrong) output on
purpose, fix-forward: when MEM-135 lands a fix, regenerate and review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
FIXTURE_VAULT = EVALS_DIR / "golden" / "fixtures" / "vault"

_PLACEHOLDER = re.compile(r"\{\{(DAYS_AGO_(\d+)|ROOT)\}\}")


def render_fixture_vault(root: Path, now: datetime) -> Path:
    """Render the fixture vault's {{DAYS_AGO_N}}/{{ROOT}} placeholders.

    `now` must be an aware UTC datetime (see evals.common.now()) so that
    two renders with the same injected clock produce byte-identical notes.
    """
    vault = root / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    (root / "project-alpha").mkdir()
    (root / "project-beta").mkdir()

    def substitute(match):
        if match.group(1) == "ROOT":
            return str(root)
        days = int(match.group(2))
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    for path in vault.rglob("*.md"):
        path.write_text(_PLACEHOLDER.sub(substitute, path.read_text()))
    return vault


def _result(path_stem, score=1.0):
    return {"path": f"notes/{path_stem}.md", "title": path_stem, "score": score, "snippet": ""}


def _score_of(results, stem):
    for r in results:
        if Path(r["path"]).stem == stem:
            return r["score"]
    return None


def _paths(results):
    return [Path(r["path"]).stem for r in results]


def fixture_checks(root: Path) -> list[dict]:
    from memento import search
    from memento.config import get_config

    config = get_config()
    checks = []

    def check(check_id, title, ok, details="", known_gap=False):
        checks.append(
            {"id": check_id, "title": title, "ok": bool(ok), "details": str(details)[:300], "known_gap": known_gap}
        )

    # --- temporal decay ---------------------------------------------------
    results = [_result("zephyr-cache-invalidation-old"), _result("zephyr-cache-invalidation-fresh")]
    search.apply_temporal_decay(results, config)
    old = _score_of(results, "zephyr-cache-invalidation-old")
    fresh = _score_of(results, "zephyr-cache-invalidation-fresh")
    check(
        "decay_orders_fresh_first",
        "A 300-day-old certainty-3 note decays below an equal-score fresh note",
        old is not None and fresh is not None and old < fresh,
        f"old={old} fresh={fresh}",
    )

    results = [_result("quasar-retry-policy-high-certainty")]
    search.apply_temporal_decay(results, config)
    check(
        "decay_immunity_high_certainty",
        "A certainty-5 note is immune to temporal decay",
        _score_of(results, "quasar-retry-policy-high-certainty") == 1.0,
        f"score={_score_of(results, 'quasar-retry-policy-high-certainty')}",
    )

    results = [_result("quasar-retry-policy-undated")]
    search.apply_temporal_decay(results, config)
    check(
        "decay_applies_to_undated_notes",
        "An undated low-certainty note should still decay (or be penalized)",
        _score_of(results, "quasar-retry-policy-undated") < 1.0,
        f"score={_score_of(results, 'quasar-retry-policy-undated')}; "
        "apply_temporal_decay skips notes without a date, so undated stale notes never decay",
        known_gap=True,
    )

    # --- quality signals ----------------------------------------------------
    results = [_result("nimbus-auth-session"), _result("nimbus-auth-typed")]
    results = search.apply_quality_signals(results, config)
    typed = _score_of(results, "nimbus-auth-typed")
    session = _score_of(results, "nimbus-auth-session")
    check(
        "session_type_penalized",
        "A type:session note ranks below an equal-score typed note",
        typed is not None and session is not None and session < typed,
        f"typed={typed} session={session}",
    )

    results = [_result("nimbus-auth-lowcert"), _result("nimbus-auth-typed")]
    results = search.apply_quality_signals(results, config)
    low = _score_of(results, "nimbus-auth-lowcert")
    high = _score_of(results, "nimbus-auth-typed")
    check(
        "low_certainty_penalized",
        "A certainty-1 note ranks below an equal-score certainty-4 note",
        low is not None and high is not None and low < high,
        f"low={low} high={high}",
    )

    results = [
        {"path": "fleeting/2026-01-01.md", "title": "daily", "score": 1.0, "snippet": ""},
        _result("nimbus-auth-typed"),
    ]
    results = search.apply_quality_signals(results, config)
    check(
        "fleeting_paths_dropped",
        "fleeting/ daily-log paths are dropped from candidates",
        "2026-01-01" not in _paths(results),
        f"survivors={_paths(results)}",
    )

    # --- project scoping -------------------------------------------------------
    alpha_cwd = str(root / "project-alpha")

    def orchid_results():
        return [
            _result("orchid-routing-project-alpha"),
            _result("orchid-routing-project-beta"),
            _result("orchid-routing-general"),
            _result("orchid-routing-gamma-slug"),
        ]

    strict = search.filter_by_project(orchid_results(), alpha_cwd, require_match=True)
    check(
        "project_scoping_strict",
        "require_match=True keeps only the matching project's notes",
        _paths(strict) == ["orchid-routing-project-alpha"],
        f"survivors={_paths(strict)}",
    )

    relaxed = search.filter_by_project(orchid_results(), alpha_cwd, require_match=False)
    check(
        "project_scoping_keeps_general",
        "require_match=False keeps matching plus untagged notes, drops other projects",
        "orchid-routing-project-alpha" in _paths(relaxed)
        and "orchid-routing-general" in _paths(relaxed)
        and "orchid-routing-project-beta" not in _paths(relaxed),
        f"survivors={_paths(relaxed)}",
    )

    # Slug-valued project fields are realpath'd against the PROCESS cwd, so a
    # slug accidentally matches any cwd that is its path prefix. Desired: a
    # slug for a different project never matches an unrelated cwd.
    os.chdir(root)
    slug_only = search.filter_by_project([_result("orchid-routing-gamma-slug")], str(root), require_match=True)
    check(
        "slug_project_not_universal",
        "A slug-valued project field must not match an unrelated cwd",
        _paths(slug_only) == [],
        f"survivors={_paths(slug_only)}; filter_by_project realpaths slugs against process cwd",
        known_gap=True,
    )

    # --- archive exclusion --------------------------------------------------------
    hits = search.qmd_search("krakenarchive retired", limit=5)
    check(
        "archive_never_surfaces",
        "Archived notes never surface through qmd_search",
        all(not r["path"].startswith("archive/") for r in hits),
        f"paths={[r['path'] for r in hits]}",
    )

    # --- supersession ---------------------------------------------------------------
    results = [_result("falcon-deploy-superseded"), _result("falcon-deploy-successor")]
    enhanced = search.enhance_results(list(results), config)
    superseded_score = _score_of(enhanced, "falcon-deploy-superseded")
    successor_score = _score_of(enhanced, "falcon-deploy-successor")
    check(
        "superseded_note_demoted",
        "A superseded note should rank below its successor at equal base score",
        superseded_score is not None and successor_score is not None and superseded_score < successor_score,
        f"superseded={superseded_score} successor={successor_score}; "
        "no pipeline stage currently reads the supersedes field for ranking",
        known_gap=True,
    )

    # --- end-to-end fixture retrieval ------------------------------------------------
    golden = [
        ("zephyrcache invalidation write-through", "zephyr-cache-invalidation-fresh"),
        ("quasarretry backoff jitter", "quasar-retry-policy-high-certainty"),
        ("nimbusauth token rotation", "nimbus-auth-typed"),
        ("falcondeploy canary releases", "falcon-deploy-successor"),
    ]
    for query, expected in golden:
        hits = search.qmd_search(query, limit=3)
        check(
            f"fixture_query_{expected}",
            f"Fixture query {query!r} finds {expected} in top 3",
            expected in _paths(hits),
            f"got={_paths(hits)}",
        )

    return checks


# --- Golden ranked-order regression (MEM-133) --------------------------------

RANKED_ORDER_GOLDEN = EVALS_DIR / "golden" / "ranked_order.json"

# (check_id, query). Each query is engineered to pull in multiple fixture
# notes sharing a nonsense token so real ranking tension (age, certainty,
# note type, raw BM25 relevance) decides the order -- see the fixture notes
# under evals/golden/fixtures/vault/notes/ for the engineered scenario each
# query exercises.
RANKED_ORDER_QUERIES = [
    (
        "vortex_certainty_beats_age",
        "vortexmigration",
    ),  # decay-immune old cert-5 note vs. fresh cert-3 vs. fresh low-cert vs. decayed cert-3
    (
        "briar_duplicate_title_certainty_tiebreak",
        "briarconfig",
    ),  # two near-duplicate titles, identical age, differ only by certainty
    (
        "nimbus_quality_signals_stack",
        "nimbusauth",
    ),  # typed decision vs. penalized session note vs. penalized low-certainty note
    (
        "orchid_general_and_scoped_notes_rank",
        "orchidrouting",
    ),  # four notes at equal age/similar certainty: mostly BM25/fusion-driven order
    (
        "quasar_certainty_immunity_vs_undated",
        "quasarretry",
    ),  # decay-immune cert-5 vs. undated low-certainty note (known_gap: undated notes never decay)
]


def _pinned_ranked_order_config(base_config: dict) -> dict:
    """Pin every parameter the ranked-order path depends on, explicitly.

    A copy of the ambient config, not a mutation of it, so this check can
    never perturb (or be perturbed by) fixture_checks() running in the same
    process. Values matching current DEFAULT_CONFIG are still restated here
    on purpose: an accidental default change in memento/config.py must not
    silently move these goldens, only a deliberate MEMENTO_REGEN_GOLDEN=1
    run should.
    """
    config = dict(base_config)
    config.update(
        {
            "prf_enabled": True,
            "prf_top_docs": 3,
            "prf_max_terms": 5,
            "rrf_k": 60,
            "temporal_decay": True,
            "temporal_decay_half_life": 90,
            "temporal_decay_certainty_floor": 4,
            "quality_signals_enabled": True,
            "quality_session_note_factor": 0.85,
            "quality_untyped_factor": 0.95,
            "quality_low_certainty_factor": 0.9,
            # Excluded from the v0 blocking path -- see module docstring.
            "reranker_enabled": False,
            "ppr_enabled": False,
            "wikilink_expansion": False,
            "multi_hop_enabled": False,
            "deep_recall_enabled": False,
            "access_log_enabled": False,
        }
    )
    return config


def _make_fts5_backend(vault: Path, db_path: Path):
    """A pure FTS5/BM25 EmbeddedSearchBackend: no embedding provider, so no
    vector table and no onnxruntime/sqlite-vec dependency at all."""
    from memento.embedded_search import EmbeddedSearchBackend

    return EmbeddedSearchBackend(vault_path=vault, db_path=db_path, embedding_provider=None)


def _ranked_order_top5(search_module, query: str, config: dict, limit: int = 5) -> list[str]:
    """Run one query through the real BM25 + PRF + RRF-fusion + policy path.

    Mirrors the production stages in memento.search / memento.retrieval_policy
    at the function level (this repo's fixture checks test at that same
    granularity): an initial BM25 pass, a PRF-expanded second pass, RRF
    fusion of the two (real memento.search.rrf_fuse, the same function the
    live pipeline uses to fuse text + vector), then temporal decay and
    quality signals (memento.search.apply_temporal_decay /
    apply_quality_signals) with the parameters above pinned.

    Deliberately calls those two policy functions directly rather than the
    enhance_results() orchestrator: enhance_results also runs a PageRank
    boost stage that is unconditional whenever `networkx` happens to be
    importable (it is not gated by ppr_enabled -- that config key only
    gates a *later* PPR-expansion stage). networkx is not a project
    dependency, so its presence differs by environment (observed: absent
    in a plain `pip install -e '.[mcp,embedded]'` venv, present in this
    repo's CI `gate-tests` job, which installs it for unrelated graph
    tests). Going through enhance_results made two of these five golden
    queries flip order purely based on whether networkx happened to be on
    sys.path -- exactly the kind of environment-dependent drift this ticket
    exists to catch, not create. Calling the two sub-stages directly keeps
    this check's inputs deterministic regardless of what else is installed.
    """
    pool = limit + 5
    primary = search_module.qmd_search(query, limit=pool)
    expanded_query = search_module.prf_expand_query(query, config=config, initial_results=primary)
    expanded = search_module.qmd_search(expanded_query, limit=pool) if expanded_query != query else []
    fused = search_module.rrf_fuse([primary, expanded], k=config.get("rrf_k", 60))
    decayed = search_module.apply_temporal_decay(fused, config)
    enhanced = search_module.apply_quality_signals(decayed, config)
    return _paths(enhanced)[:limit]


def ranked_order_checks(root: Path) -> list[dict]:
    """Golden ranked-order regression: pins the exact top-5 order for a
    fixed query set through the real FTS5/BM25 + fusion + policy path.
    See the module docstring for scope, exclusions, and the regeneration
    contract (MEMENTO_REGEN_GOLDEN=1).
    """
    from memento import search, search_backend
    from memento.config import get_config

    vault = root / "vault"
    config = _pinned_ranked_order_config(get_config())

    backend = _make_fts5_backend(vault, root / ".search-ranked" / "search.db")
    previous_backend = search_backend.get_backend()
    search_backend.set_backend(backend)
    try:
        actual_by_id = {check_id: _ranked_order_top5(search, query, config) for check_id, query in RANKED_ORDER_QUERIES}
    finally:
        search_backend.set_backend(previous_backend)

    regen = bool(os.environ.get("MEMENTO_REGEN_GOLDEN"))
    golden = {}
    if RANKED_ORDER_GOLDEN.exists():
        golden = json.loads(RANKED_ORDER_GOLDEN.read_text())

    checks = []
    new_golden = {}
    for check_id, query in RANKED_ORDER_QUERIES:
        actual = actual_by_id[check_id]
        new_golden[check_id] = {"query": query, "top5": actual}
        expected = (golden.get(check_id) or {}).get("top5")
        checks.append(
            {
                "id": f"ranked_order_{check_id}",
                "title": f"Golden top-5 order pinned for query {query!r}",
                "ok": True if regen else actual == expected,
                "details": str({"expected": expected, "got": actual})[:300],
                "known_gap": False,
            }
        )

    if regen:
        RANKED_ORDER_GOLDEN.write_text(json.dumps(new_golden, indent=2, sort_keys=True) + "\n")

    return checks


def vector_ordering_advisory_checks(root: Path) -> list[dict]:
    """Advisory, local-only: re-run the ranked-order queries through the
    embedded backend WITH a real embedding provider (FTS5 BM25 + vector,
    RRF-fused inside EmbeddedSearchBackend._hybrid_search) and report the
    resulting order for information only.

    Opt-in only: requires MEMENTO_EVAL_VECTOR_ADVISORY=1. Discovered while
    validating this check: ONNX CPU inference is not bit-reproducible
    across runs (near-tied cosine similarities occasionally swap rank), so
    this can never be part of the default `--mode fixture` output without
    breaking retrieval_probe.py's byte-for-byte reproducibility contract
    (tests/test_evals.py::TestRetrievalProbe::test_fixture_mode_same_now_is_byte_identical).
    That nondeterminism is fine for an informational, never-asserted signal
    -- it would not be fine as ambient default behavior.

    Beyond the opt-in flag, silently returns [] unless ALL of: the
    `embedded` extra's dependencies are installed, the configured provider
    is the local ONNX provider (API providers call out over the network on
    every embed() call -- never "local-only"), and that provider's model
    files are already cached on disk. That last check is deliberate and NOT
    the same gate memento.search_backend._make_embedded uses
    (`provider.is_available()` there only checks that onnxruntime imports;
    it does not check the model is cached, because in production triggering
    a first-time download is the intended behavior). An eval/gate script
    must never surprise a CI run or a contributor's `--strict` invocation
    with a multi-hundred-MB Hugging Face download, so this function checks
    the cache directly and never calls embed()/embed_query() unless the
    model is already there.

    Deliberately kept out of the `checks` list entirely (see main():
    reported under the separate "vector_advisory" JSON key) so it can never
    affect --strict or the retrieval_accuracy.policy_pass_rate metric.
    CI never runs this (the blocking gate installs without the `embedded`
    extra, so the first import already fails closed; the opt-in flag is
    belt-and-suspenders).
    """
    if not os.environ.get("MEMENTO_EVAL_VECTOR_ADVISORY"):
        return []

    try:
        import onnxruntime  # noqa: F401
        import sqlite_vec  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return []

    from memento import search, search_backend
    from memento.config import get_config
    from memento.embedded_search import EmbeddedSearchBackend

    config = _pinned_ranked_order_config(get_config())
    if config.get("embedding_provider", "local") != "local":
        return []  # API-backed providers call out over the network per query

    cache_dir = Path(os.environ.get("MEMENTO_MODEL_CACHE_DIR") or (Path.home() / ".cache" / "memento-vault" / "models"))
    model_dir = cache_dir / str(config.get("embedding_model", "nomic-embed-text-v1.5"))
    if not (model_dir / "model_quantized.onnx").exists() or not (model_dir / "tokenizer.json").exists():
        return []  # model not cached locally; never trigger a download from here

    from memento.embedding import get_embedding_provider

    try:
        provider = get_embedding_provider(config)
        if not provider.is_available():
            return []
    except Exception:
        return []

    vault = root / "vault"
    backend = EmbeddedSearchBackend(
        vault_path=vault, db_path=root / ".search-vector-advisory" / "search.db", embedding_provider=provider
    )
    previous_backend = search_backend.get_backend()
    search_backend.set_backend(backend)
    try:
        checks = []
        for check_id, query in RANKED_ORDER_QUERIES:
            try:
                actual = _ranked_order_top5(search, query, config)
            except Exception as exc:
                actual = None
                error = str(exc)
            else:
                error = None
            checks.append(
                {
                    "id": f"vector_advisory_{check_id}",
                    "title": f"Advisory (non-blocking) embedded/vector order for query {query!r}",
                    "details": str({"got": actual, "error": error})[:300],
                }
            )
        return checks
    finally:
        search_backend.set_backend(previous_backend)


def live_checks(queries_path: Path) -> dict:
    from memento import search
    from memento.config import get_config
    from memento.search_backend import get_backend

    backend = get_backend()
    payload = {
        "backend": type(backend).__name__,
        "available": backend.is_available(),
        "positive": [],
        "negative": [],
    }
    if not payload["available"]:
        return payload

    spec = json.loads(queries_path.read_text())
    config = get_config()
    min_score = float(config.get("recall_min_score", 0.4) or 0.4)

    def _rank(hits, expect_any):
        for idx, hit in enumerate(hits, start=1):
            if any(marker in hit["path"] for marker in expect_any):
                return idx
        return None

    for item in spec.get("positive", []):
        hits = search.qmd_search(item["query"], limit=5)
        # The default path above mirrors what prompt recall actually does
        # (BM25-first). The semantic pass is recorded alongside it so the
        # scorecard can show whether vector search would have rescued a miss.
        semantic_hits = search.qmd_search(item["query"], limit=5, semantic=True, timeout=30)
        payload["positive"].append(
            {
                "id": item["id"],
                "query": item["query"],
                "rank": _rank(hits, item["expect_any"]),
                "semantic_rank": _rank(semantic_hits, item["expect_any"]),
                "semantic_top_score": round(semantic_hits[0]["score"], 3) if semantic_hits else None,
                "got": [h["path"] for h in hits],
            }
        )

    for item in spec.get("negative", []):
        hits = search.qmd_search(item["query"], limit=3, min_score=min_score)
        payload["negative"].append(
            {
                "id": item["id"],
                "query": item["query"],
                "leaked": [f"{h['path']} ({h['score']:.2f})" for h in hits],
            }
        )
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["fixture", "live"], required=True)
    parser.add_argument("--queries", default=str(EVALS_DIR / "golden" / "retrieval_queries.json"))
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any non-known-gap fixture check fails (for CI gates)",
    )
    parser.add_argument(
        "--now",
        help="ISO-8601 UTC timestamp to freeze the eval clock for reproducible runs (env fallback: MEMENTO_EVAL_NOW)",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))

    from evals.common import now as eval_now, set_now

    set_now(args.now)
    effective_now = eval_now().isoformat()

    if args.mode == "fixture":
        tmp = tempfile.mkdtemp(prefix="memento-eval-vault-")
        try:
            vault = render_fixture_vault(Path(tmp), eval_now())
            os.environ["MEMENTO_VAULT_PATH"] = str(vault)
            os.environ["MEMENTO_SEARCH_BACKEND"] = "grep"
            os.environ.pop("MEMENTO_DEBUG", None)
            from memento.config import reset_config

            reset_config()
            checks = fixture_checks(Path(tmp))
            checks += ranked_order_checks(Path(tmp))
            vector_advisory = vector_ordering_advisory_checks(Path(tmp))
            payload = {"effective_now": effective_now, "checks": checks}
            if vector_advisory:
                payload["vector_advisory"] = vector_advisory
            print(json.dumps(payload, sort_keys=True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if args.strict and any(not c["ok"] and not c["known_gap"] for c in checks):
            sys.exit(1)
    else:
        payload = live_checks(Path(args.queries))
        payload["effective_now"] = effective_now
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
