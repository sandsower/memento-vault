"""
Cross-encoder reranker for Tenet retrieval pipeline (Tier 2).
Re-scores (query, candidate) pairs via MiniLM-L-6-v2 ONNX model.

All dependencies are optional. If onnxruntime or tokenizers are missing,
rerank() returns results unchanged.
"""

import os
from pathlib import Path

import numpy as np


MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MODEL_CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.join(str(Path.home()), ".cache")),
    "memento-vault",
    "models",
    "minilm-l6-v2",
)

_SESSION = None  # module-level cache: (ort_session, tokenizer)
_DEPS_AVAILABLE = None  # cached dep-check result


def _check_deps():
    """Return True if onnxruntime, tokenizers, huggingface_hub are importable."""
    global _DEPS_AVAILABLE
    if _DEPS_AVAILABLE is not None:
        return _DEPS_AVAILABLE

    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import huggingface_hub  # noqa: F401

        _DEPS_AVAILABLE = True
    except ImportError:
        _DEPS_AVAILABLE = False

    return _DEPS_AVAILABLE


def _ensure_model(model_id=None, cache_dir=None):
    """Download model files if not already cached. Returns cache_dir."""
    model_id = model_id or MODEL_ID
    cache_dir = cache_dir or MODEL_CACHE_DIR

    model_path = os.path.join(cache_dir, "model.onnx")
    tokenizer_path = os.path.join(cache_dir, "tokenizer.json")

    if os.path.exists(model_path) and os.path.exists(tokenizer_path):
        return cache_dir

    os.makedirs(cache_dir, exist_ok=True)

    from huggingface_hub import hf_hub_download

    # Download ONNX model
    hf_hub_download(
        repo_id=model_id,
        filename="onnx/model.onnx",
        local_dir=cache_dir,
        local_dir_use_symlinks=False,
    )
    # Move from onnx/ subdirectory to cache root if needed
    nested = os.path.join(cache_dir, "onnx", "model.onnx")
    if os.path.exists(nested) and not os.path.exists(model_path):
        os.rename(nested, model_path)

    # Download tokenizer
    hf_hub_download(
        repo_id=model_id,
        filename="tokenizer.json",
        local_dir=cache_dir,
        local_dir_use_symlinks=False,
    )

    return cache_dir


def _load_session(cache_dir=None):
    """Load or return cached (ort_session, tokenizer) pair."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    import onnxruntime as ort
    import tokenizers

    cache_dir = _ensure_model(cache_dir=cache_dir)
    model_path = os.path.join(cache_dir, "model.onnx")
    tokenizer_path = os.path.join(cache_dir, "tokenizer.json")

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    tokenizer = tokenizers.Tokenizer.from_file(tokenizer_path)

    _SESSION = (session, tokenizer)
    return _SESSION


def _score_pairs(session, tokenizer, query, texts):
    """Score (query, text) pairs through the cross-encoder. Returns list of floats."""
    if not texts:
        return []

    # Tokenize each pair
    encodings = [tokenizer.encode(query, text) for text in texts]

    # Pad to max length in batch
    max_len = max(len(enc.ids) for enc in encodings)

    input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
    attention_mask = np.zeros((len(encodings), max_len), dtype=np.int64)
    token_type_ids = np.zeros((len(encodings), max_len), dtype=np.int64)

    for i, enc in enumerate(encodings):
        length = len(enc.ids)
        input_ids[i, :length] = enc.ids
        attention_mask[i, :length] = enc.attention_mask
        token_type_ids[i, :length] = enc.type_ids

    # Check which inputs the model expects
    input_names = {inp.name for inp in session.get_inputs()}
    feed = {"input_ids": input_ids, "attention_mask": attention_mask}
    if "token_type_ids" in input_names:
        feed["token_type_ids"] = token_type_ids

    outputs = session.run(None, feed)
    logits = outputs[0]  # shape: (batch_size, 1) or (batch_size,)

    # Flatten and apply sigmoid for 0-1 range
    logits = logits.flatten()
    scores = (1.0 / (1.0 + np.exp(-logits))).tolist()

    return scores


def rerank(query, results, config=None):
    """Re-rank results using cross-encoder model.

    Returns results re-sorted by cross-encoder score.
    If deps missing or model unavailable, returns results unchanged.
    """
    if config is None:
        from memento_utils import get_config

        config = get_config()

    if not config.get("reranker_enabled", True):
        return results

    if not _check_deps():
        return results

    if len(results) <= 1:
        return results

    top_k = config.get("reranker_top_k", 10)
    min_score = config.get("reranker_min_score", 0.01)

    # Split: rerank top_k, keep the rest in original order
    to_rerank = results[:top_k]
    rest = results[top_k:]

    try:
        session, tokenizer = _load_session()
    except Exception:
        return results  # model not available

    # Build texts from titles + snippets
    texts = []
    for r in to_rerank:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        if snippet:
            text = f"{title}. {snippet}" if title else snippet
        else:
            text = title
        texts.append(text or "")

    try:
        scores = _score_pairs(session, tokenizer, query, texts)
    except Exception:
        return results  # inference failed

    # Apply new scores and re-sort
    for r, score in zip(to_rerank, scores):
        r["_rerank_score"] = score
        r["score"] = score

    # Filter by min_score
    to_rerank = [r for r in to_rerank if r.get("_rerank_score", 0) >= min_score]

    # Sort by cross-encoder score descending
    to_rerank.sort(key=lambda r: r.get("_rerank_score", 0), reverse=True)

    # Clean internal metadata
    for r in to_rerank:
        r.pop("_rerank_score", None)

    return to_rerank + rest
