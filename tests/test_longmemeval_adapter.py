"""Tests for LongMemEval haystack ingestion adapter."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmark"))

import pytest
from longmemeval_adapter import (
    build_vault_notes,
    compute_retrieval_metrics,
    get_default_config,
    ingest_haystack,
    load_dataset,
    run_retrieval,
    run_retrieval_eval,
)


# ---- fixtures ----


@pytest.fixture
def sample_question():
    return {
        "question_id": "test_q1",
        "question": "What is the user's favorite coffee?",
        "answer": "espresso",
        "question_date": "2024-03-15",
        "haystack_session_ids": ["sess_001", "sess_002", "sess_003"],
        "haystack_dates": ["2024-01-10", "2024-02-15", "2024-03-01"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I really love espresso, it's my favorite coffee"},
                {
                    "role": "assistant",
                    "content": "Espresso is a great choice! Do you prefer it straight or as a latte?",
                },
                {"role": "user", "content": "Straight, always straight espresso"},
            ],
            [
                {"role": "user", "content": "What's the weather like today?"},
                {
                    "role": "assistant",
                    "content": "It's sunny and warm, perfect day to be outside.",
                },
            ],
            [
                {"role": "user", "content": "Can you help me with my Python code?"},
                {"role": "assistant", "content": "Of course! What are you working on?"},
                {"role": "user", "content": "I'm building a REST API with FastAPI"},
                {
                    "role": "assistant",
                    "content": "FastAPI is great for that. Let's start with your route definitions.",
                },
            ],
        ],
        "answer_session_ids": ["sess_001"],
    }


# ---- ingest_haystack: session granularity ----


class TestIngestSessionGranularity:
    def test_ingest_session_granularity(self, sample_question):
        """3 sessions should produce 3 documents."""
        docs = ingest_haystack(sample_question, granularity="session")
        assert len(docs) == 3

    def test_document_has_required_fields(self, sample_question):
        """Each document must have id, text, and metadata keys."""
        docs = ingest_haystack(sample_question, granularity="session")
        for doc in docs:
            assert "id" in doc
            assert "text" in doc
            assert "metadata" in doc

    def test_document_metadata_has_date(self, sample_question):
        """metadata.date should match the corresponding haystack_dates entry."""
        docs = ingest_haystack(sample_question, granularity="session")
        assert docs[0]["metadata"]["date"] == "2024-01-10"
        assert docs[1]["metadata"]["date"] == "2024-02-15"
        assert docs[2]["metadata"]["date"] == "2024-03-01"

    def test_document_metadata_has_session_id(self, sample_question):
        """metadata.session_id should match the corresponding haystack_session_ids entry."""
        docs = ingest_haystack(sample_question, granularity="session")
        assert docs[0]["metadata"]["session_id"] == "sess_001"
        assert docs[1]["metadata"]["session_id"] == "sess_002"
        assert docs[2]["metadata"]["session_id"] == "sess_003"

    def test_session_text_contains_all_turns(self, sample_question):
        """Session document text should contain content from every turn in that session."""
        docs = ingest_haystack(sample_question, granularity="session")
        # First session has 3 turns about espresso
        assert "espresso" in docs[0]["text"]
        assert "straight" in docs[0]["text"].lower()
        assert "latte" in docs[0]["text"]
        # Second session about weather
        assert "weather" in docs[1]["text"]
        assert "sunny" in docs[1]["text"]


# ---- ingest_haystack: turn granularity ----


class TestIngestTurnGranularity:
    def test_ingest_turn_granularity(self, sample_question):
        """3 sessions with 3+2+4 turns should produce 9 documents."""
        docs = ingest_haystack(sample_question, granularity="turn")
        assert len(docs) == 9

    def test_turn_text_has_role_prefix(self, sample_question):
        """Each turn document should start with 'user:' or 'assistant:'."""
        docs = ingest_haystack(sample_question, granularity="turn")
        for doc in docs:
            text = doc["text"]
            assert text.startswith("user:") or text.startswith("assistant:"), (
                f"Turn text should start with role prefix, got: {text[:40]}"
            )

    def test_turn_metadata_has_turn_idx(self, sample_question):
        """Turn-granularity docs should have turn_idx in metadata."""
        docs = ingest_haystack(sample_question, granularity="turn")
        for doc in docs:
            assert "turn_idx" in doc["metadata"]

    def test_turn_metadata_has_role(self, sample_question):
        """Turn-granularity docs should have role in metadata."""
        docs = ingest_haystack(sample_question, granularity="turn")
        for doc in docs:
            assert doc["metadata"]["role"] in ("user", "assistant")


# ---- build_vault_notes ----


class TestBuildVaultNotes:
    def test_build_vault_notes_creates_files(self, sample_question, tmp_path):
        """Each document should produce a markdown file in notes/."""
        docs = ingest_haystack(sample_question, granularity="session")
        vault_path, note_paths = build_vault_notes(docs, vault_dir=str(tmp_path))
        assert len(note_paths) == 3
        for p in note_paths:
            assert p.exists()
            assert p.suffix == ".md"

    def test_build_vault_notes_has_frontmatter(self, sample_question, tmp_path):
        """Each note should have YAML frontmatter with title and date."""
        docs = ingest_haystack(sample_question, granularity="session")
        _, note_paths = build_vault_notes(docs, vault_dir=str(tmp_path))
        for p in note_paths:
            content = p.read_text()
            assert content.startswith("---"), f"Missing frontmatter in {p.name}"
            assert "title:" in content
            assert "date:" in content

    def test_build_vault_notes_temp_dir(self, sample_question):
        """When vault_dir=None, a temp directory is created automatically."""
        docs = ingest_haystack(sample_question, granularity="session")
        vault_path, note_paths = build_vault_notes(docs, vault_dir=None)
        assert vault_path.exists()
        assert "longmemeval-vault-" in str(vault_path)
        assert len(note_paths) == 3
        # Clean up
        for p in note_paths:
            p.unlink()

    def test_build_vault_notes_in_notes_subdir(self, sample_question, tmp_path):
        """Notes should be written inside a notes/ subdirectory."""
        docs = ingest_haystack(sample_question, granularity="session")
        vault_path, note_paths = build_vault_notes(docs, vault_dir=str(tmp_path))
        for p in note_paths:
            assert p.parent.name == "notes"


# ---- load_dataset ----


class TestLoadDataset:
    def test_load_dataset_list_format(self, tmp_path):
        """A JSON file containing a plain list should return that list."""
        data = [{"question_id": "q1"}, {"question_id": "q2"}]
        path = tmp_path / "dataset.json"
        path.write_text(json.dumps(data))
        result = load_dataset(str(path))
        assert len(result) == 2
        assert result[0]["question_id"] == "q1"

    def test_load_dataset_dict_format(self, tmp_path):
        """A JSON file with {"data": [...]} should extract the list."""
        data = {"data": [{"question_id": "q1"}, {"question_id": "q2"}]}
        path = tmp_path / "dataset.json"
        path.write_text(json.dumps(data))
        result = load_dataset(str(path))
        assert len(result) == 2
        assert result[0]["question_id"] == "q1"

    def test_load_dataset_questions_key(self, tmp_path):
        """A JSON file with {"questions": [...]} should also work."""
        data = {"questions": [{"question_id": "q1"}]}
        path = tmp_path / "dataset.json"
        path.write_text(json.dumps(data))
        result = load_dataset(str(path))
        assert len(result) == 1


# ---- run_retrieval ----


class TestRunRetrieval:
    def test_returns_results(self, sample_question):
        """run_retrieval should return a non-empty list of result dicts."""
        results = run_retrieval(sample_question)
        assert len(results) > 0
        assert all("path" in r and "score" in r for r in results)

    def test_finds_answer_session(self, sample_question):
        """Top results should include the answer session (sess_001 has espresso)."""
        config = get_default_config()
        config["retrieval_limit"] = 5
        results = run_retrieval(sample_question, config)
        retrieved_ids = [Path(r["path"]).stem for r in results]
        assert "sess_001" in retrieved_ids or any("sess_001" in rid for rid in retrieved_ids)

    def test_respects_config_limit(self, sample_question):
        config = get_default_config()
        config["retrieval_limit"] = 2
        results = run_retrieval(sample_question, config)
        assert len(results) <= 2

    def test_prf_disabled(self, sample_question):
        """With PRF disabled, should still return results."""
        config = get_default_config()
        config["prf_enabled"] = False
        results = run_retrieval(sample_question, config)
        assert len(results) > 0


# ---- compute_retrieval_metrics ----


class TestComputeRetrievalMetrics:
    def test_perfect_recall(self, sample_question):
        """When answer session is the top result, recall@1 should be 1.0."""
        results = [{"path": "notes/sess_001.md", "score": 0.9, "_doc_id": "sess_001"}]
        metrics = compute_retrieval_metrics(results, sample_question)
        assert metrics["recall@1"] == 1.0
        assert metrics["mrr"] == 1.0

    def test_partial_recall(self):
        """When answer session is at position 3, recall@1=0, recall@3=1, mrr=0.33."""
        question = {"answer_session_ids": ["target"]}
        results = [
            {"path": "notes/other1.md", "score": 0.9, "_doc_id": "other1"},
            {"path": "notes/other2.md", "score": 0.8, "_doc_id": "other2"},
            {"path": "notes/target.md", "score": 0.7, "_doc_id": "target"},
        ]
        metrics = compute_retrieval_metrics(results, question)
        assert metrics["recall@1"] == 0.0
        assert metrics["recall@3"] == 1.0
        assert abs(metrics["mrr"] - 1 / 3) < 0.01

    def test_no_answer_found(self):
        """When answer session is not in results, all metrics should be 0."""
        question = {"answer_session_ids": ["missing"]}
        results = [{"path": "notes/other.md", "score": 0.9, "_doc_id": "other"}]
        metrics = compute_retrieval_metrics(results, question)
        assert metrics["recall@1"] == 0.0
        assert metrics["mrr"] == 0.0

    def test_empty_results(self):
        question = {"answer_session_ids": ["target"]}
        metrics = compute_retrieval_metrics([], question)
        assert metrics["recall@1"] == 0.0


# ---- run_retrieval_eval ----


class TestRunRetrievalEval:
    def test_aggregates_metrics(self, tmp_path, sample_question):
        """run_retrieval_eval should return aggregated metrics."""
        dataset = [sample_question]
        dataset_path = tmp_path / "test_dataset.json"
        dataset_path.write_text(json.dumps(dataset))

        metrics = run_retrieval_eval(str(dataset_path))
        assert "recall@5" in metrics
        assert "mrr" in metrics
        assert "num_questions" in metrics
        assert metrics["num_questions"] == 1

    def test_max_questions(self, tmp_path, sample_question):
        """max_questions should limit evaluation."""
        dataset = [sample_question, sample_question, sample_question]
        dataset_path = tmp_path / "test_dataset.json"
        dataset_path.write_text(json.dumps(dataset))

        metrics = run_retrieval_eval(str(dataset_path), max_questions=1)
        assert metrics["num_questions"] == 1
