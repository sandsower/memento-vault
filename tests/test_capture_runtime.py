import json
from datetime import datetime, timezone
from pathlib import Path

from memento.capture_runtime import (
    CaptureProcessRequest,
    CaptureRuntime,
    MemoryQueueStore,
    RuntimeClock,
    safe_segment,
    select_captures,
)


class FakeProcessingStore:
    def __init__(self, root: Path):
        self._root = root
        self.released = []
        self.lock_error = None

    def root(self) -> Path:
        return self._root

    def acquire_lock(self, run_id: str, owner_pid: int = 0):
        return self.lock_error

    def release_lock(self, run_id: str) -> None:
        self.released.append(run_id)

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read_json(self, path: Path):
        return json.loads(path.read_text())


class FakePreparer:
    def prepare(self, run_dir: Path, vault: Path, group: dict, transcript_max_bytes: int) -> dict:
        group_id = group["group_id"]
        input_json = run_dir / "inputs" / f"{group_id}.json"
        input_markdown = run_dir / "inputs" / f"{group_id}.md"
        result_json = run_dir / "results" / f"{group_id}.json"
        log_markdown = run_dir / "logs" / f"{group_id}.md"
        input_json.write_text(json.dumps(group))
        input_markdown.write_text("input")
        return {
            "group_id": group_id,
            "capture_ids": group.get("capture_ids", []),
            "session_id": group.get("session_id"),
            "project": group.get("project"),
            "branch": group.get("branch"),
            "cwd": group.get("cwd"),
            "input_json": str(input_json),
            "input_markdown": str(input_markdown),
            "result_json": str(result_json),
            "log_markdown": str(log_markdown),
            "transcript": {"included": False, "reason": "test"},
        }


class FakeVaultWriter:
    def __init__(self, existing_paths=None):
        self.existing_paths = set(existing_paths or [])
        self.dequeued = []

    def reported_note_exists(self, vault: Path, path: str) -> bool:
        return path in self.existing_paths

    def on_dequeued(self, vault: Path, run_id: str, dequeue_ids: set[str]) -> None:
        self.dequeued.append((run_id, set(dequeue_ids)))


def _runtime(tmp_path, captures):
    queue = MemoryQueueStore(captures, tmp_path / "state" / "queue" / "pi-captures.jsonl")
    processing = FakeProcessingStore(tmp_path / "state" / "processing")
    writer = FakeVaultWriter(existing_paths={"notes/created.md"})
    runtime = CaptureRuntime(
        vault=lambda: tmp_path,
        queue=queue,
        processing=processing,
        preparer=FakePreparer(),
        writer=writer,
        transcript_counter=lambda groups: {"transcript_fallback_group_count": len(groups)},
        clock=RuntimeClock(
            now=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            uuid_hex=lambda: "abcdef1234567890",
        ),
    )
    return runtime, queue, processing, writer


def test_select_captures_oldest_first_and_skips_processor_captures():
    captures = [
        {"id": "q2", "created_at": "2026-01-01T00:02:00Z", "metadata": {"project": "repo"}},
        {"id": "q1", "created_at": "2026-01-01T00:01:00Z", "metadata": {"project": "repo"}},
        {"id": "qp", "created_at": "2026-01-01T00:00:00Z", "metadata": {"project": "repo", "memento_processor": True}},
        {"id": "other", "created_at": "2026-01-01T00:03:00Z", "metadata": {"project": "other"}},
    ]

    selected = select_captures(captures, project="repo")

    assert [capture["id"] for capture in selected] == ["q1", "q2"]


