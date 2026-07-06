"""Phase 3: profile (curated) notes are exempt from decay, sweep, and resurfacing."""

from __future__ import annotations

from memento import config
from memento.config import is_curated_path


def _cfg(**profile_overrides):
    base = dict(config.DEFAULT_CONFIG)
    base["profile"] = {**config.DEFAULT_CONFIG["profile"], **profile_overrides}
    return base


def test_is_curated_path_true_for_profile():
    assert is_curated_path("profile/voice.md", config.DEFAULT_CONFIG) is True


def test_is_curated_path_false_for_regular_dirs():
    assert is_curated_path("notes/foo.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("fleeting/x.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("projects/bar.md", config.DEFAULT_CONFIG) is False
    assert is_curated_path("archive/old.md", config.DEFAULT_CONFIG) is False


def test_is_curated_path_custom_dir():
    cfg = _cfg(dir="persona")
    assert is_curated_path("persona/voice.md", cfg) is True
    assert is_curated_path("profile/voice.md", cfg) is False


def test_is_curated_path_edge_inputs():
    assert is_curated_path("", config.DEFAULT_CONFIG) is False
    assert is_curated_path("voice.md", config.DEFAULT_CONFIG) is False  # no dir segment
