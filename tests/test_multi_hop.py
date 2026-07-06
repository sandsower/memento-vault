"""Tests for multi-hop retrieval via wikilink-following."""

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from memento.config import DEFAULT_CONFIG
from memento.graph import extract_wikilinks
from memento.search import expand_result_links, multi_hop_search, qmd_get, rrf_fuse
from memento.search_backend import reset_backend

RECALL_MIN_SCORE = DEFAULT_CONFIG["recall_min_score"]
RECALL_HIGH_CONFIDENCE = DEFAULT_CONFIG["recall_high_confidence"]


class TestExtractWikilinks:
    """Extract [[wikilink]] targets from markdown text."""

    def test_basic_wikilink(self):
        text = "See [[redis-cluster-eviction]] for details."
        assert extract_wikilinks(text) == ["redis-cluster-eviction"]

    def test_multiple_wikilinks(self):
        text = "- [[note-a]]\n- [[note-b]]\n- [[note-c]]"
        assert extract_wikilinks(text) == ["note-a", "note-b", "note-c"]

    def test_aliased_wikilink(self):
        text = "Check [[redis-config|the Redis config]] for details."
        assert extract_wikilinks(text) == ["redis-config"]

    def test_deduplicates(self):
        text = "See [[note-a]] and also [[note-a]] again."
        assert extract_wikilinks(text) == ["note-a"]

    def test_no_wikilinks(self):
        text = "Plain text with no links."
        assert extract_wikilinks(text) == []

    def test_empty_string(self):
        assert extract_wikilinks("") == []

    def test_none_input(self):
        assert extract_wikilinks(None) == []

    def test_ignores_code_blocks(self):
        text = "```\n[[not-a-link]]\n```\n\n[[real-link]]"
        result = extract_wikilinks(text)
        assert "real-link" in result
        assert "not-a-link" not in result

    def test_wikilink_with_spaces(self):
        text = "See [[some note with spaces]]."
        assert extract_wikilinks(text) == ["some-note-with-spaces"]

    def test_supersedes_frontmatter(self):
        text = 'supersedes: "[[old-note]]"\n\n- [[related-note]]'
        assert "old-note" in extract_wikilinks(text)
        assert "related-note" in extract_wikilinks(text)


