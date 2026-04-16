"""Tests for the Inception main pipeline."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


from memento_inception import (
    main,
    check_dependencies,
    parse_args,
)


class TestCheckDependencies:
    def test_passes_when_all_installed(self):
        """No exception when numpy, hdbscan, sklearn are importable."""
        # These are installed in the test venv
        check_dependencies()  # should not raise

    def test_fails_when_missing(self):
        """Returns list of missing packages."""
        with patch.dict(sys.modules, {"hdbscan": None}):
            with patch("builtins.__import__", side_effect=_selective_import_error("hdbscan")):
                missing = check_dependencies()
                assert "hdbscan" in missing


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.dry_run is False
        assert args.full is False
        assert args.max_clusters is None
        assert args.verbose is False

    def test_dry_run(self):
        args = parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_full(self):
        args = parse_args(["--full"])
        assert args.full is True

    def test_max_clusters(self):
        args = parse_args(["--max-clusters", "5"])
        assert args.max_clusters == 5

    def test_verbose(self):
        args = parse_args(["--verbose"])
        assert args.verbose is True


class TestMainPipeline:
    def test_exits_0_when_disabled(self, mock_config, tmp_vault, inception_state_path):
        """When inception_enabled=False and not --full, exits 0."""
        mock_config["inception_enabled"] = False
        result = _run_main(mock_config, inception_state_path, [])
        assert result == 0

    def test_exits_0_when_no_notes(self, mock_config, tmp_vault, inception_state_path):
        """Empty vault exits 0 cleanly."""
        # Remove all notes
        for f in (tmp_vault / "notes").glob("*.md"):
            f.unlink()
        result = _run_main(mock_config, inception_state_path, ["--full"])
        assert result == 0

    def test_dry_run_writes_no_files(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """Dry run prints clusters but writes no new note files."""
        notes_before = set((tmp_vault / "notes").glob("*.md"))

        with _mock_llm_response():
            result = _run_main(
                mock_config,
                inception_state_path,
                ["--dry-run", "--full"],
                db_path=str(mock_qmd_db),
            )

        notes_after = set((tmp_vault / "notes").glob("*.md"))
        assert result == 0
        assert notes_before == notes_after

    def test_lock_prevents_concurrent(self, mock_config, sample_notes, tmp_vault, inception_state_path):
        """If lock is held, exits 1."""
        lock_path = str(tmp_vault / "inception.lock")
        # Write a lock with our own PID (simulates another instance)
        Path(lock_path).write_text(str(os.getpid()))

        result = _run_main(mock_config, inception_state_path, ["--full"], lock_path=lock_path)
        assert result == 1

        # Cleanup
        Path(lock_path).unlink(missing_ok=True)

    def test_state_updated_after_run(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """After a successful run, state file is updated."""
        with _mock_llm_response():
            result = _run_main(
                mock_config,
                inception_state_path,
                ["--full"],
                db_path=str(mock_qmd_db),
            )

        assert result == 0
        assert inception_state_path.exists()
        state = json.loads(inception_state_path.read_text())
        assert state["last_run_iso"] is not None
        assert len(state["runs"]) >= 1

    def test_handles_zero_clusters(self, mock_config, tmp_vault, inception_state_path, mock_qmd_db):
        """When HDBSCAN finds no clusters, exits 0 without error."""
        # Create just 2 very different notes (won't cluster with min_cluster_size=3)
        for i, (stem, tag) in enumerate([("note-alpha", "alpha"), ("note-beta", "beta")]):
            (tmp_vault / "notes" / f"{stem}.md").write_text(
                f"---\ntitle: {stem}\ntype: discovery\ntags: [{tag}]\n"
                f"date: 2026-03-22T10:0{i}\n---\n\nSome content about {tag}.\n"
            )
        result = _run_main(mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db))
        assert result == 0

    def test_processed_notes_empty_when_below_cluster_min(
        self, mock_config, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """Too few notes to cluster must not mark anything processed."""
        for i, (stem, tag) in enumerate([("solo-a", "alpha"), ("solo-b", "beta")]):
            (tmp_vault / "notes" / f"{stem}.md").write_text(
                f"---\ntitle: {stem}\ntype: discovery\ntags: [{tag}]\n"
                f"date: 2026-03-22T10:0{i}\n---\n\nSome content about {tag}.\n"
            )
        result = _run_main(mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db))
        assert result == 0
        state = json.loads(inception_state_path.read_text())
        assert state["processed_notes"] == []
        assert state["last_run_iso"] is not None
        assert len(state["runs"]) >= 1

    def test_processed_notes_empty_on_llm_failure(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """LLM failure on an unledgered cluster leaves processed_notes empty."""
        # Force a cluster of notes that have NO pre-existing pattern hit, so
        # the only way to consolidate them is writing a new pattern note.
        # With LLM returning empty, synthesis fails and nothing gets written.
        with _force_cluster(["zustand-state-reset", "react-query-wrapper"]):
            with patch("memento_inception.call_llm", return_value=""):
                result = _run_main(
                    mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db)
                )
        assert result == 0
        state = json.loads(inception_state_path.read_text())
        assert "zustand-state-reset" not in state["processed_notes"]
        assert "react-query-wrapper" not in state["processed_notes"]

    def test_processed_notes_populated_when_pattern_written(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """After a real pattern is written, its source stems are marked consolidated."""
        # Remove the pre-seeded pattern so check_ledger_dedup returns "create".
        (tmp_vault / "notes" / "existing-pattern.md").unlink()
        with _force_cluster(["redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"]):
            with _mock_llm_response():
                result = _run_main(
                    mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db)
                )
        assert result == 0
        state = json.loads(inception_state_path.read_text())
        processed = set(state["processed_notes"])
        assert {"redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"}.issubset(processed)

    def test_processed_notes_marked_when_ledger_skip(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """Cluster exactly matching an existing pattern → check_ledger_dedup skip → stems marked."""
        # sample_notes' existing-pattern has synthesized_from =
        # ["redis-cache-ttl", "redis-eviction-policy"]. Force a cluster of
        # that exact set → check_ledger_dedup returns ("skip", None) → stems
        # get added to consolidated_stems without any LLM call.
        with _force_cluster(["redis-cache-ttl", "redis-eviction-policy"]):
            result = _run_main(
                mock_config, inception_state_path, ["--full"], db_path=str(mock_qmd_db)
            )
        assert result == 0
        state = json.loads(inception_state_path.read_text())
        processed = set(state["processed_notes"])
        assert {"redis-cache-ttl", "redis-eviction-policy"}.issubset(processed)

    def test_processed_notes_unchanged_on_dry_run(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """Dry run updates run metadata but never extends processed_notes."""
        (tmp_vault / "notes" / "existing-pattern.md").unlink()
        with _force_cluster(["redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"]):
            with _mock_llm_response():
                result = _run_main(
                    mock_config,
                    inception_state_path,
                    ["--dry-run", "--full"],
                    db_path=str(mock_qmd_db),
                )
        assert result == 0
        state = json.loads(inception_state_path.read_text())
        assert state["processed_notes"] == []
        assert len(state["runs"]) >= 1
        assert state["runs"][-1]["dry_run"] is True


# --- Helpers ---


def _selective_import_error(blocked_module):
    """Create an import function that blocks a specific module."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"No module named '{blocked_module}'")
        return real_import(name, *args, **kwargs)

    return _import


def _force_cluster(stems):
    """Patch cluster_notes to return a single deterministic cluster.

    Lets tests exercise the consolidation-tracking paths without depending on
    HDBSCAN finding enough density in mock embeddings.
    """
    return patch("memento_inception.cluster_notes", return_value={0: list(stems)})


def _mock_llm_response():
    """Patch call_llm to return a valid synthesis JSON."""
    response = json.dumps(
        {
            "title": "Test Pattern Note",
            "body": "This is a synthesized pattern across multiple notes.",
            "tags": ["test", "pattern"],
            "certainty": 3,
            "related": [],
        }
    )
    return patch("memento_inception.call_llm", return_value=response)


def _run_main(config, state_path, argv, db_path=None, lock_path=None):
    """Run the main pipeline with mocked config and paths."""
    args = parse_args(argv)

    with patch("memento_inception.get_config", return_value=config):
        kwargs = {}
        if db_path:
            kwargs["db_path"] = db_path
        if lock_path:
            kwargs["lock_path"] = lock_path

        return main(args, state_path=str(state_path), **kwargs)
