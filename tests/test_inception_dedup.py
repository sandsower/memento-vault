"""Tests for the three-layer dedup system in Inception."""

from pathlib import Path


def _write_note(path, *, title, note_type="discovery", tags=None, date="",
                certainty=None, project=None, source=None,
                synthesized_from=None, body=""):
    """Helper: write a markdown note with YAML frontmatter."""
    lines = ["---"]
    lines.append(f"title: {title}")
    lines.append(f"type: {note_type}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    if date:
        lines.append(f"date: {date}")
    if certainty is not None:
        lines.append(f"certainty: {certainty}")
    if project:
        lines.append(f"project: {project}")
    if source:
        lines.append(f"source: {source}")
    if synthesized_from:
        lines.append("synthesized_from:")
        for s in synthesized_from:
            lines.append(f"  - {s}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


class TestBuildSynthesizedFromLedger:
    """Tests for build_synthesized_from_ledger."""

    def test_ledger_empty_dir(self, tmp_path):
        """Empty notes dir returns empty dict."""
        from memento_inception import build_synthesized_from_ledger

        empty_dir = tmp_path / "notes"
        empty_dir.mkdir()

        result = build_synthesized_from_ledger(str(empty_dir))
        assert result == {}

    def test_ledger_finds_inception_notes(self, tmp_path):
        """Dir with inception-sourced notes builds correct ledger mapping."""
        from memento_inception import build_synthesized_from_ledger

        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        _write_note(
            notes_dir / "cross-project-redis-patterns.md",
            title="Cross-project Redis patterns",
            note_type="pattern",
            source="inception",
            synthesized_from=["redis-cache-ttl", "redis-eviction-policy"],
            body="Redis caching patterns recur across services.",
        )
        _write_note(
            notes_dir / "frontend-testing-patterns.md",
            title="Frontend testing patterns",
            note_type="pattern",
            source="inception",
            synthesized_from=["zustand-state-reset", "react-query-wrapper"],
            body="Testing patterns for React frontends.",
        )

        result = build_synthesized_from_ledger(str(notes_dir))

        assert "cross-project-redis-patterns" in result
        assert result["cross-project-redis-patterns"] == {
            "redis-cache-ttl", "redis-eviction-policy"
        }
        assert "frontend-testing-patterns" in result
        assert result["frontend-testing-patterns"] == {
            "zustand-state-reset", "react-query-wrapper"
        }

    def test_ledger_ignores_non_inception(self, tmp_path):
        """Notes without source:inception are not included in the ledger."""
        from memento_inception import build_synthesized_from_ledger

        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        # A human-authored note (no source field)
        _write_note(
            notes_dir / "human-discovery.md",
            title="Human discovery",
            note_type="discovery",
            tags=["redis"],
            body="A human-written note.",
        )
        # A inception note that should be picked up
        _write_note(
            notes_dir / "inception-pattern.md",
            title="Inception pattern",
            note_type="pattern",
            source="inception",
            synthesized_from=["alpha", "beta"],
            body="Synthesized pattern.",
        )

        result = build_synthesized_from_ledger(str(notes_dir))

        assert "human-discovery" not in result
        assert "inception-pattern" in result
        assert result["inception-pattern"] == {"alpha", "beta"}


class TestCheckLedgerDedup:
    """Tests for check_ledger_dedup."""

    def test_dedup_skip_exact_match(self):
        """Cluster stems == existing pattern sources -> skip."""
        from memento_inception import check_ledger_dedup

        ledger = {
            "redis-patterns": {"redis-cache-ttl", "redis-eviction-policy"},
        }
        cluster_stems = ["redis-cache-ttl", "redis-eviction-policy"]

        action, stem = check_ledger_dedup(cluster_stems, ledger)
        assert action == "skip"
        assert stem is None

    def test_dedup_skip_subset(self):
        """Cluster is a subset of existing pattern sources -> skip."""
        from memento_inception import check_ledger_dedup

        ledger = {
            "redis-patterns": {"redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"},
        }
        cluster_stems = ["redis-cache-ttl", "redis-eviction-policy"]

        action, stem = check_ledger_dedup(cluster_stems, ledger)
        assert action == "skip"
        assert stem is None

    def test_dedup_merge_superset(self):
        """Cluster is a superset of existing pattern -> merge with that stem."""
        from memento_inception import check_ledger_dedup

        ledger = {
            "redis-patterns": {"redis-cache-ttl", "redis-eviction-policy"},
        }
        cluster_stems = ["redis-cache-ttl", "redis-eviction-policy", "redis-cache-invalidation"]

        action, stem = check_ledger_dedup(cluster_stems, ledger)
        assert action == "merge"
        assert stem == "redis-patterns"

    def test_dedup_create_novel(self):
        """No overlap with existing patterns -> create new."""
        from memento_inception import check_ledger_dedup

        ledger = {
            "redis-patterns": {"redis-cache-ttl", "redis-eviction-policy"},
        }
        cluster_stems = ["zustand-state-reset", "react-query-wrapper"]

        action, stem = check_ledger_dedup(cluster_stems, ledger)
        assert action == "create"
        assert stem is None


class TestCheckTitleOverlap:
    """Tests for check_title_overlap."""

    def test_title_overlap_high(self):
        """High token overlap (>0.60) returns True."""
        from memento_inception import check_title_overlap

        slug = "redis-cache-ttl-patterns"
        existing_stems = ["redis-cache-ttl"]

        assert check_title_overlap(slug, existing_stems) is True

    def test_title_overlap_low(self):
        """Low token overlap returns False."""
        from memento_inception import check_title_overlap

        slug = "zustand-testing-mocks"
        existing_stems = ["redis-cache-ttl"]

        assert check_title_overlap(slug, existing_stems) is False

    def test_title_overlap_rephrased(self):
        """Rephrased title with 70% overlap is caught at 0.60 threshold."""
        from memento_inception import check_title_overlap

        # Real case: "prefer-the-tightest-real-constraint-over-the-convenient-proxy"
        # vs "prefer-the-real-governing-constraint-over-the-convenient-proxy"
        slug = "prefer-the-real-governing-constraint-over-the-convenient-proxy"
        existing_stems = ["prefer-the-tightest-real-constraint-over-the-convenient-proxy"]

        assert check_title_overlap(slug, existing_stems) is True

    def test_title_overlap_empty(self):
        """Empty slug returns False."""
        from memento_inception import check_title_overlap

        assert check_title_overlap("", existing_stems=["redis-cache-ttl"]) is False
