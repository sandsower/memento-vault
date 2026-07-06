"""Tests for MEM-162: code citations captured at write time, verified at use.

Covers the full loop: triage emits citations from a transcript (fake LLM),
store.py normalizes/writes them defensively, retrieval_policy/lifecycle
verify them cheaply at injection time (recall + tool-context), and the
sweeper folds queued staleness flags into durable frontmatter.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from memento.llm import LLMResult
from memento.retrieval_policy import (
    PromptRecallRequest,
    PromptRecallRuntime,
    apply_stale_citation_marker,
    evaluate_citation_verification,
)
from memento.store import (
    _normalize_citations,
    append_stale_citation_review,
    fold_stale_citations_into_frontmatter,
    write_note,
)

# Import memento-triage.py (hyphenated filename) under its own sys.modules key
# -- NOT "memento_triage" (test_triage.py already claims that name). Sharing a
# key would make whichever test file's module happens to import last silently
# win the registration, so patch("memento_triage....") in one file could
# target the OTHER file's module instance depending on collection order.
_triage_spec = importlib.util.spec_from_file_location(
    "memento_triage_citations",
    str(Path(__file__).parent.parent / "hooks" / "memento-triage.py"),
)
_triage_mod = importlib.util.module_from_spec(_triage_spec)
sys.modules["memento_triage_citations"] = _triage_mod
_triage_spec.loader.exec_module(_triage_mod)
process_structured_notes = _triage_mod.process_structured_notes

# Import memento-sweeper.py (hyphenated filename), same pattern as test_sweeper.py.
_sweeper_spec = importlib.util.spec_from_file_location(
    "memento_sweeper_citations",
    str(Path(__file__).parent.parent / "hooks" / "memento-sweeper.py"),
)
_sweeper_mod = importlib.util.module_from_spec(_sweeper_spec)
sys.modules["memento_sweeper_citations"] = _sweeper_mod
_sweeper_spec.loader.exec_module(_sweeper_mod)


@pytest.fixture(autouse=True)
def _smart_store_patches(monkeypatch):
    """Patch smart_store dependencies so triage tests don't hit real backends."""
    monkeypatch.setattr("memento.smart_store.get_vault", lambda *a, **kw: _triage_mod.get_vault())
    monkeypatch.setattr("memento.smart_store.has_qmd", lambda: False)
    monkeypatch.setattr("memento.smart_store.inspect_contradictions", lambda *a, **kw: {})


def _user_msg(text, cwd="/home/user/project", branch="main"):
    return {"type": "user", "cwd": cwd, "gitBranch": branch, "message": {"content": text}}


# --- store.py: normalization ---------------------------------------------


class TestNormalizeCitations:
    def test_drops_malformed_entries_keeps_valid_ones(self):
        result = _normalize_citations(
            [
                {"file": "memento/store.py", "anchor": "def write_note("},
                "not-a-dict",
                {"file": "", "anchor": "x"},  # empty file
                {"file": "a.py", "anchor": ""},  # empty anchor
                {"anchor": "no file key"},
                42,
                None,
            ]
        )
        assert result == [{"file": "memento/store.py", "anchor": "def write_note("}]

    def test_truncates_oversized_anchor(self):
        long_anchor = "x" * 200
        result = _normalize_citations([{"file": "a.py", "anchor": long_anchor}])
        assert len(result[0]["anchor"]) == 120
        assert result[0]["anchor"] == "x" * 120

    def test_keeps_optional_commit(self):
        result = _normalize_citations([{"file": "a.py", "anchor": "y", "commit": "abc1234"}])
        assert result == [{"file": "a.py", "anchor": "y", "commit": "abc1234"}]

    def test_non_list_input_returns_empty(self):
        assert _normalize_citations("not-a-list") == []
        assert _normalize_citations(None) == []
        assert _normalize_citations({"file": "a.py"}) == []


class TestWriteNoteCitations:
    def test_write_note_emits_citations_line(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Citations note",
            body="Body.",
            note_type="discovery",
            tags=["x"],
            certainty=3,
            citations=[{"file": "memento/store.py", "anchor": "def write_note(", "commit": "abc1234"}],
        )
        text = path.read_text()
        assert 'citations: [{"file": "memento/store.py", "anchor": "def write_note(", "commit": "abc1234"}]' in text

    def test_write_note_omits_citations_line_when_absent(self, tmp_vault):
        path = write_note(
            tmp_vault, title="No citations note", body="Body.", note_type="discovery", tags=["x"], certainty=3
        )
        assert "citations:" not in path.read_text()

    def test_write_note_drops_bad_citations_but_still_writes_note(self, tmp_vault):
        path = write_note(
            tmp_vault,
            title="Bad citations note",
            body="Body.",
            note_type="discovery",
            tags=["x"],
            certainty=3,
            citations=["not-a-dict", {"file": "a.py"}],
        )
        assert path.exists()
        assert "citations:" not in path.read_text()