class TestQmdGet:
    """Fetch a single note by path via qmd get."""

    def setup_method(self):
        reset_backend()

    def teardown_method(self):
        reset_backend()

    def test_returns_note_dict(self):
        mock_output = '{"file": "notes/foo.md", "title": "Foo", "content": "Some content with [[bar]]."}'
        with (
            patch("memento.search_backend.shutil.which", return_value="/usr/bin/qmd"),
            patch("memento.search_backend.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = mock_output
            result = qmd_get("notes/foo.md")

        assert result is not None
        assert result["path"] == "notes/foo.md"
        assert "content" in result

    def test_returns_none_on_failure(self):
        with (
            patch("memento.search_backend.shutil.which", return_value="/usr/bin/qmd"),
            patch("memento.search_backend.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = qmd_get("notes/nonexistent.md")

        assert result is None

    def test_returns_none_on_timeout(self):
        import subprocess

        with (
            patch("memento.search_backend.shutil.which", return_value="/usr/bin/qmd"),
            patch("memento.search_backend.subprocess.run", side_effect=subprocess.TimeoutExpired("qmd", 5)),
        ):
            result = qmd_get("notes/slow.md")

        assert result is None

    def test_calls_qmd_with_correct_args(self):
        with (
            patch("memento.search_backend.shutil.which", return_value="/usr/bin/qmd"),
            patch("memento.search_backend.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '{"file": "notes/foo.md", "content": "text"}'
            qmd_get("notes/foo.md", collection="memento")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "qmd"
        assert cmd[1] == "get"
        assert "notes/foo.md" in cmd
        assert "-c" in cmd
        assert "memento" in cmd


class TestMultiHopSearch:
    """Multi-hop should follow wikilinks from initial results."""

    def _make_result(self, path, title, snippet, score=0.5):
        return {"path": path, "title": title, "snippet": snippet, "score": score}

    def test_follows_wikilinks_from_top_results(self):
        initial = [
            self._make_result("notes/a.md", "Note A", "Initial result.", 0.6),
        ]

        note_a_content = "Some content.\n\n## Related\n- [[note-b]]\n- [[note-c]]"
        note_b = {"path": "notes/note-b.md", "title": "Note B", "content": "B content.", "score": 0.0}
        note_c = {"path": "notes/note-c.md", "title": "Note C", "content": "C content.", "score": 0.0}

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": note_a_content}
            if path == "notes/note-b.md":
                return note_b
            if path == "notes/note-c.md":
                return note_c
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test query", initial, config={"multi_hop_max": 2})

        paths = [r["path"] for r in results]
        assert "notes/note-b.md" in paths
        assert "notes/note-c.md" in paths
        assert len(results) == 3

    def test_respects_multi_hop_max(self):
        initial = [
            self._make_result("notes/a.md", "Note A", "Result.", 0.6),
        ]

        note_a_content = "- [[note-b]]\n- [[note-c]]\n- [[note-d]]"

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": note_a_content}
            return {"path": path, "title": Path(path).stem, "content": "content", "score": 0.0}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 2})

        # initial (1) + max 2 linked = 3
        assert len(results) == 3

    def test_skips_already_present_results(self):
        initial = [
            self._make_result("notes/a.md", "Note A", "Result.", 0.6),
            self._make_result("notes/note-b.md", "Note B", "Already here.", 0.5),
        ]

        note_a_content = "- [[note-b]]\n- [[note-c]]"

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": note_a_content}
            if path == "notes/note-c.md":
                return {"path": "notes/note-c.md", "title": "Note C", "content": "C", "score": 0.0}
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 5})

        # note-b already present, so only note-c added
        assert len(results) == 3
        paths = [r["path"] for r in results]
        assert paths.count("notes/note-b.md") == 1

    def test_skips_missing_notes(self):
        initial = [
            self._make_result("notes/a.md", "Note A", "Result.", 0.6),
        ]

        note_a_content = "- [[nonexistent]]\n- [[note-b]]"

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": note_a_content}
            if path == "notes/note-b.md":
                return {"path": "notes/note-b.md", "title": "Note B", "content": "B", "score": 0.0}
            return None  # nonexistent

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 5})

        assert len(results) == 2
        paths = [r["path"] for r in results]
        assert "notes/nonexistent.md" not in paths

    def test_empty_initial_results(self):
        results = multi_hop_search("something", [], config={"multi_hop_max": 1})
        assert results == []

    def test_no_wikilinks_no_change(self):
        initial = [
            self._make_result("notes/a.md", "Note A", "Plain text.", 0.6),
        ]

        def mock_get(path, **kwargs):
            return {"path": path, "content": "Plain text with no links."}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 2})

        assert len(results) == 1

    def test_sorted_by_score(self):
        initial = [
            self._make_result("notes/a.md", "Low", "Content.", 0.3),
        ]

        note_a_content = "- [[note-b]]"

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": note_a_content}
            return {"path": path, "title": "High", "content": "B", "score": 0.0}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("test", initial, config={"multi_hop_max": 1})

        # Original result (0.3) should be first, linked note (0.0) second
        assert results[0]["score"] >= results[-1]["score"]

    def test_only_fetches_top_n_results_for_links(self):
        """Should only extract wikilinks from top results, not all."""
        initial = [self._make_result(f"notes/{i}.md", f"Note {i}", ".", 0.5 - i * 0.1) for i in range(10)]

        get_calls = []

        def mock_get(path, **kwargs):
            get_calls.append(path)
            return {"path": path, "content": "No links here."}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            multi_hop_search("test", initial, config={"multi_hop_max": 2})

        # Should only fetch content for top 3, not all 10
        assert len(get_calls) <= 3


