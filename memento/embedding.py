"""Embedding provider abstraction — vector embeddings from text.

Provides an EmbeddingProvider protocol and concrete providers:
- NomicLocalProvider: runs nomic-embed-text-v1.5 locally via ONNX Runtime
- VoyageProvider: Voyage AI API
- OpenAIProvider: OpenAI embeddings API (or compatible proxy)
- GoogleProvider: Google Generative AI embeddings API
"""

import importlib
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple document texts. Returns list of float vectors."""
        ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. May differ from document embedding for
        asymmetric models (e.g. nomic prefix convention)."""
        ...

    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensionality of the output embeddings."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is ready to produce embeddings."""
        ...


# ---------------------------------------------------------------------------
# Matryoshka truncation + L2 normalization
# ---------------------------------------------------------------------------


def _truncate_and_normalize(
    vectors: np.ndarray, target_dim: int
) -> np.ndarray:
    """Truncate to *target_dim* columns and L2-normalize each row.

    Handles the Matryoshka property of nomic-embed-text-v1.5: the model
    emits 768-d vectors whose leading *d* dimensions form a valid *d*-d
    embedding for any d <= 768.
    """
    truncated = vectors[:, :target_dim].copy()
    norms = np.linalg.norm(truncated, axis=1, keepdims=True)
    # Avoid division by zero for all-zero vectors
    norms = np.where(norms == 0, 1.0, norms)
    # Only normalize non-zero rows
    mask = np.linalg.norm(truncated, axis=1) > 0
    truncated[mask] = truncated[mask] / norms[mask]
    return truncated


# ---------------------------------------------------------------------------
# NomicLocalProvider
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "nomic-embed-text-v1.5"
_DEFAULT_DIMS = 512
_NATIVE_DIMS = 768
_HF_REPO = "nomic-ai/nomic-embed-text-v1.5"
_ONNX_FILENAME = "onnx/model_quantized.onnx"
_TOKENIZER_FILENAME = "tokenizer.json"


