"""Tests for the single-source vault content-dir registry (config.py).

Phase 1 of profile-as-first-class-memory: profile/ becomes an indexed,
source-taggable dir, and the ~7 hardcoded ("notes","fleeting","projects")
tuples derive from one registry.
"""

from __future__ import annotations

from memento import config
from memento.config import (
    DirSpec,
    archive_root_names,
    content_dirs,
    core_dir_names,
    expected_dir_names,
    indexed_dir_names,
    source_for_path,
)


def _cfg(**profile):
    base = dict(config.DEFAULT_CONFIG)
    if profile:
        base["profile"] = profile
    return base


def test_default_config_has_profile_block():
    assert config.DEFAULT_CONFIG["profile"] == {
        "dir": "profile",
        "searchable": True,
        "inject_into_briefing": True,
        "decay_exempt": True,
    }


def test_registry_specs():
    specs = {d.name: d for d in content_dirs(config.DEFAULT_CONFIG)}
    assert isinstance(specs["notes"], DirSpec)

    notes = specs["notes"]
    assert notes.indexed and notes.core and notes.health_expected and notes.archivable
    assert not notes.curated and not notes.inject_index

    archive = specs["archive"]
    assert archive.health_expected and archive.archivable
    assert not archive.indexed and not archive.core and not archive.curated

    profile = specs["profile"]
    assert profile.indexed and profile.archivable and profile.curated and profile.inject_index
    assert not profile.core
    # profile absence must not trip the health WARN, so it is NOT health_expected
    assert not profile.health_expected


def test_indexed_dir_names_includes_profile():
    assert indexed_dir_names(config.DEFAULT_CONFIG) == ("notes", "fleeting", "projects", "profile")


def test_core_dir_names_unchanged():
    assert core_dir_names(config.DEFAULT_CONFIG) == ("notes", "fleeting", "projects")


def test_expected_dir_names_preserve_health_behavior():
    # Behavior-preserving in Phase 1: profile is NOT expected (no warn on absence).
    assert expected_dir_names(config.DEFAULT_CONFIG) == ("notes", "fleeting", "projects", "archive")


def test_archive_root_names_include_profile():
    assert archive_root_names(config.DEFAULT_CONFIG) == ("notes", "fleeting", "projects", "archive", "profile")


def test_source_for_path_default_profile_dir():
    assert source_for_path("profile/voice.md", config.DEFAULT_CONFIG) == "profile"
    assert source_for_path("notes/foo.md", config.DEFAULT_CONFIG) == "note"
    assert source_for_path("projects/bar.md", config.DEFAULT_CONFIG) == "note"
    assert source_for_path("fleeting/x.md", config.DEFAULT_CONFIG) == "note"


def test_custom_profile_dir_name():
    cfg = _cfg(dir="persona", searchable=True, inject_into_briefing=True, decay_exempt=True)
    assert "persona" in indexed_dir_names(cfg)
    assert "profile" not in indexed_dir_names(cfg)
    assert source_for_path("persona/voice.md", cfg) == "profile"
    assert source_for_path("profile/voice.md", cfg) == "note"


def test_searchable_false_drops_profile_from_index_but_keeps_archivable():
    cfg = _cfg(dir="profile", searchable=False, inject_into_briefing=True, decay_exempt=True)
    assert "profile" not in indexed_dir_names(cfg)
    assert "profile" in archive_root_names(cfg)


def test_partial_profile_config_falls_back_to_defaults():
    # user overrides only `dir`; shallow-merged config drops other keys, so
    # accessors must re-apply defaults (searchable/decay_exempt default True).
    cfg = _cfg(dir="me")
    assert "me" in indexed_dir_names(cfg)
    specs = {d.name: d for d in content_dirs(cfg)}
    assert specs["me"].curated
    assert specs["me"].inject_index
