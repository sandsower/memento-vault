"""End-to-end Phase 1: profile/ is indexed and its hits are tagged source=profile.

Exercises the full vertical path: config registry -> indexer -> search backend
-> shape_search_results, proving profile facts are now discoverable in-session.
"""

from __future__ import annotations

import pytest

from memento.config import reset_config
from memento.embedded_search import EmbeddedSearchBackend
from memento.indexer import scan_and_index
from memento.search import shape_search_results
from memento.search_backend import reset_backend


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    for d in ("notes", "fleeting", "projects", "profile"):
        (v / d).mkdir(parents=True)
    (v / "notes" / "alpha.md").write_text("---\ntitle: Alpha\n---\n\nRedis notes.\n")
    (v / "profile" / "voice.md").write_text(
        "---\nname: voice\ndescription: writing voice\n---\n\n"
        "This is the writing voice: no em dashes, no semicolons, no inside speak.\n"
    )
    return v


@pytest.fixture
def backend(vault):
    b = EmbeddedSearchBackend(vault_path=vault, db_path=vault / ".search" / "search.db")
    yield b
    b.close()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    reset_backend()
    reset_config()


def test_profile_file_gets_indexed(vault, backend):
    stats = scan_and_index(vault, backend)
    # notes/alpha.md + profile/voice.md (fleeting/ and projects/ are empty)
    assert stats["indexed"] == 2


def test_profile_hit_is_searchable_and_source_tagged(vault, backend):
    scan_and_index(vault, backend)

    raw = backend.search("voice", "memento")
    assert any(r["path"] == "profile/voice.md" for r in raw), "profile file should be searchable"

    shaped = shape_search_results(raw, vault=vault)
    voice = next(e for e in shaped["results"] if e["path"] == "profile/voice.md")
    assert voice["source"] == "profile"
