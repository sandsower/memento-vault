"""Shared retrieval policy/runtime decisions for recall and search surfaces.

The public host/MCP adapters keep their payload formats, while this module owns
structured retrieval decisions that those adapters can format.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from memento.config import detect_project as default_detect_project
from memento.config import get_config, get_vault, slugify
from memento.graph import lookup_concepts, read_note_metadata
from memento.search import (
    MISS_RECOVERY_HINTS,
    build_search_miss,
    enhance_results as default_enhance_results,
    filter_by_project as default_filter_by_project,
    has_qmd,
    miss_envelope,
    normalize_miss_reason,
    is_vsearch_warm,
    multi_hop_search,
    prf_expand_query,
    qmd_search_with_extras,
    resolve_concrete_mode,
    rrf_fuse,
    shape_search_results,
)
from memento.store import log_retrieval as default_log_retrieval
from memento.store import record_access as default_record_access


@dataclass(frozen=True)
class PromptRecallRequest:
    """Inputs needed to resolve prompt-time recall."""

    prompt: str
    cwd: str = ""
    session_id: str = "unknown"
    host_id: str | None = None


@dataclass
class RetrievalDecision:
    """Structured retrieval decision shared by host/MCP adapters."""

    should_inject: bool
    source: str
    lines: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    reason: str | None = None
    metadata: dict = field(default_factory=dict)
    top_path: str | None = None

    @property
    def content(self) -> str:
        return "\n".join(self.lines)


class StructuredMissReason(str):
    """String reason that carries a structured miss payload through recall internals."""

    def __new__(cls, reason: str, miss: dict):
        obj = str.__new__(cls, reason)
        obj.miss = miss
        return obj


RECALL_CONTROL_WORDS = {
    "a",
    "after",
    "again",
    "ahead",
    "all",
    "and",
    "continue",
    "do",
    "for",
    "fresh",
    "go",
    "it",
    "lets",
    "next",
    "ok",
    "on",
    "one",
    "ship",
    "slice",
    "start",
    "the",
    "this",
    "to",
    "what",
    "whats",
    "is",
}

NATURAL_QUERY_STOPWORDS = RECALL_CONTROL_WORDS | {
    "about",
    "an",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "before",
    "by",
    "can",
    "did",
    "does",
    "decide",
    "decided",
    "from",
    "had",
    "has",
    "have",
    "having",
    "how",
    "in",
    "into",
    "of",
    "or",
    "our",
    "should",
    "that",
    "their",
    "there",
    "these",
    "they",
    "was",
    "we",
    "were",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "without",
    "appear",
    "appears",
    "between",
    "changing",
    "instead",
    "live",
    "code",
    "permission",
    "permissions",
    "return",
    "status",
}

_SHORT_SEARCH_TERMS = {"api", "aws", "db", "dns", "gcp", "jwt", "mcp", "pr", "rls", "sqs", "ttl", "ui", "url"}

_QUERY_TERM_ALIASES = {
    "queues": "queues",
    "remembering": "remember",
    "upgrades": "upgrade",
    "urls": "url",
}

RECALL_DOMAIN_ALLOWLIST = {
    "backend",
    "capture",
    "dedup",
    "extract",
    "lifecycle",
    "mcp",
    "queue",
    "sync",
    "ticket",
    "tolgee",
}

LOW_SIGNAL_RECALL_PATTERNS = (
    r"^(ok[, ]*)?(go for it|go ahead|do it|continue|start fresh|lets start fresh|let's start fresh|ship it)$",
    r"^(go for the )?(next|next one|next slice)$",
    r"^what (is|'s|s) the next( slice| step| feature)?\??$",
    r"^go for the [a-z0-9 _-]+ cleanup$",
)

BROAD_PROJECT_HISTORY_PATTERNS = (
    r"^what previous decisions did we make (?:on|about|for) (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
    r"^what do we know about (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
    r"^summari[sz]e (?P<subject>[a-z0-9][a-z0-9 _-]*?)(?: history)?\??$",
    r"^what was decided before about (?P<subject>[a-z0-9][a-z0-9 _-]*)\??$",
)

PROJECT_ENTITY_STOPWORDS = {
    "what",
    "previous",
    "decisions",
    "make",
    "know",
    "about",
    "summarize",
    "summarise",
    "history",
    "decided",
    "before",
}
SPECIFIC_PROJECT_QUERY_PATTERNS = (r"\bwhat did we decide about (?P<subject>[a-z0-9][a-z0-9 _-]*)",)

RETRIEVAL_REASON_ALIASES = {
    "broad-project-query": "query_too_broad",
    "filtered-empty": "no_exact_match",
    "low-signal-prompt": "query_too_broad",
    "no-results": "no_exact_match",
    "project-mismatch-filtered-empty": "project_filter_removed_all",
    "skipped-prompt": "query_too_broad",
}

SEARCH_MISS_REASONS = {
    "no_concrete_match",
    "no_exact_match",
    "project_filter_removed_all",
    "query_too_broad",
    "threshold_too_high",
}

TOOL_CONTEXT_MISS_REASONS = {
    "duplicate",
    "ignored",
    "no-results",
    "no_results",
    "no_exact_match",
    "skipped",
}

DEEP_PIPELINE_MARKERS = {"prf", "ce", "rerank", "multi_hop", "deep"}

# Default relative gap (fraction of the top score) the leading result must
# hold over the runner-up before the deep pipeline treats it as confident
# enough to skip expansion. See confidence_margin() below for why this
# replaces a single absolute score threshold.
DEFAULT_RECALL_CONFIDENCE_MARGIN = 0.30


def confidence_margin(results: list[dict]) -> float:
    """Relative score gap between the top result and the runner-up.

    A single absolute score threshold (the old `top_score < high_conf`
    gate) breaks down across un-normalized, per-backend score scales:
    QMD's correct hits commonly score 0.96-0.97 while a barely-related
    catch-all note scores 0.87-0.89 - both comfortably above any absolute
    "confident" cutoff tuned for this backend, and neither number is
    meaningful next to another backend's scale. A relative margin sidesteps
    that: it only reads as confident when the leader is decisively clear of
    the field, which holds regardless of the backend's absolute scale - this
    is why confidence_margin() (and single_strong_hit below) were already
    backend-agnostic before MEM-127 landed, with no separate qmd-only
    identity gate to remove here: PRF/RRF/rerank/multi_hop/deep_recall all
    key off `confident` (this function plus single_strong_hit), never off
    the backend's type.

    MEM-127 fixed the actual root cause this function was working around:
    per-backend score scales weren't normalized at the source. The concrete
    bug that mattered most here was the embedded backend's FTS5 search
    normalizing scores by `score / max_score_in_this_batch`, which forced
    the top hit in *any* result batch to exactly 1.0 regardless of true
    relevance - so a single mediocre embedded hit always looked like a
    perfect match to single_strong_hit's absolute check (this function
    can't establish a margin from a single result at all, see below), and
    the deep pipeline never got a chance to run on it. See
    memento.embedded_search.normalize_fts5_score /
    normalize_vec_cosine_distance and memento.search_backend.
    normalize_qmd_score / normalize_grep_term_coverage for the fix, and
    every backend.search() result now also carries a `backend` field
    (qmd | embedded-fts | embedded-vec | grep) for downstream diagnostics.

    Fewer than two results can never establish a margin - including zero
    results, and a single result with no runner-up to compare against -
    so both read as "not confident" by construction and fall through to
    expansion rather than being treated as a trivially safe pick.
    """
    if len(results) < 2:
        return 0.0
    top = results[0].get("score") or 0
    if top <= 0:
        return 0.0
    second = results[1].get("score") or 0
    return (top - second) / top


def _singularize_query_term(token: str) -> str:
    if token in _QUERY_TERM_ALIASES:
        return _QUERY_TERM_ALIASES[token]
    if token in _SHORT_SEARCH_TERMS:
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "is")):
        return token[:-1]
    return token


def normalized_natural_query(prompt: str) -> str:
    """Return a lexical-search-friendly variant for question-shaped prompts.

    Day-to-day memory requests are often phrased as questions ("how should we
    store bearer tokens that appear in URLs?"). BM25 backends tend to treat the
    stopword-heavy raw prompt as either empty or dominated by incidental words,
    while the relevant note contains the durable concepts ("store bearer token
    URL"). This helper keeps that deterministic and backend-agnostic before the
    pipeline falls back to semantic search.
    """
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9-]*", (prompt or "").lower())
    normalized: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        if token in NATURAL_QUERY_STOPWORDS:
            continue
        if len(token) <= 2 and token not in _SHORT_SEARCH_TERMS:
            continue
        term = _singularize_query_term(token)
        if term and term not in seen:
            normalized.append(term)
            seen.add(term)
    return " ".join(normalized)


def recall_signal_terms(prompt: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9-]+", (prompt or "").lower())
    return [token for token in tokens if token not in RECALL_CONTROL_WORDS and len(token) > 2]


def is_low_signal_recall_prompt(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", (prompt or "").lower()).strip().strip(".!?")
    if not normalized:
        return True
    if any(re.match(pattern, normalized) for pattern in LOW_SIGNAL_RECALL_PATTERNS):
        return True
    signal_terms = recall_signal_terms(normalized)
    if len(signal_terms) >= 2:
        return False
    return not (len(signal_terms) == 1 and signal_terms[0] in RECALL_DOMAIN_ALLOWLIST)


def _subject_is_broad(subject: str) -> bool:
    terms = recall_signal_terms(subject)
    return 0 < len(terms) <= 3


def is_broad_project_history_query(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", (prompt or "").lower()).strip().strip(".!")
    if not normalized:
        return False
    for pattern in BROAD_PROJECT_HISTORY_PATTERNS:
        match = re.match(pattern, normalized)
        if match and _subject_is_broad(match.group("subject")):
            return True
    return False


def should_skip_recall(prompt: str, config: dict, concrete: bool = False) -> bool:
    prompt = (prompt or "").strip()
    if not concrete:
        if len(prompt) < 10:
            return True
        if config.get("recall_skip_low_signal", True) and is_low_signal_recall_prompt(prompt):
            return True
        if config.get("recall_skip_broad_project_queries", True) and is_broad_project_history_query(prompt):
            return True
    if prompt.startswith("/"):
        return True
    if "<command-message>" in prompt or "<command-name>" in prompt or "<task-notification>" in prompt:
        return True
    if prompt.startswith("# ") and len(prompt) > 200:
        return True
    if "You are working on" in prompt or prompt.startswith("Continuation guidance:"):
        return True
    if "<local-command-caveat>" in prompt:
        return True
    if len(prompt) > 200:
        return True
    for pattern in config.get("recall_skip_patterns", []):
        try:
            if re.match(pattern, prompt.lower().strip(), re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def should_append_project_to_recall(prompt: str, concrete: bool = False) -> bool:
    return not concrete and not is_low_signal_recall_prompt(prompt) and not is_broad_project_history_query(prompt)


def _project_slug_from_value(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip().strip('"').strip("'")
    if not value:
        return ""
    if "/" in value or "\\" in value:
        value = Path(value).name
    return slugify(value)


def _candidate_project_slug(result: dict) -> str:
    project = result.get("project")
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else None
    if not project and meta:
        project = meta.get("project")
    if not project:
        note_path = result.get("path", "")
        if note_path:
            meta = read_note_metadata(note_path)
            if meta:
                project = meta.get("project")
    return _project_slug_from_value(project)


def _subject_project_slug(subject: str) -> str:
    terms = recall_signal_terms(subject)
    if not terms:
        return ""
    slug = slugify(terms[0])
    if not slug or slug in PROJECT_ENTITY_STOPWORDS or slug in RECALL_DOMAIN_ALLOWLIST:
        return ""
    return slug


def _prompt_project_slugs(prompt: str) -> set[str]:
    slugs = set()
    for pattern in SPECIFIC_PROJECT_QUERY_PATTERNS:
        match = re.search(pattern, prompt.lower())
        if not match:
            continue
        slug = _subject_project_slug(match.group("subject"))
        if slug:
            slugs.add(slug)
    return slugs


def _explicit_project_slugs(prompt: str, results: list[dict]) -> set[str]:
    normalized_prompt = f" {slugify(prompt).replace('-', ' ')} "
    slugs = set()

    for result in results:
        candidate_slug = _candidate_project_slug(result)
        if not candidate_slug:
            continue
        phrase = candidate_slug.replace("-", " ")
        if f" {phrase} " in normalized_prompt:
            slugs.add(candidate_slug)

    slugs.update(_prompt_project_slugs(prompt))

    return slugs


def filter_recall_results_by_explicit_project(prompt: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop results with explicit mismatching project metadata."""
    explicit_slugs = _explicit_project_slugs(prompt, results)
    if not explicit_slugs:
        return results, []

    filtered = []
    decisions = []
    for result in results:
        candidate_slug = _candidate_project_slug(result)
        if not candidate_slug:
            filtered.append(result)
            decisions.append(_candidate_summary(result, "no-project-metadata"))
        elif candidate_slug in explicit_slugs:
            filtered.append(result)
            decisions.append(_candidate_summary(result, "project-match"))
        else:
            decisions.append(_candidate_summary(result, "project-mismatch"))
    return filtered, decisions


def _strip_injection(text: str) -> str:
    """Strip instruction-like patterns from injected content (defense-in-depth)."""
    if not text:
        return text
    text = re.sub(r"(?i)(ignore\s+(all\s+)?previous\s+instructions)", "[filtered]", str(text))
    text = re.sub(r"(?i)(you\s+are\s+now\s+|you\s+must\s+now\s+)", "[filtered]", text)
    text = re.sub(r"(?i)^(system|assistant)\s*:", "[filtered]:", text)
    text = re.sub(r"</?s>", "", text)
    return text


def _compact_injected_text(text: object, *, sentence_boundary: bool = False, limit: int = 120) -> str:
    text = _strip_injection(str(text or "").strip())
    if not text:
        return ""
    if sentence_boundary:
        dot = text.find(".")
        if 0 < dot < limit:
            return text[: dot + 1]
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _candidate_summary(result: dict, decision: str = "candidate") -> dict:
    return {
        "path": result.get("path", ""),
        "title": _compact_injected_text(result.get("title", "")),
        "score": round(float(result.get("score", 0) or 0), 4),
        "decision": decision,
    }


def _format_result(result: dict) -> str:
    """Format a retrieval result as a compact sanitized one-liner."""
    title = _compact_injected_text(result.get("title") or result.get("path", ""))
    snippet = _compact_injected_text(result.get("snippet", ""), sentence_boundary=True)
    if snippet:
        return f"  - {title}: {snippet}"
    return f"  - {title}"


def _empty_decision(source: str, reason: str, *, query: str = "", details: dict | None = None) -> RetrievalDecision:
    metadata = {}
    normalized = normalize_miss_reason(reason, query)
    if normalized in MISS_RECOVERY_HINTS:
        metadata["miss"] = build_search_miss(normalized, details=details)
    return RetrievalDecision(False, source, reason=reason, metadata=metadata)


@dataclass(frozen=True)
class ExplicitSearchRequest:
    """Inputs for explicit MCP/user-facing vault search."""

    query: str
    limit: int = 5
    semantic: bool = False
    min_score: float = 0.0
    cwd: str = ""
    concrete: str | bool = "auto"
    detail_level: str = "summary"
    include_content: bool = False
    token_budget: int | None = 2000


class ExplicitSearchRuntime:
    """Runtime for explicit search policy shared by MCP/search adapters."""

    def __init__(
        self,
        *,
        vault_loader: Callable[[], Path] = get_vault,
        has_backend: Callable[[], bool] = has_qmd,
        qmd_search: Callable[..., list[dict]] = qmd_search_with_extras,
        enhance_results: Callable[..., list[dict]] = default_enhance_results,
        filter_by_project: Callable[..., list[dict]] = default_filter_by_project,
        log_retrieval: Callable[..., None] = default_log_retrieval,
        record_access: Callable[..., None] = default_record_access,
    ) -> None:
        self.vault_loader = vault_loader
        self.has_backend = has_backend
        self.qmd_search = qmd_search
        self.enhance_results = enhance_results
        self.filter_by_project = filter_by_project
        self.log_retrieval = log_retrieval
        self.record_access = record_access

    def _search_metadata(
        self,
        request: ExplicitSearchRequest,
        vault: Path | None = None,
        *,
        backend: str | None = None,
        backend_index: str = "unknown",
        query_variant: str | None = None,
        semantic_used: bool | None = None,
        concrete_enabled: bool | None = None,
    ) -> dict:
        metadata = shape_search_results(
            [],
            vault=vault or self.vault_loader(),
            detail_level=request.detail_level,
            include_content=request.include_content,
            token_budget=request.token_budget,
        )["metadata"]
        if backend:
            metadata["backend"] = backend
        metadata["backend_index"] = backend_index
        if query_variant:
            metadata["query_variant"] = query_variant
        if semantic_used is not None:
            metadata["semantic_used"] = bool(semantic_used)
        if concrete_enabled is not None:
            metadata["concrete_enabled"] = bool(concrete_enabled)
        return metadata

    def _miss(self, reason: str, request: ExplicitSearchRequest, *, details: dict | None = None) -> dict:
        miss = miss_envelope(reason, details=details, metadata=self._search_metadata(request))
        self.log_retrieval("mcp", "search_miss", query=request.query, reason=miss["miss"]["reason"])
        return miss

    def search(self, request: ExplicitSearchRequest) -> dict:
        query = request.query or ""
        if not query.strip():
            return self._miss("query_too_broad", request, details={"query": request.query})

        try:
            limit = max(1, min(int(request.limit), 50))
        except (TypeError, ValueError):
            limit = 5

        vault = self.vault_loader()
        search_metadata = self._search_metadata(request, vault)
        if not vault.exists() or not any(
            (vault / directory).exists() for directory in ("notes", "fleeting", "projects")
        ):
            miss = miss_envelope("empty_vault", details={"vault": str(vault)}, metadata=search_metadata)
            self.log_retrieval("mcp", "search_miss", query=query, reason=miss["miss"]["reason"])
            return miss

        backend_name = "unknown"
        try:
            from memento.search_backend import get_backend

            backend_name = type(get_backend()).__name__
        except Exception:
            pass
        search_metadata = self._search_metadata(request, vault, backend=backend_name)

        if not self.has_backend():
            miss = miss_envelope("backend_unavailable", metadata=search_metadata)
            self.log_retrieval("mcp", "search_miss", query=query, reason=miss["miss"]["reason"])
            return miss

        concrete_enabled, _auto_selected = resolve_concrete_mode(request.concrete, query)
        concrete_auto_mode = request.concrete is None or (
            isinstance(request.concrete, str) and request.concrete.strip().lower() in ("", "auto")
        )
        search_metadata = self._search_metadata(
            request,
            vault,
            backend=backend_name,
            semantic_used=bool(request.semantic and not concrete_enabled),
            concrete_enabled=concrete_enabled,
        )
        conceptual_miss_reason = normalize_miss_reason("no-results", query) if concrete_auto_mode else "no_exact_match"
        semantic_used = bool(request.semantic and not concrete_enabled)
        query_variant = ""
        raw_results = self.qmd_search(
            query,
            limit=limit + 3,
            semantic=False if concrete_enabled else request.semantic,
            timeout=10,
            min_score=request.min_score,
            concrete=concrete_enabled,
        )
        results = raw_results

        if not concrete_enabled and not request.semantic:
            variant = normalized_natural_query(query)
            if (
                variant
                and variant != query
                and (not results or confidence_margin(results) < DEFAULT_RECALL_CONFIDENCE_MARGIN)
            ):
                query_variant = variant
                variant_results = self.qmd_search(
                    variant,
                    limit=limit + 3,
                    semantic=False,
                    timeout=10,
                    min_score=request.min_score,
                    concrete=False,
                )
                if variant_results:
                    existing = {result.get("path") for result in results}
                    for result in variant_results:
                        if result.get("path") not in existing:
                            results.append(result)
                            existing.add(result.get("path"))
                    results.sort(key=lambda result: result.get("score", 0), reverse=True)
                    raw_results = results
                search_metadata = self._search_metadata(
                    request,
                    vault,
                    backend=backend_name,
                    query_variant=query_variant,
                    semantic_used=semantic_used,
                    concrete_enabled=concrete_enabled,
                )

        if results:
            if concrete_enabled:
                if request.cwd:
                    results = self.filter_by_project(results, request.cwd)
            else:
                results = self.enhance_results(results, cwd=request.cwd or None)

        if not results:
            if raw_results:
                reason = "project_filter_removed_all"
                details = {"cwd": request.cwd} if request.cwd else None
            elif request.min_score > 0:
                low_threshold_results = self.qmd_search(
                    query,
                    limit=1,
                    semantic=False if concrete_enabled else request.semantic,
                    timeout=10,
                    min_score=0.0,
                    concrete=concrete_enabled,
                )
                reason = (
                    "threshold_too_high"
                    if low_threshold_results
                    else ("no_concrete_match" if concrete_enabled else conceptual_miss_reason)
                )
                details = {"min_score": request.min_score} if reason == "threshold_too_high" else None
            else:
                reason = "no_concrete_match" if concrete_enabled else conceptual_miss_reason
                details = None
            miss = miss_envelope(reason, details=details, metadata=search_metadata)
            self.log_retrieval("mcp", "search_miss", query=query, reason=miss["miss"]["reason"])
            return miss

        shaped = shape_search_results(
            results[:limit],
            vault=vault,
            detail_level=request.detail_level,
            include_content=request.include_content,
            token_budget=request.token_budget,
        )
        output = shaped["results"]
        metadata = shaped["metadata"]
        metadata.update(
            {
                "backend": backend_name,
                "backend_index": "unknown",
                "semantic_used": semantic_used,
                "concrete_enabled": concrete_enabled,
            }
        )
        if query_variant:
            metadata["query_variant"] = query_variant

        self.log_retrieval("mcp", "search", query=query, results=len(output))
        self.record_access(
            [entry["path"] for entry in output if entry.get("path")],
            hook="mcp",
            tool="search",
            query=query,
            result_count=len(output),
        )
        return {"results": output, "metadata": metadata}


class PromptRecallRuntime:
    """Runtime for prompt recall policy decisions.

    Dependencies are injectable so callers can preserve existing adapters while
    tests exercise the policy seam directly.
    """

    def __init__(
        self,
        *,
        config_loader: Callable[[], dict] = get_config,
        vault_loader: Callable[[], Path] = get_vault,
        has_backend: Callable[[], bool] = has_qmd,
        remote_available: Callable[[], bool] | None = None,
        remote_search_envelope: Callable[..., dict] | None = None,
        detect_project: Callable[[str], tuple[str, object]] = lambda cwd: default_detect_project(cwd, None),
        qmd_search: Callable[..., list[dict]] = qmd_search_with_extras,
        enhance_results: Callable[..., list[dict]] = default_enhance_results,
        recently_injected_paths: Callable[..., set[str]] = lambda *_args, **_kwargs: set(),
        bump_prompts_since: Callable[..., None] = lambda *_args, **_kwargs: None,
        concept_lookup: Callable[[str], list[dict]] = lookup_concepts,
        prf_expand: Callable[..., str] = prf_expand_query,
        semantic_warm: Callable[[], bool] = is_vsearch_warm,
        fuse_results: Callable[..., list[dict]] = rrf_fuse,
        multi_hop: Callable[..., list[dict]] = multi_hop_search,
        deep_recall_pending_exists: Callable[[], bool] = lambda: True,
        spawn_deep_recall: Callable[..., None] = lambda *_args, **_kwargs: None,
        rerank_results: Callable[[str, list[dict], dict], list[dict]] | None = None,
        log_retrieval: Callable[..., None] = default_log_retrieval,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config_loader = config_loader
        self.vault_loader = vault_loader
        self.has_backend = has_backend
        self.remote_available = remote_available or (lambda: False)
        self.remote_search_envelope = remote_search_envelope
        self.detect_project = detect_project
        self.qmd_search = qmd_search
        self.enhance_results = enhance_results
        self.recently_injected_paths = recently_injected_paths
        self.bump_prompts_since = bump_prompts_since
        self.concept_lookup = concept_lookup
        self.prf_expand = prf_expand
        self.semantic_warm = semantic_warm
        self.fuse_results = fuse_results
        self.multi_hop = multi_hop
        self.deep_recall_pending_exists = deep_recall_pending_exists
        self.spawn_deep_recall = spawn_deep_recall
        self.rerank_results = rerank_results
        self.log_retrieval = log_retrieval
        self.now = now

    def _diagnostic_enabled(self, config: dict) -> bool:
        return bool(config.get("recall_diagnostics", False))

    def _log_diagnostic(self, config: dict, action: str, **kwargs) -> None:
        if self._diagnostic_enabled(config):
            self.log_retrieval("recall", f"diagnostic-{action}", **kwargs)

    def _log_candidates(self, config: dict, results: list[dict], stage: str, **kwargs) -> None:
        if not self._diagnostic_enabled(config) or not config.get("recall_diagnostics_include_candidates", False):
            return
        max_candidates = int(config.get("recall_diagnostics_max_candidates", 10) or 10)
        candidates = [_candidate_summary(result) for result in results[: max(0, max_candidates)]]
        self._log_diagnostic(config, "candidates", stage=stage, candidates=candidates, **kwargs)

    def _project_slug(self, cwd: str) -> str:
        if not cwd:
            return "unknown"
        try:
            project_slug, _ = self.detect_project(cwd)
        except Exception:
            return "unknown"
        return project_slug or "unknown"

    def _remote_recall(
        self,
        prompt: str,
        cwd: str,
        config: dict,
        session_id: str,
        concrete: bool,
        host_id: str | None,
    ) -> tuple[RetrievalDecision | None, object, list[dict]]:
        if self.remote_search_envelope is None:
            return None, None, []
        if should_skip_recall(prompt, config, concrete=concrete):
            reason = "broad-project-query" if is_broad_project_history_query(prompt) else "skipped-prompt"
            return None, reason, []

        max_notes = config.get("recall_max_notes", 3)
        min_score = config.get("recall_min_score", 0.25)
        envelope = self.remote_search_envelope(
            query=prompt, limit=max_notes + 3, min_score=min_score, cwd=cwd, concrete=concrete
        )
        raw_results = envelope.get("results", [])
        if concrete:
            results = raw_results
            project_decisions = []
        else:
            results, project_decisions = filter_recall_results_by_explicit_project(prompt, raw_results)
        if not results:
            if project_decisions:
                reason = "project-mismatch-filtered-empty"
            elif isinstance(envelope.get("miss"), dict):
                reason = envelope["miss"].get("reason") or "no-results"
                reason = StructuredMissReason(reason, envelope["miss"])
            else:
                reason = "no-results"
            return None, reason, project_decisions

        recent = self.recently_injected_paths(session_id, cwd=cwd, host_id=host_id)
        if recent:
            fresh = [result for result in results if result.get("path", "") not in recent]
            if not fresh:
                return None, "duplicate", project_decisions
            results = fresh
        top_path = results[0].get("path", "")
        injected_results = results[:max_notes]
        lines = ["[vault] Related memories:", *[_format_result(result) for result in injected_results]]
        return (
            RetrievalDecision(
                True,
                "recall",
                lines=lines,
                results=injected_results,
                top_path=top_path,
                metadata={"top_path": top_path},
            ),
            None,
            project_decisions,
        )

    def run(self, request: PromptRecallRequest) -> RetrievalDecision:
        config = self.config_loader()
        prompt = request.prompt or ""
        project_slug = self._project_slug(request.cwd)
        concrete_enabled, concrete_auto_selected = resolve_concrete_mode(
            config.get("recall_concrete_mode", False), prompt
        )

        self._log_diagnostic(
            config,
            "start",
            prompt_len=len(prompt),
            cwd=request.cwd,
            session_id=request.session_id,
            project_slug=project_slug,
            signal_terms=recall_signal_terms(prompt),
            low_signal=is_low_signal_recall_prompt(prompt),
            concrete_mode=config.get("recall_concrete_mode", False),
            concrete_enabled=concrete_enabled,
            concrete_auto_selected=concrete_auto_selected,
        )

        if not config.get("prompt_recall", True):
            self._log_diagnostic(config, "decision", decision="skipped", reason="disabled")
            return _empty_decision("recall", "disabled", query=prompt)
        if not prompt:
            self._log_diagnostic(config, "decision", decision="skipped", reason="empty-prompt")
            return _empty_decision("recall", "empty-prompt", query=prompt)

        self.bump_prompts_since(
            request.session_id,
            cwd=request.cwd,
            project_slug=project_slug,
            host_id=request.host_id,
        )

        if should_skip_recall(prompt, config, concrete=concrete_enabled):
            if (
                not concrete_enabled
                and config.get("recall_skip_low_signal", True)
                and is_low_signal_recall_prompt(prompt)
            ):
                reason = "low-signal-prompt"
            elif (
                not concrete_enabled
                and config.get("recall_skip_broad_project_queries", True)
                and is_broad_project_history_query(prompt)
            ):
                reason = "broad-project-query"
            else:
                reason = "skipped-prompt"
            self.log_retrieval("recall", reason, query=prompt, cwd=request.cwd, session_id=request.session_id)
            self._log_diagnostic(
                config,
                "skip",
                reason=reason,
                normalized_prompt=re.sub(r"\s+", " ", prompt).strip(),
                broad_project_query=is_broad_project_history_query(prompt),
                concrete_enabled=concrete_enabled,
                concrete_auto_selected=concrete_auto_selected,
            )
            self._log_diagnostic(config, "decision", decision="skipped", reason=reason)
            return _empty_decision("recall", reason, query=prompt)

        fallback_remote_reason = None
        if self.remote_available() and prompt:
            try:
                remote_decision, remote_reason, project_decisions = self._remote_recall(
                    prompt,
                    request.cwd,
                    config,
                    request.session_id,
                    concrete_enabled,
                    request.host_id,
                )
                if project_decisions and config.get("recall_diagnostics_include_candidates", False):
                    self._log_diagnostic(
                        config,
                        "candidates",
                        stage="remote-project-filter",
                        candidates=project_decisions,
                        query=prompt,
                    )
                if remote_decision and remote_decision.lines:
                    self._log_diagnostic(
                        config,
                        "decision",
                        decision="injected",
                        source="remote",
                        top_path=remote_decision.top_path,
                    )
                    return remote_decision
                if remote_reason in ("duplicate", "project-mismatch-filtered-empty"):
                    self._log_diagnostic(config, "decision", decision="skipped", source="remote", reason=remote_reason)
                    return _empty_decision("recall", str(remote_reason), query=prompt)
                if remote_reason and remote_reason != "no-results":
                    fallback_remote_reason = remote_reason
            except Exception as exc:
                print(f"[memento] remote vault unreachable, using local only ({exc})", file=sys.stderr)

        vault = self.vault_loader()
        if not vault.exists() or not (vault / "notes").exists():
            reason = (
                fallback_remote_reason
                if isinstance(fallback_remote_reason, StructuredMissReason)
                else normalize_miss_reason(fallback_remote_reason or "empty_vault", prompt)
            )
            self._log_diagnostic(config, "decision", decision="skipped", reason=str(reason))
            return _empty_decision("recall", str(reason), query=prompt)
        if not self.has_backend():
            reason = (
                fallback_remote_reason
                if isinstance(fallback_remote_reason, StructuredMissReason)
                else normalize_miss_reason(fallback_remote_reason or "backend_unavailable", prompt)
            )
            self._log_diagnostic(config, "decision", decision="skipped", reason=str(reason))
            remote_miss = getattr(reason, "miss", None)
            decision = _empty_decision("recall", str(reason), query=prompt)
            if isinstance(remote_miss, dict):
                decision.reason = reason
                decision.metadata["miss"] = remote_miss
            return decision

        min_score = config.get("recall_min_score", 0.25)
        max_notes = config.get("recall_max_notes", 3)
        confidence_margin_threshold = config.get("recall_confidence_margin", DEFAULT_RECALL_CONFIDENCE_MARGIN)
        query = prompt
        appended_project = False
        if (
            request.cwd
            and should_append_project_to_recall(prompt, concrete=concrete_enabled)
            and project_slug != "unknown"
        ):
            query = f"{prompt} {project_slug.replace('-', ' ')}"
            appended_project = True
        self._log_diagnostic(
            config,
            "query",
            original_prompt=prompt,
            final_query=query,
            appended_project=appended_project,
            project_slug=project_slug,
        )

        search_limit = max_notes + 4
        threshold_probe_found = False
        threshold_probe_checked = False
        t0 = self.now()
        results = self.qmd_search(
            query,
            limit=search_limit,
            semantic=False,
            timeout=5,
            min_score=min_score,
            concrete=concrete_enabled,
        )
        latency_ms = int((self.now() - t0) * 1000)
        pipeline_depth = "concrete" if concrete_enabled else "bm25"
        self._log_candidates(config, results, "bm25", query=query)

        if not results and min_score > 0:
            threshold_probe_checked = True
            threshold_probe_found = bool(
                self.qmd_search(
                    query,
                    limit=1,
                    semantic=False,
                    timeout=5,
                    min_score=0.0,
                    concrete=concrete_enabled,
                )
            )

        lexical_variant_matched = False
        single_strong_hit = len(results) == 1 and float(results[0].get("score", 0) or 0) >= float(
            config.get("recall_high_confidence", 0.55) or 0.55
        )
        if not concrete_enabled and not threshold_probe_found:
            variant_query = normalized_natural_query(query)
            should_try_variant = (
                variant_query
                and variant_query != query
                and (
                    not results or (not single_strong_hit and confidence_margin(results) < confidence_margin_threshold)
                )
            )
            if should_try_variant:
                variant_results = self.qmd_search(
                    variant_query,
                    limit=search_limit,
                    semantic=False,
                    timeout=5,
                    min_score=min_score,
                )
                if variant_results:
                    existing = {result["path"] for result in results}
                    appended_any = False
                    for result in variant_results:
                        if result["path"] not in existing:
                            results.append(result)
                            existing.add(result["path"])
                            appended_any = True
                    results.sort(key=lambda result: result["score"], reverse=True)
                    if appended_any:
                        lexical_variant_matched = True
                        pipeline_depth = "bm25+query_terms"
                    self._log_candidates(config, results, "query-terms", query=variant_query)

        # Confident is primarily a relative rank-1-vs-rank-2 gap, not an
        # absolute score threshold (see confidence_margin() docstring for why).
        # A single strong lexical hit still counts as confident enough to avoid
        # redundant lexical fallback work, while a single weak hit remains
        # non-confident so deep recall can investigate.
        confident = (
            single_strong_hit or lexical_variant_matched or confidence_margin(results) >= confidence_margin_threshold
        )

        if not concrete_enabled and not confident:
            expanded_query = self.prf_expand(query, config=config, initial_results=results)
            if expanded_query != query:
                prf_results = self.qmd_search(
                    expanded_query,
                    limit=search_limit,
                    semantic=False,
                    timeout=5,
                    min_score=min_score,
                )
                if prf_results:
                    existing = {result["path"] for result in results}
                    for result in prf_results:
                        if result["path"] not in existing:
                            results.append(result)
                            existing.add(result["path"])
                    results.sort(key=lambda result: result["score"], reverse=True)
                    pipeline_depth = "prf"
                    self._log_candidates(config, results, "prf", query=expanded_query)

            if results and config.get("rrf_enabled", True) and self.semantic_warm():
                vec_results = self.qmd_search(
                    query,
                    limit=search_limit,
                    semantic=True,
                    timeout=5,
                    min_score=min_score,
                )
                if vec_results:
                    results = self.fuse_results([results, vec_results], k=config.get("rrf_k", 60))
                    pipeline_depth = "rrf"
                    self._log_candidates(config, results, "rrf", query=query)

        results_before = len(results)
        project_decisions = []
        project_filter_applied = False
        multi_hop_gate = False
        multi_hop_added = 0
        deep_recall_spawned = False

        if not concrete_enabled:
            if config.get("concept_index_enabled", True):
                try:
                    concept_hits = self.concept_lookup(prompt)
                    if concept_hits:
                        existing_paths = {result.get("path", "") for result in results}
                        merged_any = False
                        for hit in concept_hits:
                            if hit["path"] in existing_paths:
                                continue
                            # Same recall_min_score contract BM25/PRF/RRF results are
                            # held to -- a concept hit whose floored score still can't
                            # clear the bar is exactly the kind of low-relevance match
                            # the confidence gate exists to keep out (MEM-141).
                            hit["score"] = max(hit.get("score", 0), config.get("concept_index_score", 0.5))
                            if hit["score"] < min_score:
                                continue
                            results.append(hit)
                            existing_paths.add(hit["path"])
                            merged_any = True
                        if merged_any:
                            # Concept hits are merged by lookup order, not by score, so
                            # the combined list must be re-sorted before anything downstream
                            # (quality signals, final [:max_notes] truncation) treats list
                            # position as a relevance ranking (MEM-141).
                            results.sort(key=lambda result: result.get("score", 0), reverse=True)
                        self._log_candidates(config, results, "concept-index", query=query)
                except Exception:
                    pass

            multi_hop_gate = not confident and config.get("multi_hop_enabled", False)
            if multi_hop_gate and results:
                try:
                    pre_hop_count = len(results)
                    results = self.multi_hop(prompt, results, config=config)
                    multi_hop_added = len(results) - pre_hop_count
                    pipeline_depth += "+hop"
                    self._log_candidates(config, results, "multi-hop", query=query)
                except Exception:
                    pass

            if (
                not confident
                and config.get("deep_recall_enabled", False)
                and results
                and not self.deep_recall_pending_exists()
            ):
                try:
                    self.spawn_deep_recall(prompt, results, config)
                    deep_recall_spawned = True
                    pipeline_depth += "+deep"
                except Exception:
                    pass

        if not results:
            if min_score > 0:
                threshold_probe = []
                if not threshold_probe_checked:
                    threshold_probe = self.qmd_search(
                        query,
                        limit=1,
                        semantic=False,
                        timeout=5,
                        min_score=0.0,
                    )
                if threshold_probe_found or threshold_probe:
                    self.log_retrieval(
                        "recall",
                        "threshold_too_high",
                        query=query,
                        min_score=min_score,
                        latency_ms=latency_ms,
                        pipeline=pipeline_depth,
                    )
                    self._log_diagnostic(
                        config,
                        "decision",
                        decision="skipped",
                        reason="threshold_too_high",
                        min_score=min_score,
                        latency_ms=latency_ms,
                    )
                    return _empty_decision(
                        "recall", "threshold_too_high", query=prompt, details={"min_score": min_score}
                    )
            miss_reason = (
                fallback_remote_reason
                if isinstance(fallback_remote_reason, StructuredMissReason)
                else normalize_miss_reason(fallback_remote_reason or "no-results", prompt)
            )
            self.log_retrieval("recall", str(miss_reason), query=query, latency_ms=latency_ms, pipeline=pipeline_depth)
            self._log_diagnostic(config, "decision", decision="skipped", reason=str(miss_reason), latency_ms=latency_ms)
            decision = _empty_decision("recall", str(miss_reason), query=prompt)
            remote_miss = getattr(miss_reason, "miss", None)
            if isinstance(remote_miss, dict):
                decision.reason = miss_reason
                decision.metadata["miss"] = remote_miss
            return decision

        if not concrete_enabled:
            results = self.enhance_results(results, config=config, cwd=request.cwd)
            self._log_candidates(config, results, "enhanced", query=query)

            results, project_decisions = filter_recall_results_by_explicit_project(prompt, results)
            project_filter_applied = bool(project_decisions)
            if project_decisions and config.get("recall_diagnostics_include_candidates", False):
                self._log_diagnostic(
                    config, "candidates", stage="project-filter", candidates=project_decisions, query=query
                )

            if not confident and config.get("reranker_enabled", True) and len(results) > 1:
                try:
                    if self.rerank_results is None:
                        from tenet_reranker import rerank

                        results = rerank(prompt, results, config)
                    else:
                        results = self.rerank_results(prompt, results, config)
                    pipeline_depth += "+ce"
                    self._log_candidates(config, results, "reranked", query=query)
                except Exception:
                    pass

        if not results:
            reason = "project-mismatch-filtered-empty" if project_filter_applied else "filtered-empty"
            self.log_retrieval("recall", reason, query=query, results_before=results_before, latency_ms=latency_ms)
            self._log_diagnostic(config, "decision", decision="skipped", reason=reason, latency_ms=latency_ms)
            return _empty_decision("recall", reason, query=prompt)

        recent = self.recently_injected_paths(
            request.session_id,
            cwd=request.cwd,
            project_slug=project_slug,
            host_id=request.host_id,
        )
        if recent:
            fresh = [result for result in results if result.get("path", "") not in recent]
            if not fresh:
                self.log_retrieval("recall", "dedup-skip", query=query)
                self._log_diagnostic(
                    config,
                    "decision",
                    decision="skipped",
                    reason="duplicate",
                    top_path=results[0].get("path", ""),
                )
                return _empty_decision("recall", "duplicate", query=prompt)
            results = fresh

        selected = results[:max_notes]
        lines = ["[vault] Related memories:", *[_format_result(result) for result in selected]]
        top_path = selected[0].get("path", "")
        injected_text = "\n".join(lines)
        injected_titles = [result.get("title", "") for result in selected]
        self.log_retrieval(
            "recall",
            "inject",
            query=query,
            latency_ms=latency_ms,
            results_before=results_before,
            results_after=len(results),
            injected_titles=injected_titles,
            injected_chars=len(injected_text),
            pipeline=pipeline_depth,
            multi_hop_gate=multi_hop_gate,
            multi_hop_added=multi_hop_added,
            deep_recall_spawned=deep_recall_spawned,
        )
        self._log_diagnostic(
            config,
            "decision",
            decision="injected",
            injected_titles=injected_titles,
            injected_chars=len(injected_text),
            latency_ms=latency_ms,
            top_path=top_path,
            pipeline=pipeline_depth,
        )
        return RetrievalDecision(
            True,
            "recall",
            lines=lines,
            results=selected,
            top_path=top_path,
            metadata={"cwd": request.cwd, "session_id": request.session_id, "top_path": top_path},
        )
