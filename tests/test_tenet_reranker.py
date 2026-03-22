"""Tests for Tier 2 cross-encoder reranker."""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))


class TestCheckDeps:
    def test_returns_bool(self):
        """_check_deps always returns a bool."""
        import tenet_reranker

        tenet_reranker._DEPS_AVAILABLE = None  # reset cache
        result = tenet_reranker._check_deps()
        assert isinstance(result, bool)

    def test_caches_result(self):
        """Subsequent calls return cached value without re-importing."""
        import tenet_reranker

        tenet_reranker._DEPS_AVAILABLE = None
        first = tenet_reranker._check_deps()
        # Set cache to opposite value — next call should return cached
        tenet_reranker._DEPS_AVAILABLE = not first
        assert tenet_reranker._check_deps() is (not first)
        tenet_reranker._DEPS_AVAILABLE = None  # reset

    def test_returns_false_when_onnxruntime_missing(self):
        """Should return False when onnxruntime import fails."""
        import tenet_reranker

        tenet_reranker._DEPS_AVAILABLE = None

        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def mock_import(name, *args, **kwargs):
            if name == "onnxruntime":
                raise ImportError("no onnxruntime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = tenet_reranker._check_deps()

        assert result is False
        tenet_reranker._DEPS_AVAILABLE = None  # reset


class TestScorePairs:
    def test_scores_batch(self):
        """Mock session returns correct shape, scores are extracted."""
        import tenet_reranker

        mock_session = MagicMock()
        mock_session.run.return_value = [np.array([[0.9], [0.1], [0.5]])]

        mock_input_ids = MagicMock()
        mock_input_ids.name = "input_ids"
        mock_attn = MagicMock()
        mock_attn.name = "attention_mask"
        mock_type = MagicMock()
        mock_type.name = "token_type_ids"
        mock_session.get_inputs.return_value = [mock_input_ids, mock_attn, mock_type]

        mock_tokenizer = MagicMock()
        mock_encoding = MagicMock()
        mock_encoding.ids = [101, 2023, 102, 3456, 102]
        mock_encoding.attention_mask = [1, 1, 1, 1, 1]
        mock_encoding.type_ids = [0, 0, 0, 1, 1]
        mock_tokenizer.encode.return_value = mock_encoding

        scores = tenet_reranker._score_pairs(
            mock_session, mock_tokenizer, "test query", ["doc1", "doc2", "doc3"]
        )
        assert len(scores) == 3
        assert scores[0] > scores[1]  # 0.9 > 0.1

    def test_applies_sigmoid(self):
        """Scores should be sigmoid-transformed to 0-1 range."""
        import tenet_reranker

        mock_session = MagicMock()
        # Large positive logit -> sigmoid near 1, large negative -> near 0
        mock_session.run.return_value = [np.array([[10.0], [-10.0]])]

        mock_input = MagicMock()
        mock_input.name = "input_ids"
        mock_attn = MagicMock()
        mock_attn.name = "attention_mask"
        mock_type = MagicMock()
        mock_type.name = "token_type_ids"
        mock_session.get_inputs.return_value = [mock_input, mock_attn, mock_type]

        mock_tokenizer = MagicMock()
        mock_encoding = MagicMock()
        mock_encoding.ids = [101, 102]
        mock_encoding.attention_mask = [1, 1]
        mock_encoding.type_ids = [0, 0]
        mock_tokenizer.encode.return_value = mock_encoding

        scores = tenet_reranker._score_pairs(
            mock_session, mock_tokenizer, "query", ["pos", "neg"]
        )
        assert scores[0] > 0.99
        assert scores[1] < 0.01

    def test_empty_texts(self):
        """Empty text list returns empty scores."""
        import tenet_reranker

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()

        scores = tenet_reranker._score_pairs(
            mock_session, mock_tokenizer, "query", []
        )
        assert scores == []

    def test_pads_to_max_length(self):
        """Encodings of different lengths should be padded to the longest."""
        import tenet_reranker

        mock_session = MagicMock()
        mock_session.run.return_value = [np.array([[0.5], [0.5]])]

        mock_input = MagicMock()
        mock_input.name = "input_ids"
        mock_attn = MagicMock()
        mock_attn.name = "attention_mask"
        mock_type = MagicMock()
        mock_type.name = "token_type_ids"
        mock_session.get_inputs.return_value = [mock_input, mock_attn, mock_type]

        # Two encodings of different lengths
        enc_short = MagicMock()
        enc_short.ids = [101, 102]
        enc_short.attention_mask = [1, 1]
        enc_short.type_ids = [0, 0]

        enc_long = MagicMock()
        enc_long.ids = [101, 200, 300, 102]
        enc_long.attention_mask = [1, 1, 1, 1]
        enc_long.type_ids = [0, 0, 1, 1]

        mock_tokenizer = MagicMock()
        mock_tokenizer.encode.side_effect = [enc_short, enc_long]

        tenet_reranker._score_pairs(
            mock_session, mock_tokenizer, "q", ["short", "longer text"]
        )

        # Verify the input arrays passed to session.run are padded to length 4
        call_args = mock_session.run.call_args
        feed_dict = call_args[1] if call_args[1] else call_args[0][1]
        input_ids = feed_dict["input_ids"]
        assert input_ids.shape == (2, 4)


class TestRerank:
    def test_reorders_by_score(self):
        """Results should be reordered by cross-encoder score."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "first"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "second"},
            {"path": "notes/c.md", "title": "C", "score": 0.5, "snippet": "third"},
        ]

        mock_scores = [0.2, 0.8, 0.5]  # B should rank first after rerank

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', return_value=mock_scores):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("test query", results, config)

        assert reranked[0]["path"] == "notes/b.md"
        assert reranked[1]["path"] == "notes/c.md"
        assert reranked[2]["path"] == "notes/a.md"

    def test_disabled_returns_unchanged(self):
        """When reranker_enabled=False, return results as-is."""
        import tenet_reranker

        results = [{"path": "notes/a.md", "title": "A", "score": 0.9}]
        config = {"reranker_enabled": False}
        reranked = tenet_reranker.rerank("query", results, config)
        assert reranked == results

    def test_missing_deps_returns_unchanged(self):
        """When onnxruntime not available, return results as-is."""
        import tenet_reranker

        results = [{"path": "notes/a.md", "title": "A", "score": 0.9}]

        with patch.object(tenet_reranker, '_check_deps', return_value=False):
            reranked = tenet_reranker.rerank("query", results, config={"reranker_enabled": True})

        assert reranked == results

    def test_single_result_returns_unchanged(self):
        """Single result needs no reranking."""
        import tenet_reranker

        results = [{"path": "notes/a.md", "title": "A", "score": 0.9}]
        reranked = tenet_reranker.rerank("query", results, config={"reranker_enabled": True})
        assert reranked == results

    def test_empty_results_returns_unchanged(self):
        """Empty list returns empty list."""
        import tenet_reranker

        reranked = tenet_reranker.rerank("query", [], config={"reranker_enabled": True})
        assert reranked == []

    def test_respects_top_k(self):
        """Only top_k results should be reranked, rest stay in place."""
        import tenet_reranker

        results = [
            {"path": f"notes/{i}.md", "title": str(i), "score": 1.0 - i * 0.1, "snippet": ""}
            for i in range(5)
        ]

        # Only rerank top 2, scores reversed
        mock_scores = [0.1, 0.9]

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', return_value=mock_scores):

            config = {"reranker_enabled": True, "reranker_top_k": 2, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        # First two should be reranked (swapped), rest unchanged
        assert reranked[0]["path"] == "notes/1.md"  # was second, now first
        assert reranked[1]["path"] == "notes/0.md"  # was first, now second
        assert len(reranked) == 5  # all results still present

    def test_filters_below_min_score(self):
        """Results below min_score should be filtered out of the reranked set."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "good"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "bad"},
        ]

        mock_scores = [0.8, 0.001]  # B below min_score

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', return_value=mock_scores):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        assert len(reranked) == 1
        assert reranked[0]["path"] == "notes/a.md"

    def test_cleans_internal_metadata(self):
        """_rerank_score should not appear in returned results."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "x"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "y"},
        ]

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', return_value=[0.5, 0.8]):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        for r in reranked:
            assert "_rerank_score" not in r

    def test_replaces_score_with_cross_encoder_score(self):
        """The score field should be updated to the cross-encoder score."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "x"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "y"},
        ]

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', return_value=[0.3, 0.6]):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        # B ranked first with score 0.6
        assert reranked[0]["path"] == "notes/b.md"
        assert reranked[0]["score"] == 0.6
        assert reranked[1]["score"] == 0.3

    def test_load_session_failure_returns_unchanged(self):
        """If _load_session raises, return results unchanged."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "x"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "y"},
        ]

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', side_effect=RuntimeError("no model")):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        assert reranked == results

    def test_score_pairs_failure_returns_unchanged(self):
        """If _score_pairs raises, return results unchanged."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "A", "score": 0.9, "snippet": "x"},
            {"path": "notes/b.md", "title": "B", "score": 0.7, "snippet": "y"},
        ]

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', side_effect=RuntimeError("onnx failed")):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            reranked = tenet_reranker.rerank("query", results, config)

        assert reranked == results

    def test_builds_text_from_title_and_snippet(self):
        """Texts passed to _score_pairs should combine title and snippet."""
        import tenet_reranker

        results = [
            {"path": "notes/a.md", "title": "My Title", "score": 0.9, "snippet": "some text"},
            {"path": "notes/b.md", "title": "", "score": 0.7, "snippet": "just snippet"},
        ]

        captured_texts = []

        def fake_score(session, tokenizer, query, texts):
            captured_texts.extend(texts)
            return [0.5] * len(texts)

        with patch.object(tenet_reranker, '_check_deps', return_value=True), \
             patch.object(tenet_reranker, '_load_session', return_value=(MagicMock(), MagicMock())), \
             patch.object(tenet_reranker, '_score_pairs', side_effect=fake_score):

            config = {"reranker_enabled": True, "reranker_top_k": 10, "reranker_min_score": 0.01}
            tenet_reranker.rerank("query", results, config)

        assert captured_texts[0] == "My Title. some text"
        assert captured_texts[1] == "just snippet"


