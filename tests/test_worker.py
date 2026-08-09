from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from fix_agent.codex_agent import (
    BatchChangeGroup,
    BatchFindingDecision,
    BatchFixDecision,
    Decision,
    InvocationMetrics,
)
from fix_agent.command import CommandResult, CommandRunner
from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.state import StateStore
from fix_agent.worker import FixWorker
from test_workspace import WorkspaceTest


class FakeAgent:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def validate_finding(
        self, repository, job, workspace, environment, workspace_base=None
    ):
        return Decision(self.valid, "The caller reaches the defective branch.")

    def apply_fix(self, repository, job, workspace, environment, workspace_base=None):
        (workspace / job.file).write_text("fixed\n", encoding="utf-8")

    def validate_fix(
        self, repository, job, workspace, environment, workspace_base=None
    ):
        return Decision(
            True,
            "The caller now receives the failure.",
            "fix(worker): 실패 반환 경로 복구",
        )


class MovingTargetAgent(FakeAgent):
    def __init__(self, repository_path: Path, *, conflict: bool) -> None:
        super().__init__()
        self.repository_path = repository_path
        self.conflict = conflict

    def apply_fix(self, repository, job, workspace, environment, workspace_base=None):
        super().apply_fix(repository, job, workspace, environment, workspace_base)
        if self.conflict:
            (self.repository_path / job.file).write_text("remote\n", encoding="utf-8")
        else:
            (self.repository_path / "src/remote.py").write_text(
                "remote\n", encoding="utf-8"
            )
        subprocess.run(
            ["git", "add", "--all"], cwd=self.repository_path, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "remote update"],
            cwd=self.repository_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=self.repository_path,
            check=True,
            capture_output=True,
        )

    def resolve_merge_conflicts(
        self,
        repository,
        job,
        workspace,
        environment,
        previous_base,
        current_target,
        conflict_files,
    ):
        (workspace / job.file).write_text("combined\n", encoding="utf-8")
        return Decision(True, "Preserved the remote update and the validated fix.")


class PerJobAgent(FakeAgent):
    def apply_fix(self, repository, job, workspace, environment, workspace_base=None):
        (workspace / job.file).write_text(
            f"fixed-{job.fingerprint[-1]}\n", encoding="utf-8"
        )


class RetryAgent(FakeAgent):
    def __init__(self) -> None:
        super().__init__()
        self.validation_calls = 0
        self.fix_calls = 0
        self.workspaces = []
        self.saw_existing_diff = False

    def validate_finding(
        self, repository, job, workspace, environment, workspace_base=None
    ):
        self.validation_calls += 1
        return super().validate_finding(
            repository, job, workspace, environment, workspace_base
        )

    def apply_fix(self, repository, job, workspace, environment, workspace_base=None):
        self.fix_calls += 1
        self.workspaces.append(workspace)
        target = workspace / job.file
        if self.fix_calls == 1:
            target.write_text("partial\n", encoding="utf-8")
            return
        self.saw_existing_diff = target.read_text(encoding="utf-8") == "partial\n"
        super().apply_fix(repository, job, workspace, environment, workspace_base)


class ReadOnlyFixAgent(FakeAgent):
    def apply_fix(self, repository, job, workspace, environment, workspace_base=None):
        super().apply_fix(repository, job, workspace, environment, workspace_base)
        (workspace / job.file).chmod(0o400)


