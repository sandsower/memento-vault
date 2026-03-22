"""
Retrieval layer for LongMemEval benchmark.
Uses rank_bm25 for per-question BM25 indexing, producing result dicts
compatible with memento_utils enhancement functions.
"""

from dataclasses import dataclass, field
from rank_bm25 import BM25Okapi
import re


@dataclass
class BM25Index:
    """A BM25 index over a set of documents."""
    bm25: BM25Okapi
    documents: list  # original document dicts
    corpus_tokens: list  # tokenized corpus


def tokenize(text):
    """Simple whitespace + punctuation tokenizer for BM25."""
    # Lowercase, split on non-alphanumeric, filter short tokens
    return [w for w in re.split(r'[^a-z0-9]+', text.lower()) if len(w) > 1]


def build_bm25_index(documents):
    """Build a BM25 index from documents.

    Args:
        documents: list of dicts with keys: id, text, metadata (optional)
            - id: unique document identifier (e.g., session_id)
            - text: document content to index
            - metadata: dict with optional keys: date, title, session_id

    Returns:
        BM25Index object
    """
    corpus_tokens = [tokenize(doc["text"]) for doc in documents]
    bm25 = BM25Okapi(corpus_tokens)
    return BM25Index(bm25=bm25, documents=documents, corpus_tokens=corpus_tokens)


def bm25_search(index, query, limit=10, min_score=0.0):
    """Search a BM25 index and return results in memento-compatible format.

    Args:
        index: BM25Index from build_bm25_index
        query: search query string
        limit: max results to return
        min_score: minimum BM25 score threshold

    Returns:
        list of dicts matching memento_utils format:
        [{"path": str, "title": str, "score": float, "snippet": str}]
        Sorted by score descending.
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    scores = index.bm25.get_scores(query_tokens)

    # Build results sorted by score
    scored = [(i, float(scores[i])) for i in range(len(scores)) if scores[i] > min_score]
    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for i, score in scored[:limit]:
        doc = index.documents[i]
        # Normalize score to 0-1 range (BM25 scores are unbounded)
        # Use simple sigmoid-like normalization
        norm_score = score / (score + 1.0) if score > 0 else 0.0

        # Extract snippet (first 200 chars of text)
        text = doc.get("text", "")
        snippet = text[:200].strip()

        # Build path from document id (mimics vault note path)
        doc_id = doc.get("id", f"doc-{i}")

        results.append({
            "path": f"notes/{doc_id}.md",
            "title": doc.get("metadata", {}).get("title", doc_id),
            "score": norm_score,
            "snippet": snippet,
            "_raw_score": score,  # keep raw score for debugging
            "_doc_id": doc_id,
        })

    return results
