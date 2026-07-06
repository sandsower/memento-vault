"""Phase 2: profile index auto-injection into the session briefing."""

from __future__ import annotations

from memento import config
from memento.lifecycle import profile_briefing_section

_LABEL = "[vault] Profile (curated - read before drafting public writing, bios, or copy):"


def _cfg(**profile_overrides):
    base = dict(config.DEFAULT_CONFIG)
    base["profile"] = {**config.DEFAULT_CONFIG["profile"], **profile_overrides}
    return base


def _make_vault(tmp_path, *, profile_dir="profile", index_text="# Agent profile index\n\n- voice - no em dashes"):
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    if profile_dir is not None:
        pdir = vault / profile_dir
        pdir.mkdir(parents=True)
        if index_text is not None:
            (pdir / "PROFILE.md").write_text(index_text)
    return vault


def test_returns_labeled_block_when_present_and_enabled(tmp_path):
    vault = _make_vault(tmp_path)
    section = profile_briefing_section(vault, _cfg())
    assert section is not None
    assert section.startswith(_LABEL)
    assert "voice - no em dashes" in section
    # leading H1 from PROFILE.md is stripped (our label replaces it)
    assert "# Agent profile index" not in section


def test_none_when_injection_disabled(tmp_path):
    vault = _make_vault(tmp_path)
    assert profile_briefing_section(vault, _cfg(inject_into_briefing=False)) is None


def test_none_when_profile_md_absent(tmp_path):
    vault = _make_vault(tmp_path, index_text=None)  # profile/ dir but no PROFILE.md
    assert profile_briefing_section(vault, _cfg()) is None


def test_none_when_profile_dir_absent(tmp_path):
    vault = _make_vault(tmp_path, profile_dir=None)
    assert profile_briefing_section(vault, _cfg()) is None


def test_none_when_index_empty(tmp_path):
    vault = _make_vault(tmp_path, index_text="   \n\n")
    assert profile_briefing_section(vault, _cfg()) is None


def test_oversized_index_is_truncated(tmp_path):
    big = "# Agent profile index\n\n" + ("- fact line padding\n" * 400)  # well over 2000 chars
    vault = _make_vault(tmp_path, index_text=big)
    section = profile_briefing_section(vault, _cfg())
    assert section is not None
    assert "truncated" in section.lower()
    # bounded: label + cap + short marker, with generous slack
    assert len(section) < len(_LABEL) + 2200


def test_custom_profile_dir_name(tmp_path):
    vault = _make_vault(tmp_path, profile_dir="persona", index_text="# idx\n\n- voice fact")
    section = profile_briefing_section(vault, _cfg(dir="persona"))
    assert section is not None
    assert "voice fact" in section


def test_build_briefing_includes_profile_section(tmp_path, monkeypatch):
    """End-to-end: build_briefing surfaces the profile index in its content."""
    import memento.lifecycle as lc
    import memento.remote_client as rc

    vault = _make_vault(tmp_path, index_text="# Agent profile index\n\n- voice - no em dashes")

    monkeypatch.setattr(lc, "get_config", lambda: dict(config.DEFAULT_CONFIG))
    monkeypatch.setattr(lc, "get_vault", lambda: vault)
    monkeypatch.setattr(lc, "get_git_branch", lambda cwd: "main")
    monkeypatch.setattr(lc, "detect_project", lambda cwd, branch: ("proj", None))
    monkeypatch.setattr(lc, "read_project_index", lambda slug: ([], []))
    monkeypatch.setattr(lc, "triage_health_warning", lambda **kw: "")
    monkeypatch.setattr(lc, "pi_bridge_health_warning", lambda **kw: "")
    monkeypatch.setattr(lc, "has_qmd", lambda: False)
    monkeypatch.setattr(lc, "load_or_build_graph", lambda v: None)
    monkeypatch.setattr(rc, "is_remote", lambda: False)

    result = lc.build_briefing(cwd=str(tmp_path), session_id="s", allow_deferred=False)

    assert result.should_inject
    assert _LABEL in result.content
    assert "voice - no em dashes" in result.content


def test_build_briefing_omits_profile_when_disabled(tmp_path, monkeypatch):
    import memento.lifecycle as lc
    import memento.remote_client as rc

    vault = _make_vault(tmp_path, index_text="# idx\n\n- voice fact")
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["profile"] = {**config.DEFAULT_CONFIG["profile"], "inject_into_briefing": False}

    monkeypatch.setattr(lc, "get_config", lambda: cfg)
    monkeypatch.setattr(lc, "get_vault", lambda: vault)
    monkeypatch.setattr(lc, "get_git_branch", lambda cwd: "main")
    monkeypatch.setattr(lc, "detect_project", lambda cwd, branch: ("proj", None))
    monkeypatch.setattr(lc, "read_project_index", lambda slug: ([], []))
    monkeypatch.setattr(lc, "triage_health_warning", lambda **kw: "")
    monkeypatch.setattr(lc, "pi_bridge_health_warning", lambda **kw: "")
    monkeypatch.setattr(lc, "has_qmd", lambda: False)
    monkeypatch.setattr(lc, "load_or_build_graph", lambda v: None)
    monkeypatch.setattr(rc, "is_remote", lambda: False)

    result = lc.build_briefing(cwd=str(tmp_path), session_id="s", allow_deferred=False)

    assert result.should_inject
    assert _LABEL not in result.content
