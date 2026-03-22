import pytest
from memento_utils import rrf_fuse, is_vsearch_warm, mark_vsearch_warm


class TestRrfFuse:
    def test_fuse_two_lists_basic(self):
        """RRF of two lists produces merged ranking."""
        list1 = [
            {"path": "notes/a.md", "title": "A", "score": 0.9},
            {"path": "notes/b.md", "title": "B", "score": 0.7},
        ]
        list2 = [
            {"path": "notes/b.md", "title": "B", "score": 0.8},
            {"path": "notes/c.md", "title": "C", "score": 0.6},
        ]
        fused = rrf_fuse([list1, list2], k=60)
        paths = [r["path"] for r in fused]
        # B appears in both lists, should rank highest
        assert paths[0] == "notes/b.md"
        assert len(fused) == 3  # a, b, c

    def test_fuse_single_list(self):
        """RRF of a single list preserves order."""
        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9},
            {"path": "notes/b.md", "title": "B", "score": 0.7},
        ]
        fused = rrf_fuse([results], k=60)
        assert [r["path"] for r in fused] == ["notes/a.md", "notes/b.md"]

    def test_fuse_disjoint_lists(self):
        """RRF of disjoint lists interleaves by rank position."""
        list1 = [{"path": "notes/a.md", "title": "A", "score": 0.9}]
        list2 = [{"path": "notes/b.md", "title": "B", "score": 0.8}]
        fused = rrf_fuse([list1, list2], k=60)
        # Both rank 1 in their list -> equal RRF score -> either order is fine
        assert len(fused) == 2

    def test_fuse_empty_lists(self):
        """RRF of empty lists -> empty result."""
        assert rrf_fuse([[], []], k=60) == []

    def test_fuse_normalizes_scores(self):
        """Fused scores should be in 0-1 range."""
        list1 = [{"path": "notes/a.md", "title": "A", "score": 0.9}]
        fused = rrf_fuse([list1], k=60)
        assert 0 <= fused[0]["score"] <= 1.0

    def test_fuse_preserves_metadata(self):
        """Fused results should preserve title, snippet etc from best occurrence."""
        list1 = [{"path": "notes/a.md", "title": "A Title", "score": 0.9, "snippet": "hello"}]
        list2 = [{"path": "notes/a.md", "title": "A", "score": 0.8}]
        fused = rrf_fuse([list1, list2], k=60)
        assert fused[0]["title"] == "A Title"  # from highest-scored occurrence


class TestVsearchWarm:
    def test_warm_flag(self, tmp_path, monkeypatch):
        """mark_vsearch_warm creates flag, is_vsearch_warm detects it."""
        flag_path = str(tmp_path / "memento-vsearch-warm")
        monkeypatch.setattr("memento_utils.VSEARCH_WARM_PATH", flag_path)
        assert not is_vsearch_warm()
        mark_vsearch_warm()
        assert is_vsearch_warm()