# --- store.py: stale-citation queue + sweeper fold ------------------------


class TestStaleCitationQueueAndFold:
    @pytest.fixture(autouse=True)
    def _isolated_lock(self, monkeypatch, tmp_path):
        monkeypatch.setattr("memento.store.VAULT_WRITE_LOCK_PATH", str(tmp_path / "locks" / "vault-write.lock"))

    @staticmethod
    def _seed_note(tmp_vault, stem="example", extra_frontmatter=""):
        note_path = tmp_vault / "notes" / f"{stem}.md"
        note_path.write_text(f"---\ntitle: Example\ntype: discovery\ntags: [redis]\n{extra_frontmatter}---\n\nBody.\n")
        return note_path

    def test_append_then_fold_marks_note_stale(self, tmp_vault, tmp_path):
        note_path = self._seed_note(tmp_vault)
        queue_path = tmp_path / "stale-citations.jsonl"

        append_stale_citation_review("notes/example.md", [{"file": "a.py", "anchor": "gone"}], queue_path=queue_path)
        assert queue_path.exists()

        result = fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)

        assert result == {"folded_notes": 1, "queued_events": 1}
        text = note_path.read_text()
        assert "citation_stale: true" in text
        # Untouched fields and body survive verbatim.
        assert "title: Example" in text
        assert "tags: [redis]" in text
        assert text.endswith("Body.\n")

    def test_fold_is_idempotent(self, tmp_vault, tmp_path):
        note_path = self._seed_note(tmp_vault)
        queue_path = tmp_path / "stale-citations.jsonl"
        append_stale_citation_review("notes/example.md", [{"file": "a.py", "anchor": "gone"}], queue_path=queue_path)
        fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)
        first_text = note_path.read_text()

        # Queue a second, duplicate flag and re-run the fold.
        append_stale_citation_review("notes/example.md", [{"file": "a.py", "anchor": "gone"}], queue_path=queue_path)
        result = fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)

        assert result["folded_notes"] == 0  # already flagged -- no rewrite
        assert note_path.read_text() == first_text
        assert first_text.count("citation_stale: true") == 1

    def test_fold_drains_queue_so_rerun_with_no_new_events_is_noop(self, tmp_vault, tmp_path):
        self._seed_note(tmp_vault)
        queue_path = tmp_path / "stale-citations.jsonl"
        append_stale_citation_review("notes/example.md", [{"file": "a.py", "anchor": "gone"}], queue_path=queue_path)
        fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)

        result = fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)
        assert result == {"folded_notes": 0, "queued_events": 0}

    def test_fold_skips_missing_and_traversal_paths(self, tmp_vault, tmp_path):
        queue_path = tmp_path / "stale-citations.jsonl"
        append_stale_citation_review("notes/does-not-exist.md", [], queue_path=queue_path)
        append_stale_citation_review("../outside.md", [], queue_path=queue_path)

        result = fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=queue_path)
        assert result == {"folded_notes": 0, "queued_events": 2}

    def test_fold_no_queue_file_is_a_noop(self, tmp_vault, tmp_path):
        result = fold_stale_citations_into_frontmatter(str(tmp_vault), queue_path=tmp_path / "missing.jsonl")
        assert result == {"folded_notes": 0, "queued_events": 0}


# --- retrieval_policy.py: verify-at-use -----------------------------------


def _write_note_with_citations(vault, rel_path, *, project_path, citations):
    note_path = vault / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "\n".join(
            [
                "---",
                "title: Fact",
                "type: discovery",
                "tags: [x]",
                "source: session",
                f"project_path: {project_path}",
                f"citations: {json.dumps(citations)}",
                "date: 2026-01-01T00:00",
                "---",
                "",
                "Body",
                "",
            ]
        )
    )
    return note_path


