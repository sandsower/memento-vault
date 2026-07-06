"""Tests for the bounded tool-using retrieval agent (MEM-161)."""

import json
from pathlib import Path
from unittest.mock import patch

from memento.llm import LLMResult
from memento.retrieval_agent import (
    AgenticRetrievalDeps,
    _dispatch_tool,
    _extract_json_object,
    _guarded_full_path,
    _hydrate_results,
    _normalize_note_path,
    _tool_get,
    _tool_query,
    _tool_related,
    _tool_search,
    agentic_retrieve,
)


def _write_note(vault: Path, stem: str, *, title: str, note_type: str = "discovery", body: str = "Body text."):
    path = vault / "notes" / f"{stem}.md"
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                f"type: {note_type}",
                "---",
                "",
                body,
            ]
        )
    )
    return path


class TestExtractJsonObject:
    def test_direct_json(self):
        assert _extract_json_object('{"done": true, "results": []}') == {"done": True, "results": []}

    def test_json_in_code_fence(self):
        raw = '```json\n{"tool": "search", "args": {"query": "x"}}\n```'
        assert _extract_json_object(raw) == {"tool": "search", "args": {"query": "x"}}

    def test_json_in_bare_code_fence(self):
        raw = '```\n{"done": true, "results": ["notes/a.md"]}\n```'
        assert _extract_json_object(raw) == {"done": True, "results": ["notes/a.md"]}

    def test_json_embedded_in_prose(self):
        raw = 'Sure thing! {"tool": "get", "args": {"path": "a"}} is what I will call.'
        assert _extract_json_object(raw) == {"tool": "get", "args": {"path": "a"}}

    def test_no_json_returns_none(self):
        assert _extract_json_object("I am not sure what to do.") is None

    def test_empty_string_returns_none(self):
        assert _extract_json_object("") is None

    def test_json_array_is_rejected_not_an_object(self):
        assert _extract_json_object("[1, 2, 3]") is None


class TestNormalizeNotePath:
    def test_bare_name_gets_notes_prefix_and_extension(self):
        assert _normalize_note_path("redis-cache-ttl") == "notes/redis-cache-ttl.md"

    def test_bare_md_name_gets_notes_prefix(self):
        assert _normalize_note_path("redis-cache-ttl.md") == "notes/redis-cache-ttl.md"

    def test_full_relative_path_is_untouched(self):
        assert _normalize_note_path("notes/redis-cache-ttl.md") == "notes/redis-cache-ttl.md"

    def test_other_directory_path_is_untouched(self):
        assert _normalize_note_path("fleeting/scratch.md") == "fleeting/scratch.md"


