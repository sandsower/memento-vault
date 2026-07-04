"""Tests for local non-MCP retrieval CLI surfaces."""

from memento import __main__ as memento_main


def test_local_search_cli_emits_search_envelope(monkeypatch, capsys):
    class FakeRuntime:
        def search(self, request):
            assert request.query == "redis cache"
            return {
                "results": [{"path": "notes/redis.md", "title": "Redis", "score": 0.9}],
                "metadata": {"backend": "FakeBackend"},
            }

    monkeypatch.setattr("memento.retrieval_policy.ExplicitSearchRuntime", lambda: FakeRuntime())

    code = memento_main.main(["search", "redis", "cache"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"notes/redis.md"' in out
    assert '"backend": "FakeBackend"' in out


def test_local_recall_cli_uses_build_recall_readonly(monkeypatch, capsys):
    calls = []

    class FakeResult:
        def to_dict(self):
            return {"should_inject": True, "content": "[vault] Related memories", "results": []}

    def fake_build_recall(prompt, cwd="", session_id="unknown", record=True, host_id=None):
        calls.append((prompt, cwd, session_id, record, host_id))
        return FakeResult()

    monkeypatch.setattr("memento.lifecycle.build_recall", fake_build_recall)

    code = memento_main.main(["recall", "prior", "context", "--cwd", "/repo", "--session-id", "s1"])

    assert code == 0
    assert calls == [("prior context", "/repo", "s1", False, "cli")]
    assert '"should_inject": true' in capsys.readouterr().out
