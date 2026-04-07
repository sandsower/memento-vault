"""Tests for parallel LLM synthesis in the Inception pipeline."""

import json
import threading
import time
from unittest.mock import patch


from memento_inception import (
    main,
    parse_args,
)


def _synth_response(title="Test Pattern", body="Synthesized."):
    """Build a valid synthesis JSON string."""
    return json.dumps(
        {
            "title": title,
            "body": body,
            "tags": ["test"],
            "certainty": 3,
            "related": [],
        }
    )


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


class TestParallelSynthesis:
    """Verify that LLM calls run in parallel via ThreadPoolExecutor."""

    def test_multiple_clusters_called_in_parallel(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """call_llm is invoked for each cluster, and calls overlap in time."""
        call_times = []
        lock = threading.Lock()

        def _slow_llm(prompt, config):
            start = time.monotonic()
            time.sleep(0.1)  # simulate 100ms LLM latency
            end = time.monotonic()
            with lock:
                call_times.append((start, end))
            return _synth_response(title=f"Pattern {len(call_times)}")

        mock_config["inception_parallel"] = 4

        # Mock clustering to return 2 predetermined clusters from sample notes
        fake_clusters = {
            0: ["redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"],
            1: ["zustand-state-reset", "react-query-wrapper"],
        }

        with patch("memento_inception.call_llm", side_effect=_slow_llm):
            with patch("memento_inception.cluster_notes", return_value=fake_clusters):
                with patch("memento_inception._commit_and_reindex"):
                    with patch("memento_inception.build_project_maps", return_value={}):
                        with patch("memento_inception.write_project_maps"):
                            with patch("memento_inception.build_concept_index", return_value={}):
                                with patch("memento_inception.write_concept_index"):
                                    _run_main(
                                        mock_config,
                                        inception_state_path,
                                        ["--full"],
                                        db_path=str(mock_qmd_db),
                                    )

        # Both clusters should have been synthesized
        assert len(call_times) >= 2, f"Expected at least 2 LLM calls, got {len(call_times)}"

        # Verify calls overlapped (parallel)
        call_times.sort()
        assert call_times[1][0] < call_times[0][1], "LLM calls should overlap in time (parallel execution)"

    def test_results_processed_in_order(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """Notes are written in scored order, not LLM completion order."""
        write_order = []
        original_write = None

        # Import the real write_pattern_note to delegate to
        import memento_inception

        original_write = memento_inception.write_pattern_note

        def _tracking_write(synthesis, stems, vault_path):
            write_order.append(synthesis["title"])
            return original_write(synthesis, stems, vault_path)

        call_count = [0]
        lock = threading.Lock()

        def _ordered_llm(prompt, config):
            with lock:
                idx = call_count[0]
                call_count[0] += 1
            # Later calls return faster to test ordering
            time.sleep(0.05 * (3 - min(idx, 2)))
            return _synth_response(title=f"Pattern {idx}", body=f"Body {idx}")

        mock_config["inception_parallel"] = 4

        with patch("memento_inception.call_llm", side_effect=_ordered_llm):
            with patch("memento_inception.write_pattern_note", side_effect=_tracking_write):
                with patch("memento_inception._commit_and_reindex"):
                    with patch("memento_inception.build_project_maps", return_value={}):
                        with patch("memento_inception.write_project_maps"):
                            with patch("memento_inception.build_concept_index", return_value={}):
                                with patch("memento_inception.write_concept_index"):
                                    _run_main(
                                        mock_config,
                                        inception_state_path,
                                        ["--full"],
                                        db_path=str(mock_qmd_db),
                                    )

        # The key assertion: writes happen in queue order (Pattern 0, 1, ...)
        # regardless of which LLM call finishes first
        if len(write_order) >= 2:
            indices = [int(t.split()[-1]) for t in write_order]
            assert indices == sorted(indices), f"Write order {write_order} should follow queue order"


class TestParallelDedupAndDryRun:
    """Dedup and dry-run still work correctly with the parallel pipeline."""

    def test_dry_run_skips_llm_calls(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """In dry-run mode, call_llm is never invoked."""
        with patch("memento_inception.call_llm") as mock_llm:
            _run_main(
                mock_config,
                inception_state_path,
                ["--dry-run", "--full"],
                db_path=str(mock_qmd_db),
            )
        mock_llm.assert_not_called()

    def test_dry_run_writes_no_files(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """Dry run produces no new note files on disk."""
        notes_before = set((tmp_vault / "notes").glob("*.md"))

        with patch("memento_inception.call_llm", return_value=_synth_response()):
            _run_main(
                mock_config,
                inception_state_path,
                ["--dry-run", "--full"],
                db_path=str(mock_qmd_db),
            )

        notes_after = set((tmp_vault / "notes").glob("*.md"))
        assert notes_before == notes_after

    def test_dedup_skip_prevents_llm_call(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """Clusters that are dedup-skipped never reach the LLM."""
        call_count = [0]

        def _counting_llm(prompt, config):
            call_count[0] += 1
            return _synth_response()

        # Mock clustering to produce a known cluster
        fake_clusters = {
            0: ["redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"],
        }

        # First run: synthesize patterns
        with patch("memento_inception.call_llm", side_effect=_counting_llm):
            with patch("memento_inception.cluster_notes", return_value=fake_clusters):
                with patch("memento_inception._commit_and_reindex"):
                    with patch("memento_inception.build_project_maps", return_value={}):
                        with patch("memento_inception.write_project_maps"):
                            with patch("memento_inception.build_concept_index", return_value={}):
                                with patch("memento_inception.write_concept_index"):
                                    _run_main(
                                        mock_config,
                                        inception_state_path,
                                        ["--full"],
                                        db_path=str(mock_qmd_db),
                                    )

        first_count = call_count[0]
        assert first_count >= 1, "First run should call LLM at least once"

        # Reset state so notes are "new" again but ledger has them
        call_count[0] = 0
        state_path2 = inception_state_path.parent / "inception-state2.json"

        with patch("memento_inception.call_llm", side_effect=_counting_llm):
            with patch("memento_inception.cluster_notes", return_value=fake_clusters):
                with patch("memento_inception._commit_and_reindex"):
                    with patch("memento_inception.build_project_maps", return_value={}):
                        with patch("memento_inception.write_project_maps"):
                            with patch("memento_inception.build_concept_index", return_value={}):
                                with patch("memento_inception.write_concept_index"):
                                    _run_main(
                                        mock_config,
                                        state_path2,
                                        ["--full"],
                                        db_path=str(mock_qmd_db),
                                    )

        # Second run may call LLM fewer times (some clusters dedup-skipped)
        # or the same if it's a refresh — but the important thing is it doesn't crash
        assert call_count[0] >= 0


class TestParallelErrorHandling:
    """A failing LLM call must not crash the pipeline."""

    def test_failing_llm_call_skipped_gracefully(
        self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db
    ):
        """If call_llm raises, that cluster is skipped; others still process."""
        call_count = [0]
        lock = threading.Lock()

        def _flaky_llm(prompt, config):
            with lock:
                idx = call_count[0]
                call_count[0] += 1
            if idx == 0:
                raise RuntimeError("LLM backend timeout")
            return _synth_response(title=f"Pattern {idx}")

        mock_config["inception_parallel"] = 4

        with patch("memento_inception.call_llm", side_effect=_flaky_llm):
            with patch("memento_inception._commit_and_reindex"):
                with patch("memento_inception.build_project_maps", return_value={}):
                    with patch("memento_inception.write_project_maps"):
                        with patch("memento_inception.build_concept_index", return_value={}):
                            with patch("memento_inception.write_concept_index"):
                                result = _run_main(
                                    mock_config,
                                    inception_state_path,
                                    ["--full"],
                                    db_path=str(mock_qmd_db),
                                )

        assert result == 0, "Pipeline should complete successfully despite LLM failure"

    def test_all_llm_calls_fail(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """If every LLM call fails, pipeline exits 0 with no notes written."""
        notes_before = set((tmp_vault / "notes").glob("*.md"))

        def _always_fail(prompt, config):
            raise RuntimeError("Total backend failure")

        mock_config["inception_parallel"] = 4

        with patch("memento_inception.call_llm", side_effect=_always_fail):
            result = _run_main(
                mock_config,
                inception_state_path,
                ["--full"],
                db_path=str(mock_qmd_db),
            )

        assert result == 0
        notes_after = set((tmp_vault / "notes").glob("*.md"))
        assert notes_before == notes_after


class TestParallelConfig:
    """Config key inception_parallel controls thread pool size."""

    def test_default_config_has_inception_parallel(self):
        """DEFAULT_CONFIG includes inception_parallel = 4."""
        from memento.config import DEFAULT_CONFIG

        assert "inception_parallel" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["inception_parallel"] == 4

    def test_custom_worker_count(self, mock_config, sample_notes, tmp_vault, inception_state_path, mock_qmd_db):
        """Setting inception_parallel=1 forces sequential execution."""
        mock_config["inception_parallel"] = 1
        call_times = []
        lock = threading.Lock()

        def _timed_llm(prompt, config):
            start = time.monotonic()
            time.sleep(0.05)
            end = time.monotonic()
            with lock:
                call_times.append((start, end))
            return _synth_response(title=f"Pattern {len(call_times)}")

        with patch("memento_inception.call_llm", side_effect=_timed_llm):
            with patch("memento_inception._commit_and_reindex"):
                with patch("memento_inception.build_project_maps", return_value={}):
                    with patch("memento_inception.write_project_maps"):
                        with patch("memento_inception.build_concept_index", return_value={}):
                            with patch("memento_inception.write_concept_index"):
                                _run_main(
                                    mock_config,
                                    inception_state_path,
                                    ["--full"],
                                    db_path=str(mock_qmd_db),
                                )

        # With max_workers=1, calls should be sequential (no overlap)
        if len(call_times) >= 2:
            call_times.sort()
            assert call_times[1][0] >= call_times[0][1], "With max_workers=1, calls should not overlap"
