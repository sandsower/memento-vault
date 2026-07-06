# Search backends

The vault auto-detects the best available search backend:

1. **QMD** (if installed) -- BM25 + vector search + reranking via external CLI tool (3.2GB)
2. **Embedded** (if onnxruntime + sqlite-vec installed) -- built-in FTS5 + sqlite-vec with nomic-embed-text, RRF hybrid fusion. No external tools needed. Default on remote/Docker deployments.
3. **Grep** -- substring matching fallback. Always works, no dependencies.

Override with `search_backend: qmd | embedded | grep` in config, or `MEMENTO_SEARCH_BACKEND` env var.

The embedded backend uses a single `search.db` SQLite file (derived, disposable).
Markdown files stay the source of truth.
Embeddings come from a local nomic-embed-text-v1.5 model by default (137MB, no API key).
Optional API providers (Voyage, OpenAI, Google) configurable via `embedding_provider` in config.

## QMD (optional)

QMD adds semantic search over your vault.
Without it the concierge agent uses the embedded backend or falls back to grep.
QMD is required for Tenet and Inception.

```bash
qmd search "caching strategy" -c memento
```

The concierge agent uses QMD automatically when you ask about past decisions.

## Related

- [Install](install.md#model-warmup) -- warming up the embedding model after a reboot
- [Architecture](architecture.md) -- where `search_backend.py` and `embedded_search.py` sit in the module map
- [Performance analysis](performance-analysis.md) -- latency and hit-rate benchmarks per backend
