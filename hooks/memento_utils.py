#!/usr/bin/env python3
"""
Shared utilities for memento-vault hooks.

This module re-exports from the memento package for backwards compatibility.
New code should import from memento.config, memento.search, etc. directly.
"""

import sys
from pathlib import Path

# Add the repo root to sys.path so `import memento` works from hooks/
_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from memento.config import (  # noqa: E402, F401
    DEFAULT_CONFIG,
    RUNTIME_DIR,
    detect_project,
    get_config,
    get_runtime_dir,
    get_vault,
    load_config,
    slugify,
)
from memento.graph import (  # noqa: E402, F401
    _GRAPH_CACHE,
    _deserialize_graph,
    _serialize_graph,
    apply_pagerank_boost,
    build_wikilink_graph,
    compute_pagerank,
    extract_wikilinks,
    load_concept_index,
    load_or_build_graph,
    load_project_maps,
    lookup_concepts,
    lookup_project_notes,
    note_is_superseded,
    ppr_expand,
    read_note_metadata,
)
from memento.search import (  # noqa: E402, F401
    VSEARCH_WARM_PATH,
    _clean_snippet,
    _extract_expansion_terms,
    apply_temporal_decay,
    enhance_results,
    expand_wikilinks,
    filter_by_project,
    has_qmd,
    is_vsearch_warm,
    mark_vsearch_warm,
    multi_hop_search,
    prf_expand_query,
    qmd_get,
    qmd_search,
    qmd_search_with_extras,
    rrf_fuse,
)
from memento.store import (  # noqa: E402, F401
    INCEPTION_LOCK_PATH,
    INCEPTION_STATE_PATH,
    RETRIEVAL_LOG_PATH,
    _should_log,
    acquire_inception_lock,
    acquire_vault_write_lock,
    find_dedup_candidates,
    load_inception_state,
    log_retrieval,
    release_inception_lock,
    release_vault_write_lock,
    save_inception_state,
    update_project_index,
    write_note,
)
from memento.utils import (  # noqa: E402, F401
    _COMPILED_SECRET_PATTERNS,
    _SECRET_PATTERNS,
    normalize_note_tags,
    normalize_tags,
    read_hook_input,
    sanitize_secrets,
)
