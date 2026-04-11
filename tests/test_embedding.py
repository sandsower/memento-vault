"""Tests for the embedding provider abstraction and all providers."""

import io
import json
import math
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Mock provider that satisfies the ABC
# ---------------------------------------------------------------------------


class _MockProvider:
    """Concrete implementation for protocol/interface testing."""

    def __init__(self, dims: int = 128):
        self._dims = dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        rng = np.random.RandomState(42)
        return [rng.randn(self._dims).tolist() for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def dimensions(self) -> int:
        return self._dims

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# ABC / interface tests
# ---------------------------------------------------------------------------


class TestEmbeddingProviderABC:
    """Verify the abstract base class enforces the contract."""

    def test_cannot_instantiate_abc(self):
        from memento.embedding import EmbeddingProvider

        with pytest.raises(TypeError):
            EmbeddingProvider()

    def test_mock_provider_satisfies_interface(self):
        from memento.embedding import EmbeddingProvider

        # _MockProvider doesn't inherit from EmbeddingProvider, but a proper
        # subclass should work — verify via the ABC's required methods.
        methods = {"embed", "embed_query", "dimensions", "is_available"}
        abc_methods = {
            name
            for name, val in vars(EmbeddingProvider).items()
            if getattr(val, "__isabstractmethod__", False)
        }
        assert methods == abc_methods

    def test_concrete_subclass_works(self):
        from memento.embedding import EmbeddingProvider

        class Dummy(EmbeddingProvider):
            def embed(self, texts):
                return [[0.0] * 4 for _ in texts]

            def embed_query(self, text):
                return [0.0] * 4

            def dimensions(self):
                return 4

            def is_available(self):
                return True

        d = Dummy()
        assert d.dimensions() == 4
        assert d.is_available()
        vecs = d.embed(["hello", "world"])
        assert len(vecs) == 2
        assert len(vecs[0]) == 4


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestGetEmbeddingProvider:
    """Test the factory function."""

    def test_local_returns_nomic_provider(self):
        from memento.embedding import NomicLocalProvider, get_embedding_provider

        config = {"embedding_provider": "local"}
        provider = get_embedding_provider(config)
        assert isinstance(provider, NomicLocalProvider)

    def test_default_is_local(self):
        from memento.embedding import NomicLocalProvider, get_embedding_provider

        provider = get_embedding_provider({})
        assert isinstance(provider, NomicLocalProvider)

    def test_unknown_provider_raises(self):
        from memento.embedding import get_embedding_provider

        with pytest.raises(ValueError, match="Unknown embedding provider"):
            get_embedding_provider({"embedding_provider": "nonexistent"})

    def test_api_providers_need_api_key(self):
        """API providers require an API key in config or env."""
        from memento.embedding import get_embedding_provider

        for name in ("voyage", "openai", "google"):
            with patch.dict(os.environ, {}, clear=True):
                provider = get_embedding_provider({"embedding_provider": name})
                assert not provider.is_available()

    def test_factory_passes_config_to_provider(self):
        from memento.embedding import get_embedding_provider

        config = {
            "embedding_provider": "local",
            "embedding_model": "nomic-embed-text-v1.5",
            "embedding_dimensions": 256,
        }
        provider = get_embedding_provider(config)
        assert provider.dimensions() == 256


# ---------------------------------------------------------------------------
# NomicLocalProvider unit tests (no model required)
# ---------------------------------------------------------------------------


class TestNomicLocalProviderUnit:
    """Tests that don't need onnxruntime or the actual model."""

    def test_is_available_false_when_onnxruntime_missing(self):
        from memento.embedding import NomicLocalProvider

        with patch.dict("sys.modules", {"onnxruntime": None}):
            p = NomicLocalProvider()
            # Reimport check — the provider caches the check, so test a fresh one
            assert not p._check_onnxruntime_importable()

    def test_is_available_true_when_onnxruntime_present(self):
        from memento.embedding import NomicLocalProvider

        mock_ort = MagicMock()
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            p = NomicLocalProvider()
            assert p._check_onnxruntime_importable()

    def test_default_dimensions_512(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        assert p.dimensions() == 512

    def test_custom_dimensions(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider(dimensions=256)
        assert p.dimensions() == 256

    def test_embed_raises_when_onnxruntime_missing(self):
        from memento.embedding import NomicLocalProvider

        with patch.dict("sys.modules", {"onnxruntime": None}):
            p = NomicLocalProvider()
            with pytest.raises(RuntimeError, match="onnxruntime"):
                p.embed(["hello"])

    def test_embed_query_raises_when_onnxruntime_missing(self):
        from memento.embedding import NomicLocalProvider

        with patch.dict("sys.modules", {"onnxruntime": None}):
            p = NomicLocalProvider()
            with pytest.raises(RuntimeError, match="onnxruntime"):
                p.embed_query("hello")


# ---------------------------------------------------------------------------
# Matryoshka truncation + L2 normalization (tested in isolation)
# ---------------------------------------------------------------------------


class TestTruncateAndNormalize:
    """Test the dimension truncation and L2 normalization logic."""

    def test_truncate_reduces_dimensions(self):
        from memento.embedding import _truncate_and_normalize

        vecs = np.random.randn(3, 768).astype(np.float32)
        result = _truncate_and_normalize(vecs, 512)
        assert result.shape == (3, 512)

    def test_truncate_to_same_dim_is_noop_on_shape(self):
        from memento.embedding import _truncate_and_normalize

        vecs = np.random.randn(2, 512).astype(np.float32)
        result = _truncate_and_normalize(vecs, 512)
        assert result.shape == (2, 512)

    def test_output_is_l2_normalized(self):
        from memento.embedding import _truncate_and_normalize

        vecs = np.random.randn(5, 768).astype(np.float32)
        result = _truncate_and_normalize(vecs, 512)
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-5)

    def test_zero_vector_stays_zero(self):
        from memento.embedding import _truncate_and_normalize

        vecs = np.zeros((1, 768), dtype=np.float32)
        result = _truncate_and_normalize(vecs, 512)
        assert result.shape == (1, 512)
        # Zero vector can't be normalized — should remain zero
        assert np.allclose(result, 0.0)

    def test_single_vector(self):
        from memento.embedding import _truncate_and_normalize

        vec = np.array([[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]], dtype=np.float32)
        result = _truncate_and_normalize(vec, 3)
        assert result.shape == (1, 3)
        norm = np.linalg.norm(result[0])
        assert abs(norm - 1.0) < 1e-5


# ---------------------------------------------------------------------------
# Query/document prefix tests
# ---------------------------------------------------------------------------


class TestNomicPrefixing:
    """Nomic model requires specific prefixes for queries vs documents."""

    def test_query_prefix(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        assert p._format_query("hello world") == "search_query: hello world"

    def test_document_prefix(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        assert p._format_document("hello world") == "search_document: hello world"

    def test_query_prefix_not_doubled(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        # If text already has the prefix, don't double it
        result = p._format_query("search_query: hello world")
        assert result == "search_query: hello world"

    def test_document_prefix_not_doubled(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        result = p._format_document("search_document: hello world")
        assert result == "search_document: hello world"


# ---------------------------------------------------------------------------
# Model cache path
# ---------------------------------------------------------------------------


class TestModelCachePath:
    """Verify model caching directory logic."""

    def test_default_cache_dir(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        cache = p._model_cache_dir()
        assert "memento-vault" in str(cache)
        assert "models" in str(cache)

    def test_custom_cache_dir(self, tmp_path):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider(cache_dir=tmp_path / "custom-models")
        assert p._model_cache_dir() == tmp_path / "custom-models"


# ---------------------------------------------------------------------------
# Integration test: actual model (skip if onnxruntime not installed)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestNomicLocalProviderIntegration:
    """Tests that require onnxruntime and the actual nomic model.

    Mark with @pytest.mark.slow — skipped unless running with -m slow.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_onnxruntime(self):
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            pytest.skip("onnxruntime not installed")

    def test_is_available(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        assert p.is_available()

    def test_embed_single_text(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vecs = p.embed(["hello world"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 512

    def test_embed_multiple_texts(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vecs = p.embed(["hello world", "foo bar", "test text"])
        assert len(vecs) == 3
        for v in vecs:
            assert len(v) == 512

    def test_embed_query_returns_single_vector(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vec = p.embed_query("how to cache Redis keys")
        assert len(vec) == 512

    def test_embeddings_are_normalized(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vecs = p.embed(["test normalization"])
        norm = math.sqrt(sum(x * x for x in vecs[0]))
        assert abs(norm - 1.0) < 1e-4

    def test_similar_texts_have_high_cosine(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vecs = p.embed(["Redis cache TTL", "Redis key expiration time"])
        a, b = np.array(vecs[0]), np.array(vecs[1])
        cosine = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
        assert cosine > 0.7

    def test_different_texts_have_lower_cosine(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        vecs = p.embed(["Redis cache TTL", "quantum physics string theory"])
        a, b = np.array(vecs[0]), np.array(vecs[1])
        cosine = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
        assert cosine < 0.7

    def test_empty_list_returns_empty(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider()
        assert p.embed([]) == []

    def test_custom_dimensions(self):
        from memento.embedding import NomicLocalProvider

        p = NomicLocalProvider(dimensions=256)
        vecs = p.embed(["test"])
        assert len(vecs[0]) == 256


# ---------------------------------------------------------------------------
# Helper: fake HTTP response for mocking urllib.request.urlopen
# ---------------------------------------------------------------------------


def _fake_urlopen(response_body: dict, status: int = 200):
    """Return a context-manager-compatible mock for urllib.request.urlopen."""
    body_bytes = json.dumps(response_body).encode()
    resp = MagicMock()
    resp.read.return_value = body_bytes
    resp.status = status
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# VoyageProvider tests
# ---------------------------------------------------------------------------


class TestVoyageProvider:
    """Tests for the Voyage AI embedding provider."""

    def test_is_available_true_with_key(self):
        from memento.embedding import VoyageProvider

        p = VoyageProvider(api_key="voy-test-key")
        assert p.is_available()

    def test_is_available_false_without_key(self):
        from memento.embedding import VoyageProvider

        with patch.dict(os.environ, {}, clear=True):
            p = VoyageProvider(api_key=None)
            assert not p.is_available()

    def test_default_model(self):
        from memento.embedding import VoyageProvider

        p = VoyageProvider(api_key="k")
        assert p._model == "voyage-3-lite"

    def test_custom_model(self):
        from memento.embedding import VoyageProvider

        p = VoyageProvider(api_key="k", model="voyage-3")
        assert p._model == "voyage-3"

    def test_dimensions(self):
        from memento.embedding import VoyageProvider

        p = VoyageProvider(api_key="k", dimensions=256)
        assert p.dimensions() == 256

    def test_embed_sends_correct_request(self):
        from memento.embedding import VoyageProvider

        fake_response = {
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ]
        }
        mock_resp = _fake_urlopen(fake_response)

        p = VoyageProvider(api_key="voy-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.embed(["hello", "world"])

            # Verify request was made
            assert mock_open.called
            req = mock_open.call_args[0][0]
            assert req.full_url == "https://api.voyageai.com/v1/embeddings"
            assert req.get_header("Authorization") == "Bearer voy-key"
            assert req.get_header("Content-type") == "application/json"

            body = json.loads(req.data)
            assert body["input"] == ["hello", "world"]
            assert body["model"] == "voyage-3-lite"

        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3]
        assert result[1] == [0.4, 0.5, 0.6]

    def test_embed_query(self):
        from memento.embedding import VoyageProvider

        fake_response = {
            "data": [{"embedding": [0.7, 0.8, 0.9]}]
        }
        mock_resp = _fake_urlopen(fake_response)

        p = VoyageProvider(api_key="voy-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.embed_query("test query")

        assert result == [0.7, 0.8, 0.9]

    def test_embed_empty_list(self):
        from memento.embedding import VoyageProvider

        p = VoyageProvider(api_key="voy-key")
        assert p.embed([]) == []

    def test_embed_api_error_raises(self):
        from memento.embedding import VoyageProvider
        import urllib.error

        p = VoyageProvider(api_key="voy-key")
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            url="https://api.voyageai.com/v1/embeddings",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error": "invalid key"}'),
        )):
            with pytest.raises(RuntimeError, match="Voyage API error"):
                p.embed(["test"])

    def test_api_key_from_env(self):
        from memento.embedding import VoyageProvider

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "env-voy-key"}):
            p = VoyageProvider()
            assert p._api_key == "env-voy-key"
            assert p.is_available()

    def test_config_key_overrides_env(self):
        from memento.embedding import VoyageProvider

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "env-key"}):
            p = VoyageProvider(api_key="config-key")
            assert p._api_key == "config-key"


# ---------------------------------------------------------------------------
# OpenAIProvider tests
# ---------------------------------------------------------------------------


class TestOpenAIProvider:
    """Tests for the OpenAI embedding provider."""

    def test_is_available_true_with_key(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="sk-test-key")
        assert p.is_available()

    def test_is_available_false_without_key(self):
        from memento.embedding import OpenAIProvider

        with patch.dict(os.environ, {}, clear=True):
            p = OpenAIProvider(api_key=None)
            assert not p.is_available()

    def test_default_model(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="k")
        assert p._model == "text-embedding-3-small"

    def test_custom_model(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="k", model="text-embedding-3-large")
        assert p._model == "text-embedding-3-large"

    def test_dimensions(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="k", dimensions=256)
        assert p.dimensions() == 256

    def test_custom_api_base(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="k", api_base="https://my-proxy.example.com/v1/embeddings")
        assert p._api_base == "https://my-proxy.example.com/v1/embeddings"

    def test_embed_sends_correct_request(self):
        from memento.embedding import OpenAIProvider

        fake_response = {
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]},
            ]
        }
        mock_resp = _fake_urlopen(fake_response)

        p = OpenAIProvider(api_key="sk-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.embed(["hello", "world"])

            req = mock_open.call_args[0][0]
            assert req.full_url == "https://api.openai.com/v1/embeddings"
            assert req.get_header("Authorization") == "Bearer sk-key"
            assert req.get_header("Content-type") == "application/json"

            body = json.loads(req.data)
            assert body["input"] == ["hello", "world"]
            assert body["model"] == "text-embedding-3-small"
            assert body["dimensions"] == 3

        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3]

    def test_embed_query(self):
        from memento.embedding import OpenAIProvider

        fake_response = {
            "data": [{"embedding": [0.7, 0.8, 0.9]}]
        }
        mock_resp = _fake_urlopen(fake_response)

        p = OpenAIProvider(api_key="sk-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.embed_query("test query")

        assert result == [0.7, 0.8, 0.9]

    def test_embed_empty_list(self):
        from memento.embedding import OpenAIProvider

        p = OpenAIProvider(api_key="sk-key")
        assert p.embed([]) == []

    def test_embed_api_error_raises(self):
        from memento.embedding import OpenAIProvider
        import urllib.error

        p = OpenAIProvider(api_key="sk-key")
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            url="https://api.openai.com/v1/embeddings",
            code=429,
            msg="Rate limited",
            hdrs=None,
            fp=io.BytesIO(b'{"error": {"message": "rate limited"}}'),
        )):
            with pytest.raises(RuntimeError, match="OpenAI API error"):
                p.embed(["test"])

    def test_api_key_from_env(self):
        from memento.embedding import OpenAIProvider

        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-oai-key"}):
            p = OpenAIProvider()
            assert p._api_key == "env-oai-key"
            assert p.is_available()

    def test_uses_custom_api_base_in_request(self):
        from memento.embedding import OpenAIProvider

        fake_response = {"data": [{"embedding": [0.1]}]}
        mock_resp = _fake_urlopen(fake_response)

        p = OpenAIProvider(api_key="sk-key", api_base="https://proxy.test/v1/embeddings", dimensions=1)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            p.embed(["test"])
            req = mock_open.call_args[0][0]
            assert req.full_url == "https://proxy.test/v1/embeddings"


# ---------------------------------------------------------------------------
# GoogleProvider tests
# ---------------------------------------------------------------------------


class TestGoogleProvider:
    """Tests for the Google Generative AI embedding provider."""

    def test_is_available_true_with_key(self):
        from memento.embedding import GoogleProvider

        p = GoogleProvider(api_key="goog-test-key")
        assert p.is_available()

    def test_is_available_false_without_key(self):
        from memento.embedding import GoogleProvider

        with patch.dict(os.environ, {}, clear=True):
            p = GoogleProvider(api_key=None)
            assert not p.is_available()

    def test_default_model(self):
        from memento.embedding import GoogleProvider

        p = GoogleProvider(api_key="k")
        assert p._model == "text-embedding-004"

    def test_custom_model(self):
        from memento.embedding import GoogleProvider

        p = GoogleProvider(api_key="k", model="embedding-001")
        assert p._model == "embedding-001"

    def test_dimensions(self):
        from memento.embedding import GoogleProvider

        p = GoogleProvider(api_key="k", dimensions=256)
        assert p.dimensions() == 256

    def test_embed_sends_correct_request(self):
        from memento.embedding import GoogleProvider

        fake_response = {
            "embedding": {"values": [0.1, 0.2, 0.3]}
        }
        mock_resp = _fake_urlopen(fake_response)

        p = GoogleProvider(api_key="goog-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.embed(["hello"])

            req = mock_open.call_args[0][0]
            assert "text-embedding-004" in req.full_url
            assert "key=goog-key" in req.full_url
            assert req.get_header("Content-type") == "application/json"

            body = json.loads(req.data)
            assert body["content"]["parts"][0]["text"] == "hello"

        assert len(result) == 1
        assert result[0] == [0.1, 0.2, 0.3]

    def test_embed_multiple_texts_makes_multiple_requests(self):
        from memento.embedding import GoogleProvider

        responses = [
            _fake_urlopen({"embedding": {"values": [0.1, 0.2]}}),
            _fake_urlopen({"embedding": {"values": [0.3, 0.4]}}),
        ]

        p = GoogleProvider(api_key="goog-key", dimensions=2)
        with patch("urllib.request.urlopen", side_effect=responses):
            result = p.embed(["hello", "world"])

        assert len(result) == 2
        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.3, 0.4]

    def test_embed_query(self):
        from memento.embedding import GoogleProvider

        fake_response = {
            "embedding": {"values": [0.7, 0.8, 0.9]}
        }
        mock_resp = _fake_urlopen(fake_response)

        p = GoogleProvider(api_key="goog-key", dimensions=3)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = p.embed_query("test query")

        assert result == [0.7, 0.8, 0.9]

    def test_embed_empty_list(self):
        from memento.embedding import GoogleProvider

        p = GoogleProvider(api_key="goog-key")
        assert p.embed([]) == []

    def test_embed_api_error_raises(self):
        from memento.embedding import GoogleProvider
        import urllib.error

        p = GoogleProvider(api_key="goog-key")
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com/...",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b'{"error": {"message": "forbidden"}}'),
        )):
            with pytest.raises(RuntimeError, match="Google API error"):
                p.embed(["test"])

    def test_api_key_from_env(self):
        from memento.embedding import GoogleProvider

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-goog-key"}):
            p = GoogleProvider()
            assert p._api_key == "env-goog-key"
            assert p.is_available()


# ---------------------------------------------------------------------------
# Factory tests for API providers
# ---------------------------------------------------------------------------


class TestFactoryAPIProviders:
    """Test get_embedding_provider returns correct API providers."""

    def test_voyage_provider(self):
        from memento.embedding import VoyageProvider, get_embedding_provider

        config = {
            "embedding_provider": "voyage",
            "embedding_api_key": "voy-key",
            "embedding_dimensions": 512,
        }
        provider = get_embedding_provider(config)
        assert isinstance(provider, VoyageProvider)
        assert provider.dimensions() == 512

    def test_openai_provider(self):
        from memento.embedding import OpenAIProvider, get_embedding_provider

        config = {
            "embedding_provider": "openai",
            "embedding_api_key": "sk-key",
            "embedding_dimensions": 256,
        }
        provider = get_embedding_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.dimensions() == 256

    def test_google_provider(self):
        from memento.embedding import GoogleProvider, get_embedding_provider

        config = {
            "embedding_provider": "google",
            "embedding_api_key": "goog-key",
            "embedding_dimensions": 512,
        }
        provider = get_embedding_provider(config)
        assert isinstance(provider, GoogleProvider)
        assert provider.dimensions() == 512

    def test_voyage_passes_model(self):
        from memento.embedding import get_embedding_provider

        config = {
            "embedding_provider": "voyage",
            "embedding_api_key": "k",
            "embedding_model": "voyage-3",
        }
        provider = get_embedding_provider(config)
        assert provider._model == "voyage-3"

    def test_openai_passes_api_base(self):
        from memento.embedding import get_embedding_provider

        config = {
            "embedding_provider": "openai",
            "embedding_api_key": "k",
            "embedding_api_base": "https://proxy.test/v1/embeddings",
        }
        provider = get_embedding_provider(config)
        assert provider._api_base == "https://proxy.test/v1/embeddings"

    def test_unknown_still_raises(self):
        from memento.embedding import get_embedding_provider

        with pytest.raises(ValueError, match="Unknown embedding provider"):
            get_embedding_provider({"embedding_provider": "nonexistent"})