class TestEnsureModel:
    def test_returns_cache_dir_when_files_exist(self, tmp_path):
        """When model files already exist, return immediately without download."""
        import tenet_reranker

        cache_dir = str(tmp_path / "models")
        os.makedirs(cache_dir)
        (tmp_path / "models" / "model.onnx").write_text("fake model")
        (tmp_path / "models" / "tokenizer.json").write_text("{}")

        result = tenet_reranker._ensure_model(cache_dir=cache_dir)
        assert result == cache_dir

    def test_downloads_when_missing(self, tmp_path):
        """When files don't exist, should call hf_hub_download."""
        import tenet_reranker

        cache_dir = str(tmp_path / "models")

        mock_download = MagicMock(return_value="fake_path")
        fake_hf = MagicMock()
        fake_hf.hf_hub_download = mock_download

        with patch.dict("sys.modules", {"huggingface_hub": fake_hf}):
            try:
                tenet_reranker._ensure_model(cache_dir=cache_dir)
            except Exception:
                pass
            assert mock_download.called


class TestLoadSession:
    def test_caches_session(self):
        """Second call returns the cached session without reloading."""
        import tenet_reranker

        old_session = tenet_reranker._SESSION
        try:
            fake_sess = MagicMock()
            fake_tok = MagicMock()
            tenet_reranker._SESSION = (fake_sess, fake_tok)

            result = tenet_reranker._load_session()
            assert result == (fake_sess, fake_tok)
        finally:
            tenet_reranker._SESSION = old_session

    def test_returns_tuple(self):
        """_load_session returns (session, tokenizer) tuple."""
        import tenet_reranker

        old_session = tenet_reranker._SESSION
        try:
            tenet_reranker._SESSION = (MagicMock(), MagicMock())
            sess, tok = tenet_reranker._load_session()
            assert sess is not None
            assert tok is not None
        finally:
            tenet_reranker._SESSION = old_session
