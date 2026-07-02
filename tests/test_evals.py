"""Smoke tests for the quality eval framework (evals/)."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evals import common  # noqa: E402
from evals.suites import capture_health, vault_content  # noqa: E402


class TestCommon:
    def test_grade_directions(self):
        assert common.grade(0.99, warn=0.9, fail=0.7, higher_is_better=True) == common.PASS
        assert common.grade(0.8, warn=0.9, fail=0.7, higher_is_better=True) == common.WARN
        assert common.grade(0.5, warn=0.9, fail=0.7, higher_is_better=True) == common.FAIL
        assert common.grade(0.05, warn=0.1, fail=0.3, higher_is_better=False) == common.PASS
        assert common.grade(0.2, warn=0.1, fail=0.3, higher_is_better=False) == common.WARN
        assert common.grade(0.4, warn=0.1, fail=0.3, higher_is_better=False) == common.FAIL
        assert common.grade(None, warn=0.9, fail=0.7) == common.SKIP

    def test_thresholds_file_parses(self):
        data = common.load_yaml_subset(common.EVALS_DIR / "thresholds.yml")
        assert data["vault_content"]["ephemeral_note_rate"]["warn"] == 0.05
        assert data["capture_health"]["window_days"] == 30
        assert data["retrieval_accuracy"]["golden_mrr"]["fail"] == 0.3

    def test_parse_note(self, tmp_path):
        note = tmp_path / "x.md"
        note.write_text("---\ntitle: T\ntype: decision\ncertainty: 4\n---\n\nBody [[link]]\n")
        fm, body = common.parse_note(note)
        assert fm["title"] == "T"
        assert fm["certainty"] == "4"
        assert "[[link]]" in body

    def test_parse_note_no_frontmatter(self, tmp_path):
        note = tmp_path / "x.md"
        note.write_text("just text")
        fm, body = common.parse_note(note)
        assert fm is None
        assert body == "just text"


def _write_note(vault, name, **fields):
    fields.setdefault("title", name)
    fields.setdefault("type", "decision")
    fields.setdefault("tags", '["t"]')
    fields.setdefault("source", "test")
    fields.setdefault("date", "2026-06-01T10:00")
    body = fields.pop("body", "A perfectly durable insight about the system.")
    frontmatter = "\n".join(f"{k}: {v}" for k, v in fields.items())
    (vault / "notes" / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n")


class TestVaultContent:
    def test_grades_synthetic_vault(self, tmp_path):
        (tmp_path / "notes").mkdir()
        _write_note(tmp_path, "good-note", certainty=4)
        _write_note(tmp_path, "bad-certainty", certainty=95)
        _write_note(
            tmp_path,
            "ephemeral-note",
            certainty=5,
            body="PR #12 was opened and marked ready for review.",
        )
        results = vault_content.run({"vault": tmp_path})
        by_id = {r.id: r for r in results}
        assert by_id["vault_content.frontmatter_parse_rate"].status == common.PASS
        cert = by_id["vault_content.certainty_valid_rate"]
        assert cert.value < 1.0
        assert any("bad-certainty" in d for d in cert.details)
        eph = by_id["vault_content.ephemeral_note_rate"]
        assert eph.value > 0.3
        assert eph.status == common.FAIL

    def test_missing_vault(self):
        results = vault_content.run({"vault": None})
        assert results[0].status == common.FAIL


class TestCaptureHealth:
    def test_grades_synthetic_telemetry(self, tmp_path):
        log = tmp_path / "triage-health.jsonl"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        events = (
            [{"ts": now, "action": "structured_notes_attempt"}] * 10
            + [{"ts": now, "action": "structured_notes_written", "notes_written": 2}] * 7
            + [{"ts": now, "action": "structured_notes_llm_failed"}] * 3
        )
        log.write_text("\n".join(json.dumps(e) for e in events))
        results = capture_health.run({"triage_health_log": log})
        by_id = {r.id: r for r in results}
        failure = by_id["capture_health.llm_failure_rate"]
        assert failure.value == 0.3
        assert failure.status == common.FAIL
        yield_check = by_id["capture_health.notes_per_attempt"]
        assert yield_check.value == 1.4

    def test_missing_log_skips(self, tmp_path):
        results = capture_health.run({"triage_health_log": tmp_path / "nope.jsonl"})
        assert results[0].status == common.SKIP


class TestRetrievalProbe:
    def test_fixture_mode_core_checks_pass(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "evals" / "retrieval_probe.py"), "--mode", "fixture"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        core = [c for c in payload["checks"] if not c["known_gap"]]
        failed = [c for c in core if not c["ok"]]
        assert not failed, failed
        assert len(core) >= 10


class TestRunner:
    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "evals" / "run_evals.py"), "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "vault_content" in proc.stdout

    def test_json_output_shape(self, tmp_path):
        (tmp_path / "notes").mkdir()
        _write_note(tmp_path, "solo-note", certainty=4)
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "evals" / "run_evals.py"),
                "--suite",
                "vault_content",
                "--vault",
                str(tmp_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        payload = json.loads(proc.stdout)
        assert payload["results"]
        for item in payload["results"]:
            assert {"id", "suite", "title", "status"} <= set(item)