class NomicLocalProvider(EmbeddingProvider):
    """Local embedding provider using nomic-embed-text-v1.5 int8 ONNX.

    Downloads the model on first use to ~/.cache/memento-vault/models/.
    Requires ``onnxruntime`` at runtime; ``is_available()`` returns False
    if the package is not installed. The actual model file is downloaded
    lazily on the first ``embed()`` call.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIMS,
        cache_dir: Path | str | None = None,
    ):
        self._model_name = model
        self._dims = dimensions
        if cache_dir is not None:
            self._cache_dir = Path(cache_dir)
        else:
            self._cache_dir = (
                Path.home() / ".cache" / "memento-vault" / "models"
            )
        self._session = None  # lazy onnxruntime.InferenceSession
        self._tokenizer = None  # lazy tokenizers.Tokenizer

    # -- public API --

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed document texts (prefixed with ``search_document: ``)."""
        if not texts:
            return []
        self._ensure_runtime()
        prefixed = [self._format_document(t) for t in texts]
        raw = self._run_inference(prefixed)
        truncated = _truncate_and_normalize(raw, self._dims)
        return truncated.tolist()

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query (prefixed with ``search_query: ``)."""
        self._ensure_runtime()
        prefixed = [self._format_query(text)]
        raw = self._run_inference(prefixed)
        truncated = _truncate_and_normalize(raw, self._dims)
        return truncated[0].tolist()

    def dimensions(self) -> int:
        return self._dims

    def is_available(self) -> bool:
        """True if onnxruntime can be imported."""
        return self._check_onnxruntime_importable()

    # -- prefixing (nomic convention) --

    def _format_query(self, text: str) -> str:
        if text.startswith("search_query: "):
            return text
        return f"search_query: {text}"

    def _format_document(self, text: str) -> str:
        if text.startswith("search_document: "):
            return text
        return f"search_document: {text}"

    # -- model caching --

    def _model_cache_dir(self) -> Path:
        return self._cache_dir

    # -- runtime checks --

    @staticmethod
    def _check_onnxruntime_importable() -> bool:
        """Return True if onnxruntime is importable (not None-patched)."""
        try:
            mod = importlib.import_module("onnxruntime")
            return mod is not None
        except (ImportError, ModuleNotFoundError):
            return False

    def _ensure_runtime(self) -> None:
        """Ensure onnxruntime is available; raise RuntimeError if not."""
        if not self._check_onnxruntime_importable():
            raise RuntimeError(
                "onnxruntime is required for local embeddings. "
                "Install it with: pip install onnxruntime"
            )
        if self._session is None:
            self._load_model()

    # -- model loading --

    def _load_model(self) -> None:
        """Download (if needed) and load the ONNX model + tokenizer."""
        import onnxruntime as ort

        model_dir = self._cache_dir / self._model_name
        onnx_path = model_dir / "model_quantized.onnx"
        tokenizer_path = model_dir / "tokenizer.json"

        if not onnx_path.exists() or not tokenizer_path.exists():
            self._download_model(model_dir)

        # Load ONNX session
        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        # Use only CPU to keep it simple and portable
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )

        # Load tokenizer
        try:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except ImportError:
            logger.warning(
                "tokenizers package not installed; "
                "falling back to basic whitespace tokenization"
            )
            self._tokenizer = None

    def _download_model(self, model_dir: Path) -> None:
        """Download the ONNX model and tokenizer from Hugging Face Hub."""
        model_dir.mkdir(parents=True, exist_ok=True)

        try:
            from huggingface_hub import hf_hub_download

            logger.info("Downloading %s model from Hugging Face Hub...", self._model_name)

            hf_hub_download(
                repo_id=_HF_REPO,
                filename=_ONNX_FILENAME,
                local_dir=model_dir,
                local_dir_use_symlinks=False,
            )
            hf_hub_download(
                repo_id=_HF_REPO,
                filename=_TOKENIZER_FILENAME,
                local_dir=model_dir,
                local_dir_use_symlinks=False,
            )

            # hf_hub_download puts files in subdirs matching the repo structure.
            # Move them to the expected flat locations if needed.
            onnx_subdir = model_dir / "onnx" / "model_quantized.onnx"
            onnx_flat = model_dir / "model_quantized.onnx"
            if onnx_subdir.exists() and not onnx_flat.exists():
                os.replace(onnx_subdir, onnx_flat)

            logger.info("Model downloaded to %s", model_dir)

        except ImportError:
            raise RuntimeError(
                "huggingface_hub is required to download the embedding model. "
                "Install it with: pip install huggingface_hub"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to download embedding model: {exc}"
            ) from exc

    # -- inference --

    def _run_inference(self, texts: list[str]) -> np.ndarray:
        """Tokenize and run ONNX inference, returning raw (768-d) vectors."""
        input_ids, attention_mask = self._tokenize(texts)
        # token_type_ids: all zeros (single-segment)
        token_type_ids = np.zeros_like(input_ids)

        feeds = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

        # Run model — output[0] is last_hidden_state
        outputs = self._session.run(None, feeds)
        last_hidden = outputs[0]  # (batch, seq_len, hidden_dim)

        # Mean pooling over non-padding positions
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = (last_hidden * mask_expanded).sum(axis=1)
        counts = mask_expanded.sum(axis=1).clip(min=1e-9)
        pooled = summed / counts  # (batch, hidden_dim)

        return pooled.astype(np.float32)

    def _tokenize(
        self, texts: list[str], max_length: int = 512
    ) -> tuple[np.ndarray, np.ndarray]:
        """Tokenize texts into padded input_ids and attention_mask arrays."""
        if self._tokenizer is not None:
            return self._tokenize_hf(texts, max_length)
        return self._tokenize_basic(texts, max_length)

    def _tokenize_hf(
        self, texts: list[str], max_length: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Tokenize using the HuggingFace tokenizers library."""
        self._tokenizer.enable_truncation(max_length=max_length)
        self._tokenizer.enable_padding(length=max_length)
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array(
            [e.ids for e in encodings], dtype=np.int64
        )
        attention_mask = np.array(
            [e.attention_mask for e in encodings], dtype=np.int64
        )
        return input_ids, attention_mask

    def _tokenize_basic(
        self, texts: list[str], max_length: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """Fallback whitespace tokenizer — crude but functional.

        Assigns sequential integers as token IDs (not aligned with
        model vocabulary, so results will be degraded).
        """
        logger.warning(
            "Using basic whitespace tokenizer; install 'tokenizers' "
            "for proper nomic-embed-text tokenization"
        )
        # Build a simple vocab on the fly
        vocab: dict[str, int] = {}
        all_ids = []
        for text in texts:
            words = text.split()[:max_length]
            ids = []
            for w in words:
                w_lower = w.lower()
                if w_lower not in vocab:
                    vocab[w_lower] = len(vocab) + 1  # 0 = padding
                ids.append(vocab[w_lower])
            all_ids.append(ids)

        # Pad to uniform length
        max_len = min(max(len(ids) for ids in all_ids), max_length)
        input_ids = np.zeros((len(texts), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(texts), max_len), dtype=np.int64)
        for i, ids in enumerate(all_ids):
            length = min(len(ids), max_len)
            input_ids[i, :length] = ids[:length]
            attention_mask[i, :length] = 1

        return input_ids, attention_mask


# ---------------------------------------------------------------------------
# VoyageProvider
# ---------------------------------------------------------------------------

_VOYAGE_DEFAULT_MODEL = "voyage-3-lite"
_VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"


class VoyageProvider(EmbeddingProvider):
    """Voyage AI embedding provider.

    Uses the Voyage REST API via ``urllib.request`` — no SDK dependency.
    Supports batching (multiple texts in one request).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _VOYAGE_DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIMS,
    ):
        self._api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        self._model = model
        self._dims = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"input": texts, "model": self._model}
        data = self._api_call(body)
        return [item["embedding"] for item in data["data"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def dimensions(self) -> int:
        return self._dims

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _api_call(self, body: dict) -> dict:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            _VOYAGE_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            error_body = exc.fp.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"Voyage API error {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Voyage API connection error: {exc.reason}"
            ) from exc


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------

_OPENAI_DEFAULT_MODEL = "text-embedding-3-small"
_OPENAI_API_URL = "https://api.openai.com/v1/embeddings"


class OpenAIProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider.

    Uses the OpenAI REST API via ``urllib.request`` — no SDK dependency.
    Supports custom ``api_base`` for proxies and compatible services.
    Sends ``dimensions`` in the request for server-side truncation.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _OPENAI_DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIMS,
        api_base: str | None = None,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._model = model
        self._dims = dimensions
        self._api_base = api_base or _OPENAI_API_URL

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {
            "input": texts,
            "model": self._model,
            "dimensions": self._dims,
        }
        data = self._api_call(body)
        return [item["embedding"] for item in data["data"]]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def dimensions(self) -> int:
        return self._dims

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _api_call(self, body: dict) -> dict:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            self._api_base,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            error_body = exc.fp.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"OpenAI API error {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"OpenAI API connection error: {exc.reason}"
            ) from exc


# ---------------------------------------------------------------------------
# GoogleProvider
# ---------------------------------------------------------------------------

_GOOGLE_DEFAULT_MODEL = "text-embedding-004"
_GOOGLE_API_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/{model}:embedContent"
)


class GoogleProvider(EmbeddingProvider):
    """Google Generative AI embedding provider.

    Uses the ``embedContent`` REST endpoint via ``urllib.request``.
    Google's API takes one text per request, so ``embed()`` loops over texts.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _GOOGLE_DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIMS,
    ):
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        self._model = model
        self._dims = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results = []
        for text in texts:
            data = self._api_call(text)
            results.append(data["embedding"]["values"])
        return results

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def dimensions(self) -> int:
        return self._dims

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _api_call(self, text: str) -> dict:
        url = _GOOGLE_API_URL_TEMPLATE.format(model=self._model)
        url = f"{url}?key={self._api_key}"
        body = {"content": {"parts": [{"text": text}]}}
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            error_body = exc.fp.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"Google API error {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Google API connection error: {exc.reason}"
            ) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_embedding_provider(config: dict) -> EmbeddingProvider:
    """Create an embedding provider from config.

    Args:
        config: dict with optional keys ``embedding_provider``,
            ``embedding_model``, ``embedding_dimensions``.

    Returns:
        An EmbeddingProvider instance.

    Raises:
        ValueError: For unknown or not-yet-implemented providers.
    """
    provider_name = config.get("embedding_provider", "local")

    if provider_name == "local":
        return NomicLocalProvider(
            model=config.get("embedding_model", _DEFAULT_MODEL),
            dimensions=config.get("embedding_dimensions", _DEFAULT_DIMS),
        )

    api_key = config.get("embedding_api_key")
    model = config.get("embedding_model")
    dims = config.get("embedding_dimensions", _DEFAULT_DIMS)
    api_base = config.get("embedding_api_base")

    if provider_name == "voyage":
        return VoyageProvider(
            api_key=api_key,
            model=model or _VOYAGE_DEFAULT_MODEL,
            dimensions=dims,
        )

    if provider_name == "openai":
        return OpenAIProvider(
            api_key=api_key,
            model=model or _OPENAI_DEFAULT_MODEL,
            dimensions=dims,
            api_base=api_base,
        )

    if provider_name == "google":
        return GoogleProvider(
            api_key=api_key,
            model=model or _GOOGLE_DEFAULT_MODEL,
            dimensions=dims,
        )

    raise ValueError(
        f"Unknown embedding provider: '{provider_name}'. "
        "Supported: local, voyage, openai, google."
    )
