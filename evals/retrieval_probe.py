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


def render_fixture_vault(root: Path) -> Path:
    vault = root / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    (root / "project-alpha").mkdir()
    (root / "project-beta").mkdir()
    now = datetime.now()

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
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))

    if args.mode == "fixture":
        tmp = tempfile.mkdtemp(prefix="memento-eval-vault-")
        try:
            vault = render_fixture_vault(Path(tmp))
            os.environ["MEMENTO_VAULT_PATH"] = str(vault)
            os.environ["MEMENTO_SEARCH_BACKEND"] = "grep"
            os.environ.pop("MEMENTO_DEBUG", None)
            from memento.config import reset_config

            reset_config()
            checks = fixture_checks(Path(tmp))
            print(json.dumps({"checks": checks}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if args.strict and any(not c["ok"] and not c["known_gap"] for c in checks):
            sys.exit(1)
    else:
        print(json.dumps(live_checks(Path(args.queries))))


if __name__ == "__main__":
    main()