class BatchAgent(FakeAgent):
    def __init__(self) -> None:
        super().__init__()
        self.validation_calls = 0
        self.fix_calls = 0
        self.result_validation_calls = 0

    def validate_findings(
        self, repository, jobs, workspace, environment, workspace_base
    ):
        self.validation_calls += 1
        return tuple(
            BatchFindingDecision(job.fingerprint, True, f"valid {job.file}")
            for job in jobs
        )

    def apply_batch_fixes(
        self,
        repository,
        jobs,
        workspace,
        environment,
        workspace_base,
        previous_error=None,
    ):
        self.fix_calls += 1
        by_file = {}
        for item in jobs:
            by_file.setdefault(item.file, []).append(item.fingerprint)
        for file in by_file:
            (workspace / file).write_text(f"fixed {file}\n", encoding="utf-8")
        return tuple(
            BatchChangeGroup(tuple(fingerprints), (file,))
            for file, fingerprints in by_file.items()
        )

    def validate_batch_fix(
        self, repository, jobs, groups, workspace, environment, workspace_base
    ):
        self.result_validation_calls += 1
        decisions = tuple(
            BatchFindingDecision(job.fingerprint, True, f"resolved {job.file}")
            for job in jobs
        )
        titled = tuple(
            BatchChangeGroup(
                group.fingerprints,
                group.files,
                f"fix(batch): {group.files[0]} 동작 수정",
            )
            for group in groups
        )
        return BatchFixDecision(True, "all findings resolved", decisions, titled)

    def take_batch_metrics(self):
        return InvocationMetrics(
            calls=3,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            duration_ms=1500,
        )


class MovingBatchAgent(BatchAgent):
    def __init__(self, repository_path: Path) -> None:
        super().__init__()
        self.repository_path = repository_path
        self.moved = False

    def apply_batch_fixes(
        self,
        repository,
        jobs,
        workspace,
        environment,
        workspace_base,
        previous_error=None,
    ):
        groups = super().apply_batch_fixes(
            repository,
            jobs,
            workspace,
            environment,
            workspace_base,
            previous_error,
        )
        if not self.moved:
            self.moved = True
            (self.repository_path / "src/remote.py").write_text(
                "remote update\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "src/remote.py"],
                cwd=self.repository_path,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "remote update"],
                cwd=self.repository_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=self.repository_path,
                check=True,
                capture_output=True,
            )
        return groups


