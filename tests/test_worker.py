from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.codex_agent import Decision
from fix_agent.command import CommandRunner
from fix_agent.contract import parse_review_event
from fix_agent.state import StateStore
from fix_agent.worker import FixWorker
from test_workspace import WorkspaceTest


class FakeAgent:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def validate_finding(self, repository, job, workspace, environment):
        return Decision(self.valid, "The caller reaches the defective branch.")

    def apply_fix(self, repository, job, workspace, environment):
        (workspace / job.file).write_text("fixed\n", encoding="utf-8")

    def validate_fix(self, repository, job, workspace, environment):
        return Decision(True, "The caller now receives the failure.")


class LocalWorker(FixWorker):
    def _push(self, repository, workspace, environment, branch):
        return None

    def _publish_pull_request(self, repository, job, branch):
        return "https://github.com/owner/repo/pull/1"


class WorkerTest(unittest.TestCase):
    def test_records_both_decisions_and_completes_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            worker = LocalWorker(config, CommandRunner(), FakeAgent())
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.precheck_status, "valid")
        self.assertEqual(completed.postcheck_status, "resolved")
        self.assertEqual(completed.pr_url, "https://github.com/owner/repo/pull/1")

    def test_rejects_false_finding_without_editing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            worker = LocalWorker(config, CommandRunner(), FakeAgent(False))
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                rejected = state.jobs()[0]
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.precheck_status, "invalid")
        self.assertIsNone(rejected.result_commit)

    @staticmethod
    def _queue(config, baseline: str, target: str) -> None:
        event = parse_review_event(
            {
                "version": 1,
                "repository": "owner/repo",
                "branch": "main",
                "baseline": baseline,
                "target": target,
                "findings": [
                    {
                        "fingerprint": "sha256:" + "c" * 64,
                        "severity": "Major",
                        "commit": target,
                        "file": "src/app.py",
                        "line": 1,
                        "cause": "Failure is swallowed.",
                        "solution": "Return the failure.",
                    }
                ],
            }
        )
        with StateStore(config.state_dir) as state:
            state.accept(config.repositories[0], event)


if __name__ == "__main__":
    unittest.main()