class TestGuardedFullPath:
    def test_traversal_outside_vault_is_rejected(self, tmp_vault):
        assert _guarded_full_path(tmp_vault, "../../etc/passwd") is None

    def test_path_inside_vault_resolves(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A")
        resolved = _guarded_full_path(tmp_vault, "notes/a.md")
        assert resolved == (tmp_vault / "notes" / "a.md").resolve()


class TestToolGet:
    def test_reads_existing_note_by_bare_name(self, tmp_vault):
        _write_note(tmp_vault, "redis-cache-ttl", title="Redis cache TTL", body="Set an explicit TTL.")

        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_get({"path": "redis-cache-ttl"})

        assert result["path"] == "notes/redis-cache-ttl.md"
        assert "Set an explicit TTL." in result["content"]

    def test_missing_path_arg_is_an_error(self, tmp_vault):
        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_get({})
        assert "error" in result

    def test_traversal_attempt_is_an_error(self, tmp_vault):
        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_get({"path": "../../etc/passwd"})
        assert "error" in result

    def test_missing_note_falls_back_to_qmd_get_then_errors(self, tmp_vault):
        with (
            patch("memento.retrieval_agent.get_vault", return_value=tmp_vault),
            patch("memento.retrieval_agent.qmd_get", return_value=None),
        ):
            result = _tool_get({"path": "does-not-exist"})
        assert "error" in result
        assert "not found" in result["error"]

    def test_missing_note_uses_qmd_get_fallback_when_available(self, tmp_vault):
        fallback = {"path": "notes/remote.md", "title": "Remote", "content": "remote content"}
        with (
            patch("memento.retrieval_agent.get_vault", return_value=tmp_vault),
            patch("memento.retrieval_agent.qmd_get", return_value=fallback),
        ):
            result = _tool_get({"path": "remote"})
        assert result == fallback


class TestToolQuery:
    def test_returns_matching_notes(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A", note_type="decision")
        _write_note(tmp_vault, "b", title="B", note_type="bugfix")

        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_query({"note_type": "decision"})

        paths = [entry["path"] for entry in result["results"]]
        assert "notes/a.md" in paths
        assert "notes/b.md" not in paths

    def test_invalid_certainty_range_surfaces_error_metadata(self, tmp_vault):
        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_query({"certainty_min": 5, "certainty_max": 1})
        assert "error" in result


class TestToolRelated:
    def test_missing_note_arg_is_an_error(self):
        assert "error" in _tool_related({})

    def test_unresolved_note_returns_structured_error(self, tmp_vault):
        with patch("memento.retrieval_agent.get_vault", return_value=tmp_vault):
            result = _tool_related({"note": "nonexistent-note"})
        assert "error" in result


class TestToolSearch:
    def test_missing_query_is_an_error(self):
        assert "error" in _tool_search({})

    def test_delegates_to_explicit_search_runtime(self, tmp_vault):
        fake_result = {"results": [{"path": "notes/a.md", "title": "A", "score": 0.9}], "metadata": {}}
        with patch("memento.retrieval_agent.ExplicitSearchRuntime") as mock_runtime_cls:
            mock_runtime_cls.return_value.search.return_value = dict(fake_result)
            result = _tool_search({"query": "caching"})
        assert result["results"][0]["path"] == "notes/a.md"

    def test_applies_type_filter_on_top_of_ranked_results(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A", note_type="decision")
        fake_result = {
            "results": [{"path": "notes/a.md", "title": "A", "score": 0.9}],
            "metadata": {},
        }
        with (
            patch("memento.retrieval_agent.get_vault", return_value=tmp_vault),
            patch("memento.retrieval_agent.ExplicitSearchRuntime") as mock_runtime_cls,
        ):
            mock_runtime_cls.return_value.search.return_value = dict(fake_result)
            result = _tool_search({"query": "caching", "type": "bugfix"})
        assert result["results"] == []

    def test_invalid_filter_surfaces_error(self, tmp_vault):
        fake_result = {"results": [{"path": "notes/a.md", "title": "A", "score": 0.9}], "metadata": {}}
        with patch("memento.retrieval_agent.ExplicitSearchRuntime") as mock_runtime_cls:
            mock_runtime_cls.return_value.search.return_value = dict(fake_result)
            result = _tool_search({"query": "caching", "certainty_min": 5, "certainty_max": 1})
        assert "error" in result


class TestDispatchTool:
    def test_unknown_tool_name_is_an_error_observation(self):
        deps = AgenticRetrievalDeps()
        result = _dispatch_tool("delete_everything", {}, deps)
        assert "error" in result
        assert "unknown tool" in result["error"]

    def test_tool_exception_becomes_error_observation_not_a_raise(self):
        deps = AgenticRetrievalDeps(search=lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
        result = _dispatch_tool("search", {"query": "x"}, deps)
        assert result == {"error": "boom"}

    def test_non_dict_args_are_coerced_to_empty_dict(self):
        seen = {}

        def fake_get(args):
            seen["args"] = args
            return {"path": "notes/a.md"}

        deps = AgenticRetrievalDeps(get=fake_get)
        _dispatch_tool("get", "not-a-dict", deps)
        assert seen["args"] == {}


class TestHydrateResults:
    def test_hydrates_known_paths_with_descending_scores(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A")
        _write_note(tmp_vault, "b", title="B")
        deps = AgenticRetrievalDeps(vault_loader=lambda: tmp_vault)

        hydrated = _hydrate_results(["notes/a.md", "notes/b.md"], deps)

        assert [entry["path"] for entry in hydrated] == ["notes/a.md", "notes/b.md"]
        assert hydrated[0]["score"] > hydrated[1]["score"]
        assert hydrated[0]["title"] == "A"

    def test_unknown_paths_are_dropped(self, tmp_vault):
        deps = AgenticRetrievalDeps(vault_loader=lambda: tmp_vault)
        assert _hydrate_results(["notes/does-not-exist.md"], deps) == []

    def test_non_list_input_returns_empty(self, tmp_vault):
        deps = AgenticRetrievalDeps(vault_loader=lambda: tmp_vault)
        assert _hydrate_results(None, deps) == []
        assert _hydrate_results("notes/a.md", deps) == []

    def test_dedups_repeated_paths(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A")
        deps = AgenticRetrievalDeps(vault_loader=lambda: tmp_vault)
        hydrated = _hydrate_results(["notes/a.md", "a", "notes/a.md"], deps)
        assert len(hydrated) == 1


def _llm(text, ok=True, error=None):
    return LLMResult(text=text, ok=ok, error=error)


class TestAgenticRetrieveLoop:
    """The ReAct loop itself, driven by a scripted fake llm_complete."""

    def test_empty_query_returns_empty_without_calling_llm(self):
        deps = AgenticRetrievalDeps(
            llm_complete=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call"))
        )
        assert agentic_retrieve("   ", config={}, deps=deps) == []

    def test_multi_turn_happy_path(self, tmp_vault):
        _write_note(tmp_vault, "redis-ttl", title="Redis TTL")
        responses = [
            _llm(json.dumps({"tool": "search", "args": {"query": "redis caching"}})),
            _llm(json.dumps({"done": True, "results": ["notes/redis-ttl.md"]})),
        ]
        calls = {"n": 0}

        def fake_llm_complete(prompt, config, timeout=None):
            result = responses[calls["n"]]
            calls["n"] += 1
            return result

        deps = AgenticRetrievalDeps(
            llm_complete=fake_llm_complete,
            search=lambda args: {"results": [{"path": "notes/redis-ttl.md", "title": "Redis TTL", "score": 0.8}]},
            vault_loader=lambda: tmp_vault,
        )

        results = agentic_retrieve("what did we decide about redis caching?", config={}, deps=deps)

        assert calls["n"] == 2
        assert len(results) == 1
        assert results[0]["path"] == "notes/redis-ttl.md"

    def test_malformed_json_gets_one_retry_then_succeeds(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A")
        responses = [
            _llm("I'm thinking about this..."),
            _llm(json.dumps({"done": True, "results": ["notes/a.md"]})),
        ]
        calls = {"n": 0}

        def fake_llm_complete(prompt, config, timeout=None):
            result = responses[calls["n"]]
            calls["n"] += 1
            return result

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete, vault_loader=lambda: tmp_vault)

        results = agentic_retrieve("query", config={}, deps=deps)

        assert calls["n"] == 2
        assert len(results) == 1

    def test_malformed_json_twice_falls_back(self):
        responses = [_llm("nonsense"), _llm("still nonsense")]
        calls = {"n": 0}

        def fake_llm_complete(prompt, config, timeout=None):
            result = responses[calls["n"]]
            calls["n"] += 1
            return result

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete)

        assert agentic_retrieve("query", config={}, deps=deps) == []
        assert calls["n"] == 2

    def test_provider_error_falls_back(self):
        deps = AgenticRetrievalDeps(llm_complete=lambda *a, **k: _llm("", ok=False, error="claude not found"))
        assert agentic_retrieve("query", config={}, deps=deps) == []

    def test_tool_call_cap_falls_back(self):
        """If the model never signals done, the loop must stop at max_tool_calls."""
        call_count = {"n": 0}

        def fake_llm_complete(prompt, config, timeout=None):
            call_count["n"] += 1
            return _llm(json.dumps({"tool": "search", "args": {"query": f"turn-{call_count['n']}"}}))

        search_calls = {"n": 0}

        def fake_search(args):
            search_calls["n"] += 1
            return {"results": []}

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete, search=fake_search, max_tool_calls=3)

        assert agentic_retrieve("query", config={}, deps=deps) == []
        assert search_calls["n"] == 3
        # One extra LLM call beyond the tool-call cap would mean the loop
        # didn't actually stop at the bound.
        assert call_count["n"] == 3

    def test_wall_clock_timeout_falls_back(self):
        """A simulated clock that blows the wall-clock budget must abort before any LLM call."""
        clock_values = iter([0.0, 61.0])
        deps = AgenticRetrievalDeps(
            llm_complete=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")),
            wall_clock_seconds=60,
        )
        deps._clock = lambda: next(clock_values)

        assert agentic_retrieve("query", config={}, deps=deps) == []

    def test_empty_results_after_hydration_is_treated_as_failure(self, tmp_vault):
        """done with paths that don't resolve to real notes hydrates to [] -- callers must fall back."""
        deps = AgenticRetrievalDeps(
            llm_complete=lambda *a, **k: _llm(json.dumps({"done": True, "results": ["notes/ghost.md"]})),
            vault_loader=lambda: tmp_vault,
        )
        assert agentic_retrieve("query", config={}, deps=deps) == []

    def test_done_with_no_results_returns_empty(self, tmp_vault):
        deps = AgenticRetrievalDeps(
            llm_complete=lambda *a, **k: _llm(json.dumps({"done": True, "results": []})),
            vault_loader=lambda: tmp_vault,
        )
        assert agentic_retrieve("query", config={}, deps=deps) == []

    def test_unknown_tool_name_is_observed_and_loop_continues(self, tmp_vault):
        _write_note(tmp_vault, "a", title="A")
        responses = [
            _llm(json.dumps({"tool": "delete_everything", "args": {}})),
            _llm(json.dumps({"done": True, "results": ["notes/a.md"]})),
        ]
        calls = {"n": 0}

        def fake_llm_complete(prompt, config, timeout=None):
            result = responses[calls["n"]]
            calls["n"] += 1
            return result

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete, vault_loader=lambda: tmp_vault)

        results = agentic_retrieve("query", config={}, deps=deps)

        assert calls["n"] == 2
        assert len(results) == 1

    def test_retrieval_agent_provider_and_model_override_llm_backend(self):
        seen_configs = []

        def fake_llm_complete(prompt, config, timeout=None):
            seen_configs.append(config)
            return _llm(json.dumps({"done": True, "results": []}))

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete)
        config = {
            "llm_backend": "claude",
            "llm_model": "sonnet",
            "retrieval_agent_provider": "pi",
            "retrieval_agent_model": "openrouter/deepseek/deepseek-v4-pro",
        }

        agentic_retrieve("query", config=config, deps=deps)

        assert seen_configs[0]["llm_backend"] == "pi"
        assert seen_configs[0]["llm_model"] == "openrouter/deepseek/deepseek-v4-pro"

    def test_falls_back_to_global_llm_backend_when_agent_specific_unset(self):
        seen_configs = []

        def fake_llm_complete(prompt, config, timeout=None):
            seen_configs.append(config)
            return _llm(json.dumps({"done": True, "results": []}))

        deps = AgenticRetrievalDeps(llm_complete=fake_llm_complete)
        config = {"llm_backend": "codex", "llm_model": "gpt-5"}

        agentic_retrieve("query", config=config, deps=deps)

        assert seen_configs[0]["llm_backend"] == "codex"
        assert seen_configs[0]["llm_model"] == "gpt-5"

    def test_observation_is_truncated_to_bound(self, tmp_vault):
        big_observation = {"results": [{"path": f"notes/{i}.md", "title": "x" * 200} for i in range(50)]}
        responses = [
            _llm(json.dumps({"tool": "search", "args": {"query": "x"}})),
            _llm(json.dumps({"done": True, "results": []})),
        ]
        calls = {"n": 0}
        seen_prompts = []

        def fake_llm_complete(prompt, config, timeout=None):
            seen_prompts.append(prompt)
            result = responses[calls["n"]]
            calls["n"] += 1
            return result

        deps = AgenticRetrievalDeps(
            llm_complete=fake_llm_complete,
            search=lambda args: big_observation,
            vault_loader=lambda: tmp_vault,
        )

        agentic_retrieve("query", config={}, deps=deps)

        # The second prompt includes the first observation -- it must be bounded.
        assert "[truncated]" in seen_prompts[1]


class TestAgenticRetrieveDefaultDeps:
    def test_default_deps_use_real_llm_complete(self):
        from memento.llm import llm_complete as real_llm_complete

        deps = AgenticRetrievalDeps()

        assert deps.llm_complete is real_llm_complete

    def test_agentic_retrieve_builds_default_deps_when_none_given(self):
        """deps=None must not raise -- it builds AgenticRetrievalDeps() internally.

        We can't observe the real llm_complete's subprocess call here (its
        defaults are bound at class-definition time, same as
        ExplicitSearchRuntime's DI pattern elsewhere in this codebase), but a
        missing/erroring CLI backend must still degrade to an empty list
        rather than raising, since this *is* the fallback contract's last
        line of defense.
        """
        with patch("memento.llm.subprocess.run", side_effect=FileNotFoundError("claude not found")):
            result = agentic_retrieve("some query", config={"llm_backend": "claude"})
        assert result == []
