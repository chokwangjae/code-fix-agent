from __future__ import annotations

from contextlib import AbstractContextManager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError, URLError

from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.crontrol import CrontrolReporter
from fix_agent.state import StateStore
from test_contract import event


class FakeResponse(AbstractContextManager):
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __exit__(self, *args: object) -> None:
        return None


class RecordingOpener:
    def __init__(self, *, missing_job: bool = False, offline: bool = False) -> None:
        self.missing_job = missing_job
        self.offline = offline
        self.calls: list[tuple[object, int]] = []

    def __call__(self, request, *, timeout: int):
        self.calls.append((request, timeout))
        if self.offline:
            raise URLError("offline")
        if self.missing_job and request.method == "PATCH":
            raise HTTPError(request.full_url, 404, "not found", {}, None)
        return FakeResponse(201 if request.method == "POST" else 200)


class CrontrolReporterTest(unittest.TestCase):
    def test_syncs_current_stage_without_finding_content(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                job = state.claim_next(config.repositories)
            opener = RecordingOpener()
            reporter = CrontrolReporter(config, opener=opener)
            first = reporter.sync(job.id, "finding 검증 중")
            second = reporter.sync(job.id, "finding 검증 중")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        payload = json.loads(request.data)
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(timeout, 4)
        self.assertEqual(payload["name"], "Code Fix Agent")
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["currentJobId"], accepted.job_ids[0])
        self.assertEqual(payload["currentRepository"], "owner/repo")
        self.assertEqual(payload["currentStage"], "finding 검증 중")
        self.assertEqual(payload["schedule"], "repo #1 · finding 검증 중 · 대기 0건")
        self.assertTrue(payload["running"])
        self.assertEqual(payload["runningJobCount"], 1)
        self.assertEqual(payload["maxConcurrentJobs"], 3)
        self.assertEqual(payload["runningJobs"][0]["jobId"], accepted.job_ids[0])
        serialized = json.dumps(payload)
        self.assertNotIn("Failure is swallowed", serialized)
        self.assertNotIn("src/app.py", serialized)

    def test_creates_registration_after_patch_404(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            opener = RecordingOpener(missing_job=True)
            sent = CrontrolReporter(config, opener=opener).sync(None)

        self.assertTrue(sent)
        self.assertEqual([call[0].method for call in opener.calls], ["PATCH", "POST"])
        payload = json.loads(opener.calls[-1][0].data)
        self.assertEqual(payload["schedule"], "유휴 · 대기 0건")
        self.assertFalse(payload["running"])

    def test_connection_failure_does_not_raise(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            reporter = CrontrolReporter(
                config, opener=RecordingOpener(offline=True)
            )
            self.assertFalse(reporter.sync(None))
            self.assertIn("offline", reporter.last_error)

    def test_unknown_current_job_is_reported_without_request(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            opener = RecordingOpener()
            reporter = CrontrolReporter(config, opener=opener)
            self.assertFalse(reporter.sync(999, "수정 중"))
            self.assertEqual(opener.calls, [])
            self.assertIn("job does not exist", reporter.last_error)

    def test_retryable_failure_remains_pending(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                job = state.claim_next(config.repositories)
                state.mark_failed(job.id, "harness failed", 30)
            opener = RecordingOpener()
            reporter = CrontrolReporter(config, opener=opener)
            self.assertTrue(reporter.sync(job.id))

        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["currentStage"], "재시도 대기")
        self.assertEqual(payload["queuedJobs"], 1)
        self.assertEqual(payload["lastResult"], "PASS")

    def test_reports_all_concurrent_jobs_and_stages(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            second_event = event()
            second_event["findings"][0]["fingerprint"] = "sha256:" + "d" * 64
            with StateStore(config.state_dir) as state:
                state.accept(config.repositories[0], parse_review_event(event()))
                state.accept(
                    config.repositories[0], parse_review_event(second_event)
                )
                first = state.claim_next(config.repositories)
                second = state.claim_next(config.repositories)
            opener = RecordingOpener()
            reporter = CrontrolReporter(config, opener=opener)
            reporter.sync(first.id, "수정 중")
            reporter.sync(second.id, "테스트 중")

        payload = json.loads(opener.calls[-1][0].data)
        self.assertEqual(payload["runningJobCount"], 2)
        self.assertEqual(payload["schedule"], "동시 2건 · repo #2 · 테스트 중 · 대기 0건")
        self.assertEqual(
            {item["jobId"]: item["stage"] for item in payload["runningJobs"]},
            {first.id: "수정 중", second.id: "테스트 중"},
        )

    @staticmethod
    def _config(root: Path):
        path = root / "fix.toml"
        path.write_text(
            """
version = 1
state_dir = ".state"
[server]
token = "intake"
max_concurrent_jobs = 3
[crontrol]
enabled = true
base_url = "http://127.0.0.1:7070"
job_id = "code-fix-agent-server"
name = "Code Fix Agent"
branch = "main"
timeout_seconds = 4
[[repositories]]
id = "repo"
github = "owner/repo"
branch = "main"
local_path = "repo"
test_commands = []
[repositories.execution]
max_attempts = 0
""",
            encoding="utf-8",
        )
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
