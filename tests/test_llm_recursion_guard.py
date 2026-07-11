"""Guard against the agentic-retrieval / triage fork bomb.

memento spawns headless `claude --print` children for agentic retrieval and
triage synthesis. Each child starts a fresh Claude Code session that re-fires
the SessionStart/UserPromptSubmit/SessionEnd hooks; without a re-entrancy guard
those hooks spawn another child, which fires the hooks again, recursively. The
guard is an environment marker set on every LLM child, checked by the hook
entry points so they no-op inside a child.
"""

import subprocess

from memento import llm
from memento.llm import LLM_SUBPROCESS_ENV, _run_cli, in_llm_subprocess
from memento.lifecycle import build_briefing, build_recall


class TestInLlmSubprocess:
    def test_false_by_default(self, monkeypatch):
        monkeypatch.delenv(LLM_SUBPROCESS_ENV, raising=False)
        assert in_llm_subprocess() is False

    def test_true_when_marker_set(self, monkeypatch):
        monkeypatch.setenv(LLM_SUBPROCESS_ENV, "1")
        assert in_llm_subprocess() is True

    def test_other_values_do_not_count(self, monkeypatch):
        monkeypatch.setenv(LLM_SUBPROCESS_ENV, "0")
        assert in_llm_subprocess() is False


class TestRunCliMarksChildEnv:
    """Every LLM CLI child must inherit the re-entrancy marker so its own
    memento hooks recognise themselves as a child and stand down."""

    def _capture_env(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(llm.subprocess, "run", fake_run)
        return captured

    def test_marker_set_without_stdin(self, monkeypatch):
        captured = self._capture_env(monkeypatch)
        _run_cli(["echo", "hi"])
        assert captured.get(LLM_SUBPROCESS_ENV) == "1"

    def test_marker_set_with_stdin(self, monkeypatch):
        captured = self._capture_env(monkeypatch)
        _run_cli(["cat"], stdin_input="prompt text")
        assert captured.get(LLM_SUBPROCESS_ENV) == "1"

    def test_parent_env_is_preserved(self, monkeypatch):
        monkeypatch.setenv("MEMENTO_SENTINEL_FOR_TEST", "kept")
        captured = self._capture_env(monkeypatch)
        _run_cli(["echo", "hi"])
        assert captured.get("MEMENTO_SENTINEL_FOR_TEST") == "kept"
        assert captured.get(LLM_SUBPROCESS_ENV) == "1"


class TestHooksNoOpInChild:
    """The hooks that trigger LLM children must produce nothing when they are
    themselves running inside a child — that is what breaks the recursion."""

    def test_build_briefing_noops(self, monkeypatch):
        monkeypatch.setenv(LLM_SUBPROCESS_ENV, "1")
        result = build_briefing("/tmp/some-project", session_id="s1")
        assert result.should_inject is False
        assert result.reason == "llm-subprocess"

    def test_build_recall_noops(self, monkeypatch):
        monkeypatch.setenv(LLM_SUBPROCESS_ENV, "1")
        result = build_recall("what did we decide about caching", "/tmp/some-project", "s1")
        assert result.should_inject is False
        assert result.reason == "llm-subprocess"

    def test_build_briefing_runs_when_not_a_child(self, monkeypatch):
        # Sanity: without the marker the guard must not fire (it falls through
        # to the normal path, which may still decline for other reasons — we
        # only assert it did not short-circuit on "llm-subprocess").
        monkeypatch.delenv(LLM_SUBPROCESS_ENV, raising=False)
        result = build_briefing("", session_id="s1")
        assert result.reason != "llm-subprocess"