def test_process_dry_run_groups_and_limits_without_writing_run(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(
        tmp_path,
        [
            {"id": "q1", "created_at": "2026-01-01T00:00:00Z", "metadata": {"project": "repo", "session_id": "s1"}},
            {"id": "q2", "created_at": "2026-01-01T00:01:00Z", "metadata": {"project": "repo", "session_id": "s1"}},
            {"id": "q3", "created_at": "2026-01-01T00:02:00Z", "metadata": {"project": "repo", "session_id": "s2"}},
        ],
    )

    payload = runtime.process(CaptureProcessRequest(project="repo", limit=1, dry_run=True))

    assert payload["dry_run"] is True
    assert payload["selected_capture_count"] == 1
    assert payload["group_count"] == 1
    assert payload["groups"][0]["capture_ids"] == ["q1"]
    assert not (tmp_path / "state" / "processing").exists()


def test_process_limit_uses_global_newest_order_across_groups(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(
        tmp_path,
        [
            {"id": "q1", "created_at": "2026-01-01T00:00:00Z", "metadata": {"project": "repo", "session_id": "s1"}},
            {"id": "q2", "created_at": "2026-01-01T00:01:00Z", "metadata": {"project": "repo", "session_id": "s2"}},
            {"id": "q3", "created_at": "2026-01-01T00:02:00Z", "metadata": {"project": "repo", "session_id": "s1"}},
            {"id": "q4", "created_at": "2026-01-01T00:03:00Z", "metadata": {"project": "repo", "session_id": "s2"}},
        ],
    )

    payload = runtime.process(CaptureProcessRequest(project="repo", limit=2, newest=True, dry_run=True))

    assert [capture_id for group in payload["groups"] for capture_id in group["capture_ids"]] == ["q4", "q3"]


def test_process_writes_manifest_and_group_inputs(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(
        tmp_path,
        [{"id": "q1", "created_at": "2026-01-01T00:00:00Z", "metadata": {"project": "repo", "session_id": "s1"}}],
    )

    payload = runtime.process(CaptureProcessRequest(project="repo"))

    assert payload["run_id"] == "20260102T030405Z-abcdef12"
    assert payload["selected_capture_count"] == 1
    run_dir = Path(payload["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "running"
    assert manifest["groups"][0]["capture_ids"] == ["q1"]
    assert Path(manifest["groups"][0]["input_markdown"]).exists()


def test_process_returns_lock_error_without_writing_run(tmp_path):
    runtime, _queue, processing, _writer = _runtime(
        tmp_path,
        [{"id": "q1", "created_at": "2026-01-01T00:00:00Z", "metadata": {"project": "repo"}}],
    )
    processing.lock_error = {"error": "locked", "reason": "lock_active"}

    payload = runtime.process(CaptureProcessRequest(project="repo"))

    assert payload == {"error": "locked", "reason": "lock_active"}
    assert not (tmp_path / "state" / "processing").exists()


def test_finalize_dequeues_successes_and_preserves_failed_captures(tmp_path):
    runtime, queue, processing, writer = _runtime(
        tmp_path,
        [
            {"id": "q1", "metadata": {"project": "repo", "session_id": "s1"}},
            {"id": "q2", "metadata": {"project": "repo", "session_id": "s2"}},
        ],
    )
    started = runtime.process(CaptureProcessRequest(project="repo"))
    run_dir = Path(started["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    by_session = {group["session_id"]: group for group in manifest["groups"]}
    Path(by_session["s1"]["result_json"]).write_text(
        json.dumps({"processed_capture_ids": ["q1"], "status": "processed_no_notes", "discard_reason": "noise"})
    )
    Path(by_session["s2"]["result_json"]).write_text(
        json.dumps({"processed_capture_ids": ["q2"], "status": "failed", "result_state": "no_output"})
    )

    finalized = runtime.finalize(started["run_id"])

    assert finalized["dequeued"] == 1
    assert [capture["id"] for capture in queue.captures] == ["q2"]
    assert {group["status"] for group in finalized["groups"]} == {"processed_no_notes", "failed"}
    assert writer.dequeued == [(started["run_id"], {"q1"})]
    assert processing.released == [started["run_id"]]


def test_finalize_returns_run_not_found_for_missing_manifest(tmp_path):
    runtime, _queue, processing, _writer = _runtime(tmp_path, [])

    payload = runtime.finalize("missing-run")

    assert payload == {"error": "processing run not found: missing-run", "reason": "run_not_found"}
    assert processing.released == []


def test_finalize_releases_lock_for_invalid_manifest(tmp_path):
    runtime, _queue, processing, _writer = _runtime(tmp_path, [])
    run_dir = processing.root() / "bad-run"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("not-json")

    payload = runtime.finalize("bad-run")

    assert payload == {"error": "invalid processing manifest: bad-run", "reason": "invalid_manifest"}
    assert processing.released == ["bad-run"]


def test_finalize_missing_result_json_is_failed_group_not_current_directory(tmp_path):
    runtime, queue, processing, _writer = _runtime(
        tmp_path,
        [{"id": "q1", "metadata": {"project": "repo", "session_id": "s1"}}],
    )
    run_dir = processing.root() / "run-with-missing-result"
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": "run-with-missing-result",
        "status": "running",
        "groups": [{"group_id": "g1", "capture_ids": ["q1"], "result_json": ""}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))

    payload = runtime.finalize("run-with-missing-result")

    assert payload["dequeued"] == 0
    assert queue.captures == [{"id": "q1", "metadata": {"project": "repo", "session_id": "s1"}}]
    assert payload["groups"] == [{"group_id": "g1", "status": "failed", "reason": "missing_result"}]
    assert processing.released == ["run-with-missing-result"]


def test_plan_retry_requires_explicit_failed_groups(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(tmp_path, [])
    status = {
        "run_id": "run1",
        "status": "interrupted",
        "groups": [
            {"group_id": "g1", "status": "failed", "capture_ids": ["q1"], "reason": "no_output"},
            {"group_id": "g2", "status": "processed_no_notes", "capture_ids": ["q2"]},
            {"group_id": "g3", "status": "failed", "capture_ids": ["q3", "q4"]},
        ],
    }

    payload = runtime.plan_retry(status, group_ids=["g3"])

    assert payload["source_run_id"] == "run1"
    assert payload["retry_group_count"] == 1
    assert payload["selected_group_ids"] == ["g3"]
    assert payload["selected_capture_ids"] == ["q3", "q4"]


def test_plan_retry_without_group_ids_selects_all_failed_groups(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(tmp_path, [])
    status = {
        "run_id": "run1",
        "status": "interrupted",
        "groups": [
            {"group_id": "g1", "status": "failed", "capture_ids": ["q1"], "reason": "no_output"},
            {"group_id": "g2", "status": "processed_no_notes", "capture_ids": ["q2"]},
            {"group_id": "g3", "status": "failed", "capture_ids": ["q3", "q4"]},
        ],
    }

    payload = runtime.plan_retry(status)

    assert payload["retry_group_count"] == 2
    assert payload["selected_group_ids"] == ["g1", "g3"]
    assert payload["selected_capture_ids"] == ["q1", "q3", "q4"]


def test_plan_retry_without_failed_groups_returns_no_failed_groups(tmp_path):
    runtime, _queue, _processing, _writer = _runtime(tmp_path, [])
    status = {"run_id": "run1", "status": "interrupted", "groups": [{"group_id": "g1", "status": "processed"}]}

    payload = runtime.plan_retry(status)

    assert payload["reason"] == "no_failed_groups"
    assert payload["run_id"] == "run1"


def test_safe_segment_adds_digest_to_avoid_sanitized_collisions():
    assert safe_segment("session:a/b") != safe_segment("session:a b")