class LocalWorker(FixWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pushed_branches = []
        self.pull_request_calls = 0

    def _push(self, repository, workspace, environment, branch):
        self.pushed_branches.append(branch)

    def _publish_pull_request(self, repository, job, branch):
        self.pull_request_calls += 1
        return "https://github.com/owner/repo/pull/1"


class LocalDirectPushWorker(LocalWorker):
    def _push(self, repository, workspace, environment, branch):
        self.pushed_branches.append(branch)
        self.runner.run(
            ["git", "push", repository.remote, f"HEAD:refs/heads/{branch}"],
            cwd=workspace,
            environment=environment,
        )


class LocalBatchPushWorker(LocalWorker):
    def _push_commit(
        self, repository, workspace, environment, branch, commit
    ):
        self.pushed_branches.append(branch)
        self.runner.run(
            ["git", "push", repository.remote, f"{commit}:refs/heads/{branch}"],
            cwd=workspace,
            environment=environment,
        )


class RecordingCrontrol:
    def __init__(self) -> None:
        self.calls = []

    def sync(self, current_job_id, stage=None):
        self.calls.append((current_job_id, stage))
        return True


class FlakySetupRunner(CommandRunner):
    def __init__(self) -> None:
        self.setup_calls = 0

    def run(self, command, **kwargs):
        if list(command) == ["setup-fixture"]:
            self.setup_calls += 1
            if self.setup_calls == 1:
                return CommandResult("", "temporary registry failure", 1)
            return CommandResult("setup complete", "", 0)
        return super().run(command, **kwargs)


class WorkerTest(unittest.TestCase):
    def test_repairs_permissions_changed_by_fix_before_harness(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            worker = LocalWorker(config, CommandRunner(), ReadOnlyFixAgent())

            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                events = state.events(completed.id)

        repairs = [
            event
            for event in events
            if event.event_type == "worktree_permissions_repaired"
        ]
        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(repairs), 1)
        self.assertIn('"repaired_files": 1', repairs[0].details_json)

    def test_retries_environment_setup_in_same_worktree_and_reuses_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config_path = root / "fix.toml"
            config_path.write_text(
                f"""
version = 1
state_dir = ".state"
[server]
token = "test-token"
[[repositories]]
id = "repo"
github = "owner/repo"
target_branch = "main"
local_path = "{repository_path}"
publish_mode = "direct"
github_token = "test-only-token"
setup_commands = [["setup-fixture"]]
test_commands = []
[repositories.execution]
setup_max_attempts = 2
setup_retry_delay_seconds = 0
max_attempts = 1
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self._queue(config, baseline, target)
            runner = FlakySetupRunner()
            worker = LocalWorker(config, runner, FakeAgent())

            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                event_types = [
                    event.event_type for event in state.events(completed.id)
                ]

        self.assertEqual(completed.status, "completed")
        self.assertEqual(runner.setup_calls, 2)
        self.assertEqual(event_types.count("environment_setup_started"), 1)
        self.assertEqual(event_types.count("environment_setup_failed"), 1)
        self.assertEqual(event_types.count("environment_setup_completed"), 1)
        self.assertEqual(event_types.count("worktree_created"), 1)

    def test_records_both_decisions_and_completes_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            crontrol = RecordingCrontrol()
            worker = LocalWorker(
                config, CommandRunner(), FakeAgent(), crontrol=crontrol
            )
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                event_types = [
                    event.event_type for event in state.events(completed.id)
                ]
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.precheck_status, "valid")
        self.assertEqual(completed.postcheck_status, "resolved")
        self.assertEqual(completed.pr_url, "https://github.com/owner/repo/pull/1")
        self.assertLess(
            event_types.index("finding_validation_started"),
            event_types.index("finding_validation_completed"),
        )
        self.assertLess(
            event_types.index("fix_started"), event_types.index("fix_applied")
        )
        stages = [stage for _, stage in crontrol.calls if stage]
        self.assertIn("finding 검증 중", stages)
        self.assertIn("수정 중", stages)
        self.assertIn("테스트 중", stages)
        self.assertIn("push 중", stages)
        self.assertEqual(crontrol.calls[-1], (None, None))

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

    def test_direct_mode_pushes_each_job_to_target_branch_without_pr(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(
                root, repository_path, publish_mode="direct"
            )
            self._queue(config, baseline, target)
            worker = LocalWorker(config, CommandRunner(), FakeAgent())
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                event_types = [event.event_type for event in state.events(completed.id)]
            author = subprocess.run(
                ["git", "show", "-s", "--format=%an|%ae", completed.result_commit],
                cwd=repository_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            title = subprocess.run(
                ["git", "show", "-s", "--format=%s", completed.result_commit],
                cwd=repository_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        self.assertEqual(completed.status, "completed")
        self.assertIsNone(completed.pr_url)
        self.assertEqual(worker.pushed_branches, ["main"])
        self.assertEqual(worker.pull_request_calls, 0)
        self.assertIn("worktree_created", event_types)
        self.assertIn("worktree_removed", event_types)
        self.assertIn("push_completed", event_types)
        self.assertEqual(author, "broken-agent|g_uapm@inswave.com")
        self.assertEqual(title, "fix(worker): 실패 반환 경로 복구")

    def test_merges_moved_target_and_revalidates_before_push(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            worker = LocalWorker(
                config,
                CommandRunner(),
                MovingTargetAgent(repository_path, conflict=False),
            )
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                event_types = [event.event_type for event in state.events(completed.id)]
            parents = subprocess.run(
                ["git", "rev-list", "--parents", "-n", "1", completed.result_commit],
                cwd=repository_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(parents), 3)
        self.assertIn("target_moved", event_types)
        self.assertIn("target_merged", event_types)
        self.assertIn("merged_fix_revalidated", event_types)

    def test_resolves_merge_conflict_and_records_reason(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(root, repository_path)
            self._queue(config, baseline, target)
            worker = LocalWorker(
                config,
                CommandRunner(),
                MovingTargetAgent(repository_path, conflict=True),
            )
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                events = state.events(completed.id)
        self.assertEqual(completed.status, "completed")
        conflict_events = {
            event.event_type: event for event in events if "conflict" in event.event_type
        }
        self.assertIn("merge_conflict_detected", conflict_events)
        self.assertIn("merge_conflict_decided", conflict_events)
        self.assertIn("merge_conflict_resolved", conflict_events)
        self.assertIn(
            "Preserved the remote update",
            conflict_events["merge_conflict_resolved"].details_json,
        )

    def test_direct_jobs_push_separate_commits_from_latest_target(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config = WorkspaceTest._config(
                root, repository_path, publish_mode="direct"
            )
            self._queue(config, baseline, target, fingerprint_character="c")
            self._queue(config, baseline, target, fingerprint_character="d")
            worker = LocalDirectPushWorker(config, CommandRunner(), PerJobAgent())
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                jobs = state.jobs()
                second_events = state.events(jobs[0].id)
            remaining_worktrees = list((config.state_dir / "worktrees").iterdir())
            commit_count = int(
                subprocess.run(
                    [
                        "git",
                        "rev-list",
                        "--count",
                        f"{target}..refs/remotes/origin/main",
                    ],
                    cwd=repository_path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
        self.assertEqual([job.status for job in jobs], ["completed", "completed"])
        self.assertEqual(worker.pushed_branches, ["main", "main"])
        self.assertEqual(commit_count, 2)
        self.assertEqual(remaining_worktrees, [])
        created = next(
            event for event in second_events if event.event_type == "worktree_created"
        )
        self.assertNotIn(target, created.details_json)

    def test_validated_job_retries_in_same_worktree_until_completed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config_path = root / "fix.toml"
            config_path.write_text(
                f"""
version = 1
state_dir = ".state"
[server]
token = "test-token"
[[repositories]]
id = "repo"
github = "owner/repo"
target_branch = "main"
local_path = "{repository_path}"
publish_mode = "direct"
github_token = "test-only-token"
test_commands = [["sh", "-c", 'test "$(cat src/app.py)" = fixed']]
[repositories.execution]
max_attempts = 0
retry_delay_seconds = 0
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self._queue(config, baseline, target, fingerprint_character="c")
            self._queue(config, baseline, target, fingerprint_character="d")
            agent = RetryAgent()
            worker = LocalWorker(config, CommandRunner(), agent)

            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                jobs = state.jobs()
                events = state.events(jobs[1].id)

        self.assertEqual(jobs[1].status, "completed")
        self.assertEqual(jobs[1].attempts, 1)
        self.assertEqual(jobs[0].status, "queued")
        self.assertEqual(agent.validation_calls, 1)
        self.assertEqual(agent.fix_calls, 2)
        self.assertTrue(agent.saw_existing_diff)
        self.assertEqual(agent.workspaces[0], agent.workspaces[1])
        self.assertIn("fix_iteration_failed", [event.event_type for event in events])
        self.assertNotIn("retry_scheduled", [event.event_type for event in events])
        self.assertEqual(
            [event.event_type for event in events].count("worktree_created"), 1
        )
        self.assertEqual(
            [event.event_type for event in events].count("worktree_removed"), 1
        )

    def test_review_batch_shares_calls_and_pushes_one_commit_per_file_group(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, app_target = WorkspaceTest._repository(root)
            (repository_path / "src/other.py").write_text(
                "other defect\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "src/other.py"], cwd=repository_path, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "second target"],
                cwd=repository_path,
                check=True,
                capture_output=True,
            )
            target = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=repository_path,
                check=True,
                capture_output=True,
            )
            config_path = root / "fix.toml"
            config_path.write_text(
                f"""
version = 1
state_dir = ".state"
[server]
token = "test-token"
[[repositories]]
id = "repo"
github = "owner/repo"
target_branch = "main"
local_path = "{repository_path}"
publish_mode = "direct"
processing_mode = "review_batch"
github_token = "test-only-token"
test_commands = []
git_author_name = "broken-agent"
git_author_email = "g_uapm@inswave.com"
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            findings = [
                {
                    "fingerprint": "sha256:" + character * 64,
                    "severity": "Major",
                    "commit": introducing_commit,
                    "file": file,
                    "line": 1,
                    "cause": f"Defect {character}",
                    "solution": f"Fix {character}",
                }
                for character, file, introducing_commit in (
                    ("c", "src/app.py", app_target),
                    ("d", "src/app.py", app_target),
                    ("e", "src/other.py", target),
                )
            ]
            review = parse_review_event(
                {
                    "version": 1,
                    "repository": "owner/repo",
                    "branch": "main",
                    "baseline": baseline,
                    "target": target,
                    "findings": findings,
                }
            )
            with StateStore(config.state_dir) as state:
                intake = state.accept(config.repositories[0], review)
            agent = BatchAgent()
            worker = LocalBatchPushWorker(config, CommandRunner(), agent)

            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                jobs = sorted(state.jobs(), key=lambda item: item.id)
                batch_run = state.batch_run(intake.batch_id)
                batch_events = state.events()

            remote_count = int(
                subprocess.run(
                    ["git", "rev-list", "--count", f"{target}..origin/main"],
                    cwd=repository_path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )

        self.assertEqual([job.status for job in jobs], ["completed"] * 3)
        self.assertEqual(agent.validation_calls, 1)
        self.assertEqual(agent.fix_calls, 1)
        self.assertEqual(agent.result_validation_calls, 1)
        self.assertEqual(jobs[0].result_commit, jobs[1].result_commit)
        self.assertNotEqual(jobs[1].result_commit, jobs[2].result_commit)
        self.assertEqual(worker.pushed_branches, ["main", "main"])
        self.assertEqual(remote_count, 2)
        self.assertEqual(batch_run.status, "completed")
        self.assertEqual(batch_run.codex_calls, 3)
        self.assertEqual(batch_run.total_tokens, 120)
        self.assertEqual(
            sum(event.event_type == "worktree_created" for event in batch_events),
            1,
        )

    def test_review_batch_revalidates_all_results_after_target_moves(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository_path, baseline, target = WorkspaceTest._repository(root)
            config_path = root / "fix.toml"
            config_path.write_text(
                f"""
version = 1
state_dir = ".state"
[server]
token = "test-token"
[[repositories]]
id = "repo"
github = "owner/repo"
target_branch = "main"
local_path = "{repository_path}"
publish_mode = "direct"
processing_mode = "review_batch"
github_token = "test-only-token"
test_commands = []
""",
                encoding="utf-8",
            )
            config = load_config(config_path)
            self._queue(config, baseline, target)
            agent = MovingBatchAgent(repository_path)
            worker = LocalBatchPushWorker(config, CommandRunner(), agent)

            self.assertTrue(worker.run_once())
            with StateStore(config.state_dir) as state:
                completed = state.jobs()[0]
                event_types = [
                    item.event_type for item in state.events(completed.id)
                ]

        self.assertEqual(completed.status, "completed")
        self.assertEqual(agent.result_validation_calls, 2)
        self.assertIn("target_moved", event_types)
        self.assertIn("target_merged", event_types)
        self.assertIn("merged_fix_revalidated", event_types)

    @staticmethod
    def _queue(
        config: object,
        baseline: str,
        target: str,
        *,
        fingerprint_character: str = "c",
    ) -> None:
        event = parse_review_event(
            {
                "version": 1,
                "repository": "owner/repo",
                "branch": "main",
                "baseline": baseline,
                "target": target,
                "findings": [
                    {
                        "fingerprint": "sha256:" + fingerprint_character * 64,
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