class TestExpandResultLinks:
    """expand_result_links (MEM-159): thin shim for memento_search's
    expand_links opt-in. Unlike multi_hop_search, entries are returned
    separately -- never merged/re-sorted with the direct hits."""

    def _shaped(self, path, title, score=0.5):
        return {"path": path, "title": title, "score": score}

    def test_returns_via_link_marked_entries(self):
        shaped = [self._shaped("notes/a.md", "Note A", 0.6)]

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": "See [[note-b]]."}
            if path == "notes/note-b.md":
                return {"path": "notes/note-b.md", "title": "Note B", "content": "B content."}
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expanded = expand_result_links(shaped, config={"multi_hop_max": 2})

        assert len(expanded) == 1
        assert expanded[0]["path"] == "notes/note-b.md"
        assert expanded[0]["via_link"] == "a"
        assert expanded[0]["score"] == 0.0

    def test_does_not_mutate_or_reorder_direct_hits(self):
        shaped = [
            self._shaped("notes/low.md", "Low", 0.1),
            self._shaped("notes/high.md", "High", 0.9),
        ]
        original = [dict(entry) for entry in shaped]

        def mock_get(path, **kwargs):
            if path == "notes/low.md":
                return {"path": "notes/low.md", "content": "See [[linked]]."}
            if path == "notes/linked.md":
                return {"path": "notes/linked.md", "title": "Linked", "content": "text"}
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expand_result_links(shaped, config={"multi_hop_max": 2})

        # Direct hits list must be untouched: same dicts, same order.
        assert shaped == original

    def test_skips_paths_already_in_direct_hits(self):
        shaped = [
            self._shaped("notes/a.md", "Note A", 0.6),
            self._shaped("notes/note-b.md", "Note B", 0.5),
        ]

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": "See [[note-b]] and [[note-c]]."}
            if path == "notes/note-c.md":
                return {"path": "notes/note-c.md", "title": "Note C", "content": "C"}
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expanded = expand_result_links(shaped, config={"multi_hop_max": 5})

        paths = [e["path"] for e in expanded]
        assert "notes/note-b.md" not in paths  # already a direct hit
        assert "notes/note-c.md" in paths

    def test_respects_max_expanded_from_config(self):
        shaped = [self._shaped("notes/a.md", "Note A", 0.6)]

        def mock_get(path, **kwargs):
            if path == "notes/a.md":
                return {"path": "notes/a.md", "content": "See [[b]], [[c]], [[d]]."}
            return {"path": path, "title": Path(path).stem, "content": "text"}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expanded = expand_result_links(shaped, config={"multi_hop_max": 1})

        assert len(expanded) == 1

    def test_only_follows_top_n_direct_hits(self):
        shaped = [self._shaped(f"notes/{i}.md", f"Note {i}", 0.9 - i * 0.1) for i in range(10)]

        get_calls = []

        def mock_get(path, **kwargs):
            get_calls.append(path)
            return {"path": path, "content": "No links here."}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expand_result_links(shaped, config={"multi_hop_max": 5}, top_n=3)

        assert len(get_calls) <= 3

    def test_empty_results_returns_empty(self):
        assert expand_result_links([], config={"multi_hop_max": 2}) == []

    def test_no_wikilinks_returns_empty(self):
        shaped = [self._shaped("notes/a.md", "Note A", 0.6)]

        def mock_get(path, **kwargs):
            return {"path": path, "content": "Plain text with no links."}

        with patch("memento.search.qmd_get", side_effect=mock_get):
            expanded = expand_result_links(shaped, config={"multi_hop_max": 2})

        assert expanded == []


class TestMultiHopGateWithFixedFusionMEM143:
    """MEM-143 gates the multi_hop_enabled default flip (MEM-159 part 3) on
    the RRF fusion fix: multi-hop follows wikilinks starting from the top of
    whatever result list it's handed, so if RRF fusion could still
    manufacture an inflated score for a weak match, multi-hop would
    confidently expand from a garbage seed. These tests run the actual
    (fixed) rrf_fuse() output through multi_hop_search() to confirm the
    bound holds end to end, not just inside rrf_fuse() in isolation."""

    def test_weak_only_multi_hop_expansion_cannot_clear_recall_min_score(self):
        """A weak match that is the sole candidate in two thin RRF input
        lists must stay below recall_min_score even after being used as a
        multi-hop seed and expanded with another equally weak linked note."""
        weak_seed = {"path": "notes/weak-seed.md", "title": "Weak seed", "score": 0.05, "snippet": ""}
        fused = rrf_fuse([[dict(weak_seed)], [dict(weak_seed)]], k=60)
        assert fused[0]["score"] < RECALL_MIN_SCORE

        def mock_get(path, **kwargs):
            if path == "notes/weak-seed.md":
                return {"path": path, "content": "See [[weak-linked]]."}
            if path == "notes/weak-linked.md":
                return {"path": "notes/weak-linked.md", "title": "Weak linked", "content": "text", "score": 0.0}
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("irrelevant query", fused, config={"multi_hop_max": 2})

        by_path = {r["path"]: r for r in results}
        assert by_path["notes/weak-linked.md"] is not None
        assert by_path["notes/weak-seed.md"]["score"] < RECALL_MIN_SCORE
        assert by_path["notes/weak-linked.md"]["score"] < RECALL_MIN_SCORE

    def test_genuinely_strong_multi_hop_result_still_surfaces(self):
        """A strong match that clears recall_high_confidence through RRF
        fusion must still surface as the top result after multi-hop
        expansion adds its linked note."""
        strong_seed = {"path": "notes/strong-seed.md", "title": "Strong seed", "score": 0.9, "snippet": ""}
        fused = rrf_fuse([[dict(strong_seed)], [dict(strong_seed)]], k=60)
        assert fused[0]["score"] >= RECALL_HIGH_CONFIDENCE

        def mock_get(path, **kwargs):
            if path == "notes/strong-seed.md":
                return {"path": path, "content": "See [[strong-linked]]."}
            if path == "notes/strong-linked.md":
                return {
                    "path": "notes/strong-linked.md",
                    "title": "Strong linked",
                    "content": "text",
                    "score": 0.0,
                }
            return None

        with patch("memento.search.qmd_get", side_effect=mock_get):
            results = multi_hop_search("irrelevant query", fused, config={"multi_hop_max": 2})

        assert results[0]["path"] == "notes/strong-seed.md"
        assert results[0]["score"] >= RECALL_MIN_SCORE
        paths = [r["path"] for r in results]
        assert "notes/strong-linked.md" in paths
