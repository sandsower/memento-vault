#!/usr/bin/env python3
"""
Inception — background consolidation agent for memento-vault.
Clusters vault notes by embedding similarity and produces pattern notes.
"""

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

# Add hooks dir to path for memento_utils import
sys.path.insert(0, str(Path(__file__).parent))
from memento_utils import (
    get_config,
    slugify,
    load_inception_state,
    save_inception_state,
    acquire_inception_lock,
    release_inception_lock,
    INCEPTION_STATE_PATH,
)


@dataclass
class NoteRecord:
    stem: str
    path: Path
    title: str
    note_type: str
    tags: list
    date: str
    certainty: int | None = None
    project: str | None = None
    source: str | None = None
    synthesized_from: list = field(default_factory=list)
    body: str = ""
    wikilinks: list = field(default_factory=list)


def parse_note(path: Path) -> NoteRecord | None:
    """Parse a note file into a NoteRecord. Returns None on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    # Parse frontmatter
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return NoteRecord(
            stem=path.stem,
            path=path,
            title=path.stem,
            note_type="unknown",
            tags=[],
            date="",
            body=text,
        )

    fm_end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            fm_end = i
            break

    if fm_end is None:
        return NoteRecord(
            stem=path.stem,
            path=path,
            title=path.stem,
            note_type="unknown",
            tags=[],
            date="",
            body=text,
        )

    fm_lines = lines[1:fm_end]
    body = "\n".join(lines[fm_end + 1 :]).strip()

    # Simple frontmatter parser
    meta = {}
    current_key = None
    current_list = None
    for line in fm_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_key:
            if current_list is None:
                current_list = []
            val = stripped[2:].strip().strip("'\"")
            # Strip wikilink syntax
            if val.startswith("[[") and val.endswith("]]"):
                val = val[2:-2]
            current_list.append(val)
            meta[current_key] = current_list
            continue

        if ":" in stripped:
            current_list = None
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key

            # Handle inline list: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                items = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
                meta[key] = items
            elif val:
                meta[key] = val
            # If val is empty, might be a multi-line list (handled by "- " above)

    # Extract wikilinks from body
    wikilinks = re.findall(r"\[\[([^\]]+)\]\]", body)

    # Parse certainty as int
    certainty = None
    if meta.get("certainty"):
        try:
            certainty = int(meta["certainty"])
        except (ValueError, TypeError):
            pass

    return NoteRecord(
        stem=path.stem,
        path=path,
        title=meta.get("title", path.stem),
        note_type=meta.get("type", "unknown"),
        tags=meta.get("tags", []),
        date=meta.get("date", ""),
        certainty=certainty,
        project=meta.get("project"),
        source=meta.get("source"),
        synthesized_from=meta.get("synthesized_from", []),
        body=body,
        wikilinks=wikilinks,
    )


def collect_eligible_notes(config, state, full=False):
    """Collect notes eligible for Inception clustering.

    Filters:
    - Only notes/*.md (no archive, fleeting, projects)
    - Skip notes with source: inception (prevent recursion)
    - Skip notes with excluded tags
    - Skip already-processed notes (unless full=True)
    """
    vault = Path(config["vault_path"])
    notes_dir = vault / "notes"

    if not notes_dir.exists():
        return []

    exclude_tags = set(config.get("inception_exclude_tags", []))
    processed = set(state.get("processed_notes", []))
    last_run = state.get("last_run_iso")

    notes = []
    for md_file in sorted(notes_dir.glob("*.md")):
        # Skip dotfiles (temp files from atomic writes)
        if md_file.name.startswith("."):
            continue

        record = parse_note(md_file)
        if record is None:
            continue

        # Skip inception-generated notes (prevent recursion)
        if record.source == "inception":
            continue

        # Skip notes with excluded tags
        if exclude_tags and exclude_tags.intersection(record.tags):
            continue

        # Skip already processed (unless full backfill)
        if not full and record.stem in processed:
            continue

        # For incremental runs, only include notes newer than last run
        if not full and last_run and record.date:
            try:
                note_dt = datetime.fromisoformat(record.date)
                last_dt = datetime.fromisoformat(last_run)
                if note_dt <= last_dt:
                    continue
            except ValueError:
                pass  # include notes with unparseable dates

        notes.append(record)

    return notes


def load_embeddings(note_stems, db_path=None, collection="memento"):
    """Load document-level embeddings from QMD's SQLite database.

    QMD stores chunk-level 768-dim float32 embeddings. This function
    mean-pools chunks into a single vector per document.

    Args:
        note_stems: list of note filename stems (e.g. ["redis-cache-ttl"])
        db_path: path to QMD SQLite database. Default: ~/.cache/qmd/index.sqlite
        collection: QMD collection name

    Returns:
        dict mapping stem -> np.ndarray (768-dim, L2-normalized)

    Notes without embeddings are silently skipped.
    """
    if db_path is None:
        db_path = Path.home() / ".cache" / "qmd" / "index.sqlite"

    if not Path(db_path).exists():
        return {}

    conn = sqlite3.connect(str(db_path))
    try:
        # Build path -> stem mapping (QMD stores relative paths, not URIs)
        path_to_stem = {}
        for stem in note_stems:
            path_to_stem[f"notes/{stem}.md"] = stem

        if not path_to_stem:
            return {}

        # Step 1: Get document hashes for our notes
        placeholders = ",".join("?" * len(path_to_stem))
        doc_rows = conn.execute(
            f"SELECT path, hash FROM documents WHERE collection = ? AND path IN ({placeholders})",
            [collection] + list(path_to_stem.keys()),
        ).fetchall()

        if not doc_rows:
            return {}

        hash_to_stem = {}
        for path, doc_hash in doc_rows:
            stem = path_to_stem.get(path)
            if stem:
                hash_to_stem.setdefault(doc_hash, stem)

        # Step 2: Get chunk info from content_vectors
        hash_placeholders = ",".join("?" * len(hash_to_stem))
        cv_rows = conn.execute(
            f"SELECT hash, seq FROM content_vectors WHERE hash IN ({hash_placeholders})",
            list(hash_to_stem.keys()),
        ).fetchall()

        if not cv_rows:
            return {}

        # Step 3: Look up vector locations in chunk storage
        # vec0 stores vectors in chunks; vectors_vec_rowids maps id -> (chunk_id, chunk_offset)
        # The id format is "{hash}_{seq}" (underscore separator)
        vec_ids = [f"{h}_{s}" for h, s in cv_rows]
        hash_seq_to_stem = {f"{h}_{s}": hash_to_stem[h] for h, s in cv_rows if h in hash_to_stem}

        # Batch lookup chunk locations
        vid_placeholders = ",".join("?" * len(vec_ids))
        rowid_rows = conn.execute(
            f"SELECT id, chunk_id, chunk_offset FROM vectors_vec_rowids WHERE id IN ({vid_placeholders})",
            vec_ids,
        ).fetchall()

        if not rowid_rows:
            return {}

        # Step 4: Group by chunk_id for efficient blob reads
        chunk_reads = {}  # chunk_id -> [(vec_id, offset)]
        for vec_id, chunk_id, chunk_offset in rowid_rows:
            chunk_reads.setdefault(chunk_id, []).append((vec_id, chunk_offset))

        # Step 5: Read chunk blobs and extract individual vectors
        # Each chunk stores up to 1024 vectors of dim floats (768 * 4 bytes each)
        dim = 768
        vec_size = dim * 4  # float32

        doc_chunks = {}
        for chunk_id, entries in chunk_reads.items():
            blob_row = conn.execute(
                "SELECT vectors FROM vectors_vec_vector_chunks00 WHERE rowid = ?",
                (chunk_id,),
            ).fetchone()
            if not blob_row or not blob_row[0]:
                continue
            blob = blob_row[0]
            for vec_id, chunk_offset in entries:
                start = chunk_offset * vec_size
                end = start + vec_size
                if end > len(blob):
                    continue
                vec = np.frombuffer(blob[start:end], dtype=np.float32).copy()
                stem = hash_seq_to_stem.get(vec_id)
                if stem:
                    doc_chunks.setdefault(stem, []).append(vec)

        # Mean-pool and normalize
        result = {}
        for stem, chunks in doc_chunks.items():
            mean_vec = np.mean(chunks, axis=0)
            norm = np.linalg.norm(mean_vec)
            if norm > 0:
                mean_vec = mean_vec / norm
            result[stem] = mean_vec

        return result
    finally:
        conn.close()


def score_cluster(stems, notes_dict):
    """Score a cluster for synthesis priority. Higher = more interesting.

    Components:
      1. Size bonus: log2(n) -- diminishing returns past 8 notes
      2. Tag diversity: unique_tags / total_tag_mentions
      3. Temporal spread: days between earliest and latest note / 30, capped at 1.0
      4. Project diversity: 0.5 bonus if notes span 2+ projects
      5. Mean certainty: normalized to 0-1 (divided by 5)

    Weights: size*1.0, diversity*0.8, temporal*0.6, project*0.5, certainty*0.3
    """
    records = [notes_dict[s] for s in stems if s in notes_dict]
    if not records:
        return 0.0

    # 1. Size (log scale)
    size_score = math.log2(len(records))

    # 2. Tag diversity
    all_tags = []
    for r in records:
        all_tags.extend(r.tags)
    unique_tags = len(set(all_tags))
    tag_diversity = unique_tags / max(len(all_tags), 1)

    # 3. Temporal spread (days / 30, capped at 1.0)
    dates = []
    for r in records:
        if r.date:
            try:
                dates.append(datetime.fromisoformat(r.date))
            except ValueError:
                pass
    if len(dates) >= 2:
        spread_days = (max(dates) - min(dates)).days
        temporal_score = min(spread_days / 30.0, 1.0)
    else:
        temporal_score = 0.0

    # 4. Project diversity
    projects = set(r.project for r in records if r.project)
    project_bonus = 0.5 if len(projects) >= 2 else 0.0

    # 5. Mean certainty (normalized to 0-1)
    certainties = [r.certainty for r in records if r.certainty is not None]
    certainty_score = (sum(certainties) / len(certainties) / 5.0) if certainties else 0.5

    return size_score * 1.0 + tag_diversity * 0.8 + temporal_score * 0.6 + project_bonus * 0.5 + certainty_score * 0.3


def build_synthesis_prompt(cluster_stems, notes_dict, merge_target=None):
    """Build a prompt for the LLM to synthesize a pattern note from a cluster.

    Args:
        cluster_stems: list of note stems in the cluster
        notes_dict: dict mapping stem -> NoteRecord
        merge_target: if set, the stem of an existing pattern note to update

    Returns:
        str: the full prompt to send to codex/claude
    """
    system = (
        "You are the Inception, a consolidation agent for a personal knowledge vault. "
        "You receive a cluster of related atomic notes from different sessions and produce "
        "a single pattern note that captures the higher-order insight connecting them.\n\n"
        "Rules:\n"
        "- If the connection is trivial, obvious, or just 'these are about the same topic,' respond with exactly: SKIP\n"
        "- The pattern note should capture a recurring approach, common root cause, or cross-cutting insight "
        "that is NOT obvious from any single source note alone.\n"
        "- Write 2-5 sentences for the body. Be specific and concrete.\n"
        "- Return your response as a JSON object (no markdown fencing) with these fields:\n"
        "  - title: string (concise pattern title)\n"
        "  - body: string (the 2-5 sentence synthesis)\n"
        "  - tags: list of strings (union of relevant source tags, plus any new cross-cutting tags)\n"
        "  - certainty: int 1-5 (your confidence this is a real pattern)\n"
        "  - related: list of strings (ONLY use the exact source note stems provided below, never invent note names)\n"
    )

    source_blocks = []
    for stem in cluster_stems:
        note = notes_dict.get(stem)
        if note is None:
            continue
        block = (
            f"### {note.title}\n"
            f"Type: {note.note_type} | Tags: {', '.join(note.tags)} | "
            f"Date: {note.date} | Certainty: {note.certainty or '?'} | "
            f"Project: {note.project or 'none'}\n\n"
            f"{note.body.strip()}"
        )
        source_blocks.append(block)

    user_content = system + "\n---\n\nHere are the clustered source notes:\n\n" + "\n\n---\n\n".join(source_blocks)

    if merge_target:
        user_content += (
            f"\n\n---\n\nAn existing pattern note already covers a subset of these sources: "
            f"[[{merge_target}]]. Revise and expand it to incorporate the new sources. "
            f"Keep the existing insight but add what the new notes contribute."
        )

    return user_content


def build_synthesized_from_ledger(notes_dir):
    """Scan existing source:inception notes and build a ledger of what's been consolidated.

    Returns:
        dict mapping pattern_note_stem -> set of source stems
    """
    ledger = {}
    notes_path = Path(notes_dir)
    if not notes_path.exists():
        return ledger

    for md_file in notes_path.glob("*.md"):
        record = parse_note(md_file)
        if record is None:
            continue
        if record.source != "inception":
            continue
        if record.synthesized_from:
            ledger[record.stem] = set(record.synthesized_from)

    return ledger


def check_ledger_dedup(cluster_stems, ledger):
    """Check if a cluster is already covered by existing pattern notes.

    Returns:
        ("skip", None) — cluster already fully covered
        ("create", None) — cluster is novel, create new pattern note
        ("merge", existing_stem) — cluster is a superset of existing, update it
    """
    cluster_set = set(cluster_stems)

    for pattern_stem, source_set in ledger.items():
        # Exact match or cluster is a subset — already covered
        if cluster_set == source_set or cluster_set.issubset(source_set):
            return ("skip", None)
        # Cluster is a superset — merge into existing
        if source_set.issubset(cluster_set) and len(cluster_set) > len(source_set):
            return ("merge", pattern_stem)

    return ("create", None)


def check_title_overlap(slug, existing_stems):
    """Check if a slugified title overlaps too much with existing note stems.

    Returns True if overlap > 0.80 with any existing stem.
    """
    slug_tokens = set(slug.split("-"))
    if not slug_tokens:
        return False

    for existing in existing_stems:
        existing_tokens = set(existing.split("-"))
        if not existing_tokens:
            continue
        intersection = slug_tokens & existing_tokens
        overlap = len(intersection) / min(len(slug_tokens), len(existing_tokens))
        if overlap > 0.80:
            return True

    return False


def cluster_notes(embedding_matrix, stem_index, config):
    """Cluster notes using HDBSCAN on their embedding vectors.

    Args:
        embedding_matrix: np.ndarray of shape (N, D) -- one row per note
        stem_index: list of stem names, parallel to embedding_matrix rows
        config: dict with inception_min_cluster_size, inception_cluster_threshold, inception_max_clusters

    Returns:
        dict mapping cluster_id -> list of stems
        Noise points (label -1) are excluded.
        Only clusters with >= min_cluster_size members are returned.
        Sorted by cluster size descending, limited to max_clusters.
    """
    import hdbscan

    min_cluster_size = config.get("inception_min_cluster_size", 3)
    max_clusters = config.get("inception_max_clusters", 10)

    if len(stem_index) < min_cluster_size:
        return {}

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=2,
        metric="euclidean",  # on normalized vectors, euclidean is proportional to cosine
        cluster_selection_method="leaf",
    )

    labels = clusterer.fit_predict(embedding_matrix)

    # Group stems by cluster label, excluding noise (-1)
    clusters = {}
    for i, label in enumerate(labels):
        if label == -1:
            continue
        clusters.setdefault(int(label), []).append(stem_index[i])

    # Filter by min size and sort by size descending
    clusters = {cid: stems for cid, stems in clusters.items() if len(stems) >= min_cluster_size}

    # Sort by size descending, limit to max_clusters
    sorted_clusters = dict(sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)[:max_clusters])

    return sorted_clusters


def write_pattern_note(synthesis, cluster_stems, vault_path):
    """Write a pattern note to the vault using atomic write.

    Args:
        synthesis: dict with keys: title, body, tags, certainty, related
        cluster_stems: list of source note stems
        vault_path: Path to vault root

    Returns:
        Path to the written note, or None if write failed
    """
    notes_dir = Path(vault_path) / "notes"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # Build slug, handle collisions
    base_slug = slugify(synthesis["title"])
    slug = base_slug
    counter = 2
    while (notes_dir / f"{slug}.md").exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    # Build frontmatter
    tags_str = "[" + ", ".join(synthesis.get("tags", [])) + "]"
    synth_lines = "\n".join(f"  - {s}" for s in cluster_stems)

    content = f"""---
title: {synthesis["title"]}
type: pattern
tags: {tags_str}
source: inception
certainty: {min(synthesis.get("certainty", 3), 3)}
synthesized_from:
{synth_lines}
date: {now}
---

{synthesis["body"].strip()}

## Related

"""
    # Add wikilinks to related notes (only existing notes, never hallucinated names)
    existing_stems = {f.stem for f in notes_dir.glob("*.md")}
    related = synthesis.get("related", cluster_stems)
    seen_links = set()
    for r in related:
        if r in existing_stems and r not in seen_links:
            content += f"- [[{r}]]\n"
            seen_links.add(r)
    # Always include source stems even if LLM forgot them
    for s in cluster_stems:
        if s not in seen_links:
            content += f"- [[{s}]]\n"

    # Atomic write: temp file then rename
    target = notes_dir / f"{slug}.md"
    tmp = notes_dir / f".inception-tmp-{slug}.md"
    try:
        tmp.write_text(content)
        os.replace(str(tmp), str(target))
        return target
    except OSError:
        # Clean up temp file on failure
        try:
            tmp.unlink()
        except OSError:
            pass
        return None


def backlink_sources(pattern_stem, source_stems, vault_path):
    """Append [[pattern_stem]] to each source note's ## Related section.

    Creates ## Related section if missing. Skips if link already exists.
    """
    notes_dir = Path(vault_path) / "notes"
    link = f"[[{pattern_stem}]]"

    for stem in source_stems:
        note_path = notes_dir / f"{stem}.md"
        if not note_path.exists():
            continue

        text = note_path.read_text()

        # Skip if link already present
        if link in text:
            continue

        # Find ## Related section
        if "## Related" in text:
            # Append after the last line in the Related section
            text = text.rstrip() + f"\n- {link}\n"
        else:
            # Create Related section at the end
            text = text.rstrip() + f"\n\n## Related\n\n- {link}\n"

        note_path.write_text(text)


def call_llm(prompt, config):
    """Call the LLM backend to synthesize a pattern note.

    Args:
        prompt: the full prompt string
        config: dict with inception_backend ("codex" or "claude")

    Returns:
        str: raw LLM response text, or empty string on failure
    """
    backend = config.get("inception_backend", "codex")

    import tempfile

    if backend == "codex":
        # codex exec writes the last message to a file via -o
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            out_path = tmp.name
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "-o",
            out_path,
            prompt,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            result = Path(out_path).read_text().strip()
            return result
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    else:
        model = config.get("inception_model", "haiku")
        cmd = [
            "claude",
            "--print",
            "--model",
            model,
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "-p",
            prompt,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""


def parse_synthesis(raw):
    """Parse the LLM's response into a synthesis dict.

    Returns:
        dict with keys: title, body, tags, certainty, related
        or None if SKIP or malformed
    """
    if not raw:
        return None

    stripped = raw.strip()

    # Check for SKIP response
    if stripped == "SKIP" or stripped.startswith("SKIP"):
        return None

    # Strip markdown code fences if present
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        stripped = "\n".join(lines)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None

    # Validate required fields
    if not isinstance(data, dict) or "title" not in data or "body" not in data:
        return None

    return {
        "title": data["title"],
        "body": data["body"],
        "tags": data.get("tags", []),
        "certainty": data.get("certainty", 3),
        "related": data.get("related", []),
    }


# --- Main Pipeline ---

import argparse  # noqa: E402


def check_dependencies():
    """Check that required ML packages are installed.

    Returns list of missing package names, or empty list if all present.
    """
    missing = []
    for pkg in ("numpy", "hdbscan", "sklearn"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


def parse_args(argv=None):
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Inception — background consolidation agent for memento-vault",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log clusters and proposed notes, write nothing")
    parser.add_argument("--full", action="store_true", help="Process all notes, ignoring threshold and processed list")
    parser.add_argument("--max-clusters", type=int, default=None, help="Override inception_max_clusters config")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    return parser.parse_args(argv)


def main(args=None, state_path=None, db_path=None, lock_path=None):
    """Run the Inception pipeline.

    Returns exit code: 0=success, 1=locked, 2=missing deps, 3=embedding failure, 5=config error.
    """
    if args is None:
        args = parse_args()

    config = get_config()

    # Gate: inception must be enabled (unless --full forces it)
    if not config.get("inception_enabled", False) and not args.full:
        return 0

    # Check dependencies
    missing = check_dependencies()
    if missing:
        print(f"Missing: {', '.join(missing)}", file=sys.stderr)
        print("Install: pip install numpy hdbscan scikit-learn", file=sys.stderr)
        return 2

    # Acquire lock
    from memento_utils import RUNTIME_DIR as _rtdir

    _lock_path = lock_path or os.path.join(_rtdir, "inception.lock")
    if not acquire_inception_lock(lock_path=_lock_path):
        if args.verbose:
            print("Another Inception instance is running", file=sys.stderr)
        return 1

    try:
        _state_path = state_path or INCEPTION_STATE_PATH
        state = load_inception_state(state_path=_state_path)
        vault_path = Path(config["vault_path"])

        # Collect NEW notes (for tracking what changed since last run)
        new_notes = collect_eligible_notes(config, state, full=args.full)
        if args.verbose:
            print(f"New notes since last run: {len(new_notes)}", file=sys.stderr)

        if not new_notes:
            if args.verbose:
                print("No new notes to process", file=sys.stderr)
            return 0

        new_stems = {n.stem for n in new_notes}

        # Collect ALL clusterable notes (everything except inception-sourced)
        # This is the key difference from the old approach: we cluster the
        # full vault so cross-temporal patterns are detected.
        all_notes = collect_eligible_notes(config, state, full=True)
        if args.verbose:
            print(f"Total clusterable notes: {len(all_notes)}", file=sys.stderr)

        if len(all_notes) < config.get("inception_min_cluster_size", 3):
            if args.verbose:
                print("Not enough notes to cluster", file=sys.stderr)
            _update_state(state, _state_path, new_notes, 0, 0, args.dry_run)
            return 0

        # Build notes dict for lookups (all notes)
        notes_dict = {n.stem: n for n in all_notes}

        # Load embeddings for all notes
        collection = config.get("qmd_collection", "memento")
        embeddings = load_embeddings(
            list(notes_dict.keys()),
            db_path=db_path,
            collection=collection,
        )

        if not embeddings:
            if args.verbose:
                print("No embeddings found — is QMD indexed?", file=sys.stderr)
            return 3

        # Build matrix (all notes with embeddings)
        stem_index = [s for s in notes_dict if s in embeddings]
        if len(stem_index) < config.get("inception_min_cluster_size", 3):
            if args.verbose:
                print(f"Only {len(stem_index)} notes have embeddings", file=sys.stderr)
            return 0

        embedding_matrix = np.array([embeddings[s] for s in stem_index])

        # Cluster ALL notes
        max_clusters = args.max_clusters or config.get("inception_max_clusters", 10)
        cluster_config = dict(config)
        cluster_config["inception_max_clusters"] = max_clusters * 3  # over-fetch, filter below
        clusters = cluster_notes(embedding_matrix, stem_index, cluster_config)

        if args.verbose:
            print(f"Found {len(clusters)} total clusters", file=sys.stderr)

        if not clusters:
            _update_state(state, _state_path, new_notes, 0, 0, args.dry_run)
            return 0

        # Filter: only clusters containing at least 1 new note OR
        # clusters that overlap with existing patterns (refresh candidates)
        ledger = build_synthesized_from_ledger(vault_path / "notes")

        relevant_clusters = {}
        for cid, stems in clusters.items():
            has_new = any(s in new_stems for s in stems)
            # Check if this cluster is a superset of an existing pattern (refresh)
            action, merge_target = check_ledger_dedup(stems, ledger)
            is_refresh = action == "merge"

            if has_new or is_refresh:
                relevant_clusters[cid] = stems

        if args.verbose:
            print(f"Clusters with new notes or refresh candidates: {len(relevant_clusters)}", file=sys.stderr)

        if not relevant_clusters:
            _update_state(state, _state_path, new_notes, 0, 0, args.dry_run)
            return 0

        # Score, rank, and cap at max_clusters
        scored = []
        for cid, stems in relevant_clusters.items():
            score = score_cluster(stems, notes_dict)
            scored.append((cid, stems, score))
        scored.sort(key=lambda x: x[2], reverse=True)
        scored = scored[:max_clusters]

        all_existing_stems = [p.stem for p in (vault_path / "notes").glob("*.md")]

        notes_written = 0
        notes_refreshed = 0
        clusters_processed = 0
        written_pattern_paths = []

        # --- Phase 1: collect clusters that need LLM synthesis ---
        synthesis_queue = []  # (cid, stems, score, prompt, action, merge_target)

        for cid, stems, score in scored:
            clusters_processed += 1

            # Dedup check
            action, merge_target = check_ledger_dedup(stems, ledger)
            if action == "skip":
                if args.verbose:
                    print(f"  Cluster {cid}: skipped (already consolidated)", file=sys.stderr)
                continue

            if args.dry_run:
                label = " [refresh]" if action == "merge" else ""
                print(f"Cluster {cid} (score={score:.2f}){label}:", file=sys.stderr)
                for s in stems:
                    marker = " *NEW*" if s in new_stems else ""
                    title = notes_dict[s].title if s in notes_dict else s
                    print(f"  - {title}{marker}", file=sys.stderr)
                continue

            prompt = build_synthesis_prompt(stems, notes_dict, merge_target=merge_target)
            synthesis_queue.append((cid, stems, score, prompt, action, merge_target))

        # --- Phase 2: parallel LLM synthesis ---
        max_workers = config.get("inception_parallel", 4)
        llm_results = {}  # cid -> raw LLM response

        if synthesis_queue:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_cid = {executor.submit(call_llm, item[3], config): item[0] for item in synthesis_queue}
                for future in as_completed(future_to_cid):
                    cid = future_to_cid[future]
                    try:
                        llm_results[cid] = future.result()
                    except Exception:
                        llm_results[cid] = ""

        # --- Phase 3: sequential post-processing ---
        for cid, stems, score, prompt, action, merge_target in synthesis_queue:
            raw = llm_results.get(cid, "")
            synthesis = parse_synthesis(raw)

            if synthesis is None:
                if args.verbose:
                    print(f"  Cluster {cid}: SKIP (trivial or failed)", file=sys.stderr)
                continue

            # Title overlap dedup (skip for refresh — we're updating an existing note)
            if action != "merge":
                new_slug = slugify(synthesis["title"])
                if check_title_overlap(new_slug, all_existing_stems):
                    if args.verbose:
                        print(f"  Cluster {cid}: skipped (title overlap)", file=sys.stderr)
                    continue

            # Ensure related includes all source stems
            synthesis["related"] = list(set(synthesis.get("related", []) + stems))

            # Write (or refresh existing)
            note_path = write_pattern_note(synthesis, stems, vault_path)
            if note_path:
                if action == "merge":
                    notes_refreshed += 1
                    if args.verbose:
                        print(f"  Refreshed: {note_path.name}", file=sys.stderr)
                else:
                    notes_written += 1
                    if args.verbose:
                        print(f"  Wrote: {note_path.name}", file=sys.stderr)
                all_existing_stems.append(note_path.stem)

                # Backlink
                backlink_sources(note_path.stem, stems, vault_path)

                # Track for pre-reasoning
                written_pattern_paths.append(note_path)

        # Sleep-time pre-reasoning
        if not args.dry_run and written_pattern_paths:
            pattern_records = []
            for pp in written_pattern_paths:
                rec = parse_note(pp)
                if rec:
                    pattern_records.append(rec)
            if pattern_records:
                try:
                    qp, cp = pre_reason(pattern_records, notes_dict, config)
                    if args.verbose and qp:
                        print(f"Pre-reason: wrote {qp.name} and {cp.name}", file=sys.stderr)
                except Exception:
                    pass  # Non-fatal

        # Update state (only mark new notes as processed)
        _update_state(state, _state_path, new_notes, clusters_processed, notes_written, args.dry_run)

        # Commit and reindex
        total_changes = notes_written + notes_refreshed
        if total_changes > 0 and not args.dry_run:
            _commit_and_reindex(total_changes, config)

            try:
                maps = build_project_maps(vault_path)
                write_project_maps(maps)
            except Exception:
                pass  # Non-fatal

            # Build retrieval indexes for Tenet
            try:
                index = build_concept_index(vault_path)
                write_concept_index(index)
            except Exception:
                pass  # Non-fatal — retrieval degrades gracefully

        if args.verbose:
            parts = [f"{clusters_processed} clusters"]
            if notes_written:
                parts.append(f"{notes_written} new")
            if notes_refreshed:
                parts.append(f"{notes_refreshed} refreshed")
            if not notes_written and not notes_refreshed:
                parts.append("0 notes written")
            print(f"Done: {', '.join(parts)}", file=sys.stderr)

        return 0

    finally:
        release_inception_lock(lock_path=_lock_path)


def _update_state(state, state_path, notes, clusters_processed, notes_written, dry_run):
    """Update and save the Inception state file."""
    now = datetime.now().isoformat(timespec="seconds")
    state["last_run_iso"] = now
    state["last_run_note_count"] = len(notes)
    state.setdefault("runs", []).append(
        {
            "iso": now,
            "clusters_found": clusters_processed,
            "notes_written": notes_written,
            "dry_run": dry_run,
        }
    )
    state["processed_notes"] = list(set(state.get("processed_notes", []) + [n.stem for n in notes]))
    save_inception_state(state, state_path=state_path)


def build_project_maps(vault_path):
    """Scan all notes and group them by project field.

    Returns:
        dict mapping project_slug -> [{stem, title, certainty, date}, ...]
        Ranked by certainty desc then date desc, capped at 20 per project.
    """
    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.exists():
        return {}

    projects = {}  # project_path -> list of dicts
    for md_file in sorted(notes_dir.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        record = parse_note(md_file)
        if record is None or not record.project:
            continue
        slug = slugify(Path(record.project).name)
        if not slug:
            continue
        projects.setdefault(slug, []).append(
            {
                "stem": record.stem,
                "title": record.title,
                "certainty": record.certainty if record.certainty is not None else 2,
                "date": record.date or "",
            }
        )

    # Rank each project's notes: certainty desc, date desc
    for slug in projects:
        projects[slug].sort(key=lambda e: (e["certainty"], e["date"]), reverse=True)
        projects[slug] = projects[slug][:20]

    return projects


def write_project_maps(maps, config_dir=None):
    """Atomic write of project maps to config dir.

    JSON format: {"version": 1, "built_at": ISO, "maps": {slug: [...]}}
    """
    if config_dir is None:
        config_dir = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
            "memento-vault",
        )
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "maps": maps,
    }

    target = config_dir / "project-maps.json"
    tmp = config_dir / ".project-maps.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(str(tmp), str(target))


# Stopwords for concept index tokenization (same set used by retrieval)
_CONCEPT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "per",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "use",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "all",
        "also",
        "any",
        "been",
        "can",
        "could",
        "did",
        "do",
        "does",
        "each",
        "get",
        "got",
        "just",
        "more",
        "most",
        "much",
        "must",
        "need",
        "new",
        "now",
        "old",
        "one",
        "only",
        "other",
        "own",
        "same",
        "set",
        "should",
        "some",
        "such",
        "take",
        "two",
        "way",
        "well",
        "would",
        # common markdown / note words
        "cross",
        "project",
        "patterns",
        "pattern",
        "notes",
        "note",
    }
)


def _tokenize_keywords(text):
    """Tokenize text into lowercase keywords, stripping punctuation."""
    words = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower()).split()
    return [w for w in words if len(w) >= 3 and w not in _CONCEPT_STOPWORDS]


def build_concept_index(vault_path):
    """Scan notes/*.md for inception pattern notes and build an inverted keyword index.

    Returns:
        dict mapping keyword -> [{stem, title, score}, ...]
    """
    notes_dir = Path(vault_path) / "notes"
    if not notes_dir.exists():
        return {}

    index = {}  # keyword -> list of {stem, title, score}

    for md_file in sorted(notes_dir.glob("*.md")):
        if md_file.name.startswith("."):
            continue
        record = parse_note(md_file)
        if record is None or record.source != "inception":
            continue

        # Score from certainty: certainty / 5, default 0.6
        score = record.certainty / 5.0 if record.certainty is not None else 0.6

        # Collect keywords from: title words, tags, synthesized_from stems
        keywords = set()

        # Title words
        keywords.update(_tokenize_keywords(record.title))

        # Tags (lowercased, as-is — already single words)
        for tag in record.tags:
            tag_lower = tag.lower().strip()
            if len(tag_lower) >= 3 and tag_lower not in _CONCEPT_STOPWORDS:
                keywords.add(tag_lower)

        # synthesized_from stems: split on hyphens to get words
        for stem in record.synthesized_from:
            for word in stem.split("-"):
                word = word.lower().strip()
                if len(word) >= 3 and word not in _CONCEPT_STOPWORDS:
                    keywords.add(word)

        entry = {"stem": record.stem, "title": record.title, "score": score}
        for kw in keywords:
            index.setdefault(kw, []).append(entry)

    return index


def write_concept_index(index, config_dir=None):
    """Atomic write of concept index to config dir.

    JSON format: {"version": 1, "built_at": ISO, "index": {keyword: [{stem, title, score}]}}
    """
    if config_dir is None:
        config_dir = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.join(str(Path.home()), ".config")),
            "memento-vault",
        )
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "index": index,
    }

    target = config_dir / "concept-index.json"
    tmp = config_dir / ".concept-index.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(str(tmp), str(target))


# --- Sleep-time pre-reasoning ---

_FILE_PATH_RE = re.compile(
    r"(?:^|[\s`\"'(])"  # boundary before path
    r"((?:/[\w.+-]+){2,})"  # at least 2 slash-separated segments
    r"(?:[\s`\"').,;:]|$)",  # boundary after path
)


def _predict_queries(pattern_record, source_records):
    """Generate predicted search queries for a pattern note.

    Uses title variants, tags, and source note titles — no LLM call.

    Returns:
        list of query strings (3-5 items)
    """
    queries = set()

    # 1. Title as-is (lowercased)
    title = pattern_record.title.strip()
    if title:
        queries.add(title.lower())

    # 2. Individual tags
    for tag in pattern_record.tags:
        tag = tag.strip()
        if len(tag) >= 3:
            queries.add(tag.lower())

    # 3. Tag pairs (if 2+ tags)
    tags = [t.strip().lower() for t in pattern_record.tags if len(t.strip()) >= 3]
    for i in range(len(tags)):
        for j in range(i + 1, len(tags)):
            queries.add(f"{tags[i]} {tags[j]}")

    # 4. Source note title keywords (skip short/stop words)
    for src in source_records:
        words = _tokenize_keywords(src.title)
        if words:
            queries.add(" ".join(words[:4]))

    # 5. Title keywords only (without stopwords)
    title_kw = _tokenize_keywords(title)
    if title_kw:
        queries.add(" ".join(title_kw))

    # Deduplicate and cap at 5
    return sorted(queries)[:5]


def _extract_connections(pattern_record, source_records):
    """Extract projects and code areas from source notes.

    Projects come from frontmatter `project` fields.
    Code areas come from file paths found in note bodies.

    Returns:
        dict with keys: projects (list[str]), code_areas (list[str])
    """
    projects = set()
    code_areas = set()

    for src in source_records:
        if src.project:
            projects.add(src.project)

        # Extract file paths from body
        for match in _FILE_PATH_RE.finditer(src.body):
            path = match.group(1)
            # Only keep paths that look like code (have a file extension or known dir)
            if "." in path.split("/")[-1] or any(seg in path for seg in ("src", "lib", "pkg", "cmd", "hooks", "tests")):
                code_areas.add(path)

    return {
        "projects": sorted(projects),
        "code_areas": sorted(code_areas),
    }


def pre_reason(pattern_notes, notes_dict, config):
    """Sleep-time pre-reasoning: build retrieval artifacts from pattern notes.

    Generates two JSON files in {vault}/notes/:
    - .inception-queries.json: maps pattern note stems to predicted search queries
    - .inception-connections.json: maps pattern note stems to related projects/code areas

    Args:
        pattern_notes: list of NoteRecord for newly written pattern notes
        notes_dict: dict mapping stem -> NoteRecord for all notes
        config: dict with vault_path and inception_pre_reason

    Returns:
        tuple (queries_path, connections_path) or (None, None) if disabled/empty
    """
    if not config.get("inception_pre_reason", True):
        return None, None

    if not pattern_notes:
        return None, None

    vault_path = Path(config["vault_path"])
    notes_dir = vault_path / "notes"

    queries_map = {}
    connections_map = {}

    for pattern in pattern_notes:
        # Gather source records for this pattern
        source_records = []
        for stem in pattern.synthesized_from:
            src = notes_dict.get(stem)
            if src:
                source_records.append(src)

        queries_map[pattern.stem] = _predict_queries(pattern, source_records)
        connections_map[pattern.stem] = _extract_connections(pattern, source_records)

    # Merge with existing files (don't clobber previous runs' data)
    queries_path = notes_dir / ".inception-queries.json"
    connections_path = notes_dir / ".inception-connections.json"

    existing_queries = {}
    if queries_path.exists():
        try:
            existing_queries = json.loads(queries_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    existing_connections = {}
    if connections_path.exists():
        try:
            existing_connections = json.loads(connections_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    existing_queries.update(queries_map)
    existing_connections.update(connections_map)

    # Atomic writes
    tmp_q = notes_dir / ".inception-queries.json.tmp"
    tmp_q.write_text(json.dumps(existing_queries, indent=2))
    os.replace(str(tmp_q), str(queries_path))

    tmp_c = notes_dir / ".inception-connections.json.tmp"
    tmp_c.write_text(json.dumps(existing_connections, indent=2))
    os.replace(str(tmp_c), str(connections_path))

    return queries_path, connections_path


def _commit_and_reindex(notes_written, config):
    """Commit vault changes and trigger QMD reindex."""
    from memento_utils import has_qmd

    vault = Path(config["vault_path"])
    commit_script = Path.home() / ".claude" / "hooks" / "vault-commit.sh"

    if config.get("auto_commit", True) and commit_script.exists():
        noun = "note" if notes_written == 1 else "notes"
        try:
            subprocess.Popen(
                [str(commit_script), f"inception: {notes_written} pattern {noun}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(vault),
            )
        except OSError:
            pass

    if has_qmd():
        collection = config.get("qmd_collection", "memento")
        try:
            subprocess.Popen(
                ["sh", "-c", f"qmd update -c {collection} && qmd embed"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