class TestEvaluateCitationVerification:
    def test_verified_when_anchor_present(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("def still_here():\n    pass\n")
        _write_note_with_citations(
            vault, "notes/fact.md", project_path=str(repo), citations=[{"file": "app.py", "anchor": "def still_here"}]
        )

        status = evaluate_citation_verification(
            vault, {"path": "notes/fact.md"}, cwd=str(repo), config={"citation_verification_enabled": True}
        )
        assert status == "verified"

    def test_stale_when_anchor_gone_and_queues_review(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("def renamed_now():\n    pass\n")
        _write_note_with_citations(
            vault, "notes/fact.md", project_path=str(repo), citations=[{"file": "app.py", "anchor": "def old_name"}]
        )

        queued = []
        status = evaluate_citation_verification(
            vault,
            {"path": "notes/fact.md"},
            cwd=str(repo),
            config={"citation_verification_enabled": True},
            queue_stale=lambda note_path, citations: queued.append((note_path, citations)),
        )
        assert status == "stale"
        assert queued == [("notes/fact.md", [{"file": "app.py", "anchor": "def old_name"}])]

    def test_unverifiable_when_file_missing(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        _write_note_with_citations(
            vault, "notes/fact.md", project_path=str(repo), citations=[{"file": "missing.py", "anchor": "x"}]
        )

        status = evaluate_citation_verification(
            vault, {"path": "notes/fact.md"}, cwd=str(repo), config={"citation_verification_enabled": True}
        )
        assert status == "unverifiable"

    def test_unverifiable_when_repo_context_unavailable(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        _write_note_with_citations(
            vault,
            "notes/fact.md",
            project_path="/definitely/not/a/real/path/xyz",
            citations=[{"file": "app.py", "anchor": "x"}],
        )

        status = evaluate_citation_verification(
            vault,
            {"path": "notes/fact.md"},
            cwd="/also/not/real",
            config={"citation_verification_enabled": True},
        )
        assert status == "unverifiable"

    def test_no_citations_is_not_verified_or_stale(self, tmp_vault):
        note = tmp_vault / "notes" / "plain.md"
        note.write_text("---\ntitle: Plain\ntype: discovery\ntags: [x]\nsource: session\n---\n\nBody\n")
        status = evaluate_citation_verification(
            tmp_vault, {"path": "notes/plain.md"}, config={"citation_verification_enabled": True}
        )
        assert status == "no-citations"

    def test_disabled_by_config_short_circuits(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("def gone_now():\n    pass\n")
        _write_note_with_citations(
            vault, "notes/fact.md", project_path=str(repo), citations=[{"file": "app.py", "anchor": "def old_name"}]
        )

        queued = []
        status = evaluate_citation_verification(
            vault,
            {"path": "notes/fact.md"},
            cwd=str(repo),
            config={"citation_verification_enabled": False},
            queue_stale=lambda *a: queued.append(a),
        )
        assert status == "no-citations"
        assert queued == []

    def test_path_traversal_in_citation_file_is_unverifiable(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("top secret contents")
        _write_note_with_citations(
            vault,
            "notes/fact.md",
            project_path=str(repo),
            citations=[{"file": "../outside-secret.txt", "anchor": "top secret"}],
        )

        status = evaluate_citation_verification(
            vault, {"path": "notes/fact.md"}, cwd=str(repo), config={"citation_verification_enabled": True}
        )
        assert status == "unverifiable"


def test_apply_stale_citation_marker_prefixes_bullet_line():
    line = "  - Old fact: some snippet"
    marked = apply_stale_citation_marker(line)
    assert marked == "  - [stale: cited code changed] Old fact: some snippet"


# --- PromptRecallRuntime end-to-end stale marker in injected output -------


class TestPromptRecallStaleMarker:
    def test_run_marks_stale_citation_in_injected_content(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("def new_function():\n    pass\n")
        _write_note_with_citations(
            vault,
            "notes/note1.md",
            project_path=str(repo),
            citations=[{"file": "app.py", "anchor": "def old_function"}],
        )

        runtime = PromptRecallRuntime(
            config_loader=lambda: {
                "prompt_recall": True,
                "recall_min_score": 0.4,
                "recall_max_notes": 3,
                "concept_index_enabled": False,
                "rrf_enabled": False,
                "multi_hop_enabled": False,
                "reranker_enabled": False,
                "citation_verification_enabled": True,
            },
            vault_loader=lambda: vault,
            has_backend=lambda: True,
            remote_available=lambda: False,
            detect_project=lambda _cwd: ("unknown", None),
            qmd_search=lambda *_a, **_k: [
                {"path": "notes/note1.md", "title": "Old fact", "score": 0.9, "snippet": "stuff"}
            ],
            enhance_results=lambda results, **_kwargs: results,
            recently_injected_paths=lambda *_args, **_kwargs: set(),
        )

        decision = runtime.run(PromptRecallRequest(prompt="what old fact do we know", cwd=str(repo)))

        assert decision.should_inject is True
        assert "[stale: cited code changed]" in decision.content
        assert "Old fact" in decision.content

    def test_run_does_not_mark_hot_path_for_citation_less_notes(self, tmp_path):
        """Regression: notes with no citations must format exactly as before MEM-162."""
        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        note = vault / "notes" / "plain.md"
        note.write_text("---\ntitle: Plain fact\ntype: discovery\ntags: [x]\nsource: session\n---\n\nBody\n")

        runtime = PromptRecallRuntime(
            config_loader=lambda: {
                "prompt_recall": True,
                "recall_min_score": 0.4,
                "recall_max_notes": 3,
                "concept_index_enabled": False,
                "rrf_enabled": False,
                "multi_hop_enabled": False,
                "reranker_enabled": False,
            },
            vault_loader=lambda: vault,
            has_backend=lambda: True,
            remote_available=lambda: False,
            detect_project=lambda _cwd: ("unknown", None),
            qmd_search=lambda *_a, **_k: [
                {"path": "notes/plain.md", "title": "Plain fact", "score": 0.9, "snippet": "stuff"}
            ],
            enhance_results=lambda results, **_kwargs: results,
            recently_injected_paths=lambda *_args, **_kwargs: set(),
        )

        decision = runtime.run(PromptRecallRequest(prompt="what plain fact do we know", cwd=str(tmp_path)))

        assert decision.content == "[vault] Related memories:\n  - Plain fact: stuff"


# --- lifecycle.py: tool-context injection stale marker --------------------


class TestBuildToolContextStaleMarker:
    def test_stale_citation_marks_tool_context_injection(self, tmp_path):
        import memento.lifecycle as lifecycle_module

        vault = tmp_path / "vault"
        (vault / "notes").mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("def new_function():\n    pass\n")
        _write_note_with_citations(
            vault,
            "notes/auth-boundary.md",
            project_path=str(repo),
            citations=[{"file": "app.py", "anchor": "def old_function"}],
        )

        config = dict(lifecycle_module.get_config())
        config["tool_context_min_score"] = 0.75
        config["citation_verification_enabled"] = True

        with (
            patch("memento.lifecycle.has_qmd", return_value=True),
            patch("memento.lifecycle.get_config", return_value=config),
            patch("memento.lifecycle.get_vault", return_value=vault),
            # pytest tmp dirs live under /tmp on Linux, which SKIP_PREFIXES
            # excludes from tool context - bypass the fast-exit for this test.
            patch("memento.lifecycle.should_skip_tool_context_path", return_value=False),
            patch(
                "memento.lifecycle.qmd_search_with_extras",
                return_value=[
                    {
                        "path": "notes/auth-boundary.md",
                        "title": "Auth boundary lives in middleware",
                        "score": 0.9,
                        "snippet": "Middleware owns auth checks.",
                    }
                ],
            ),
            patch("memento.lifecycle.enhance_results", side_effect=lambda results, *a, **kw: results),
            patch("memento.lifecycle.load_cache", return_value={"dirs": {}, "last_qmd_call": 0, "injections": {}}),
            patch("memento.lifecycle.save_cache"),
        ):
            result = lifecycle_module.build_tool_context("Read", "src/server/authMiddleware.ts", str(repo), "s1")

        assert result.should_inject is True
        assert "[stale: cited code changed]" in result.content
        assert "Auth boundary lives in middleware" in result.content


# --- hooks/memento-triage.py: capture-side citation emission --------------


class TestTriageCitations:
    def test_triage_emits_citations_from_fixture_transcript(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "feature/DC-123-cache",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }
        llm_payload = json.dumps(
            [
                {
                    "title": "Redis cache keys need explicit TTL",
                    "body": "Keys without TTL caused stale reads.",
                    "type": "bugfix",
                    "tags": ["redis", "caching"],
                    "certainty": 3,
                    "citations": [{"file": "src/cache.py", "anchor": "def set_with_ttl(", "commit": "a1b2c3d"}],
                }
            ]
        )

        with (
            patch("memento_triage_citations.get_vault", return_value=tmp_vault),
            patch(
                "memento_triage_citations.llm_complete", return_value=LLMResult(text=llm_payload, ok=True, error=None)
            ),
        ):
            written = process_structured_notes("sess-123", str(transcript), meta, "api-service")

        assert written == 1
        note = tmp_vault / "notes" / "redis-cache-keys-need-explicit-ttl.md"
        assert note.exists()
        note_text = note.read_text()
        assert 'citations: [{"file": "src/cache.py", "anchor": "def set_with_ttl(", "commit": "a1b2c3d"}]' in note_text

    def test_triage_drops_malformed_citations_but_keeps_note(self, tmp_vault, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_user_msg("Figure out the cache bug")) + "\n")
        meta = {
            "cwd": "/home/vic/Projects/api-service",
            "git_branch": "main",
            "exchange_count": 6,
            "files_edited": ["src/cache.py"],
            "first_prompt": "Figure out the cache bug",
            "last_outcome": "Fixed the TTL bug.",
        }
        llm_payload = json.dumps(
            [
                {
                    "title": "Cache invalidation strategy",
                    "body": "Invalidate on write.",
                    "type": "decision",
                    "tags": ["caching"],
                    "certainty": 3,
                    "citations": "not-a-list",
                }
            ]
        )

        with (
            patch("memento_triage_citations.get_vault", return_value=tmp_vault),
            patch(
                "memento_triage_citations.llm_complete", return_value=LLMResult(text=llm_payload, ok=True, error=None)
            ),
        ):
            written = process_structured_notes("sess-456", str(transcript), meta, "api-service")

        assert written == 1
        note = tmp_vault / "notes" / "cache-invalidation-strategy.md"
        assert note.exists()
        assert "citations:" not in note.read_text()

    def test_triage_prompt_mentions_citations(self):
        prompt = _triage_mod._build_structured_notes_prompt("s1", "transcript text", {}, "proj", [])
        assert "citations" in prompt


# --- hooks/memento-sweeper.py: wired into main() --------------------------


class TestSweeperCitationFold:
    def test_main_runs_citation_stale_fold_after_fleeting_lifecycle_sweep(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()

        monkeypatch.setattr(_sweeper_mod, "VAULT", vault)
        monkeypatch.setattr(_sweeper_mod, "FLEETING", vault / "fleeting")
        monkeypatch.setattr(_sweeper_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
        monkeypatch.setattr(_sweeper_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
        monkeypatch.setattr(_sweeper_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
        monkeypatch.setattr(_sweeper_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
        monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
        monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
        monkeypatch.setattr(_sweeper_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
        monkeypatch.setattr(_sweeper_mod, "sweep_archive_candidates", lambda vault_path: None)
        monkeypatch.setattr(_sweeper_mod, "fleeting_lifecycle_sweep", lambda vault_path: None)

        fold_calls = []
        monkeypatch.setattr(
            _sweeper_mod, "fold_stale_citations_into_frontmatter", lambda vault_path: fold_calls.append(vault_path)
        )

        with pytest.raises(SystemExit) as exc:
            _sweeper_mod.main()

        assert exc.value.code == 0
        assert fold_calls == [str(vault)]

    def test_main_still_triages_when_citation_stale_fold_raises(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()

        monkeypatch.setattr(_sweeper_mod, "VAULT", vault)
        monkeypatch.setattr(_sweeper_mod, "FLEETING", vault / "fleeting")
        monkeypatch.setattr(_sweeper_mod, "LOCK_FILE", tmp_path / "sweeper.lock")
        monkeypatch.setattr(_sweeper_mod, "CLAUDE_PROJECTS", tmp_path / "no-claude-projects")
        monkeypatch.setattr(_sweeper_mod, "PI_SESSIONS", tmp_path / "no-pi-sessions")
        monkeypatch.setattr(_sweeper_mod, "PI_SUBAGENTS", tmp_path / "no-pi-subagents")
        monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
        monkeypatch.delenv("MEMENTO_PI_TRANSCRIPT_ROOTS", raising=False)
        monkeypatch.setattr(_sweeper_mod, "fold_access_log_into_frontmatter", lambda vault_path: None)
        monkeypatch.setattr(_sweeper_mod, "sweep_archive_candidates", lambda vault_path: None)
        monkeypatch.setattr(_sweeper_mod, "fleeting_lifecycle_sweep", lambda vault_path: None)

        def _boom(vault_path):
            raise RuntimeError("citation fold exploded")

        monkeypatch.setattr(_sweeper_mod, "fold_stale_citations_into_frontmatter", _boom)

        with pytest.raises(SystemExit) as exc:
            _sweeper_mod.main()

        assert exc.value.code == 0
