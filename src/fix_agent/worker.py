from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import threading

from .codex_agent import CodexAgent
from .command import CommandRunner
from .config import AppConfig, RepositoryConfig
from .errors import FixAgentError
from .notify import DiscordNotifier
from .state import Job, StateStore
from .workspace import FixWorkspace


class FixWorker:
    def __init__(
        self,
        config: AppConfig,
        runner: CommandRunner | None = None,
        agent: CodexAgent | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.agent = agent or CodexAgent(self.runner, config.codex_executable)
        self.notifier = DiscordNotifier(config)
        self.notifier.initialize_cursors()
        self._stop = threading.Event()

    def run_once(self) -> bool:
        with StateStore(self.config.state_dir) as state:
            job = state.claim_next(self.config.repositories)
        if job is None:
            self._dispatch_notifications()
            return False
        try:
            self._process(job)
        except Exception as exc:
            with StateStore(self.config.state_dir) as state:
                state.mark_failed(job.id, str(exc))
            print(f"job {job.id} failed: {exc}")
        self._dispatch_notifications()
        return True

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _dispatch_notifications(self) -> None:
        try:
            result = self.notifier.dispatch_pending()
        except Exception as exc:
            print(f"Discord notification dispatch failed: {exc}")
            return
        if result.failed:
            print("Discord notification delivery failed; retry is scheduled")

    def _process(self, job: Job) -> None:
        repository = self.config.repository_by_id(job.repository_id)
        self._event(
            job.id,
            "processing_started",
            "worker started processing the finding",
            {
                "remote": repository.remote,
                "target_branch": repository.target_branch,
                "publish_mode": repository.publish_mode,
            },
        )
        if job.fix_branch and job.result_commit:
            if repository.publish_mode == "direct":
                with StateStore(self.config.state_dir) as state:
                    cleanup_events = [
                        event
                        for event in state.events(job.id)
                        if event.event_type
                        in {"worktree_removed", "worktree_cleanup_incomplete"}
                    ]
                    if (
                        not cleanup_events
                        or cleanup_events[-1].event_type != "worktree_removed"
                    ):
                        raise FixAgentError(
                            "direct push succeeded but worktree cleanup requires "
                            "manual reconciliation"
                        )
                    state.mark_completed(job.id, None)
                return
            pr_url = self._publish_pull_request(repository, job, job.fix_branch)
            with StateStore(self.config.state_dir) as state:
                state.mark_completed(job.id, pr_url)
            return

        with FixWorkspace(
            self.runner, repository, job, self.config.state_dir
        ) as workspace:
            environment = workspace.safe_environment
            mismatch = workspace.finding_mismatch_reason()
            if mismatch:
                with StateStore(self.config.state_dir) as state:
                    state.record_precheck(job.id, False, mismatch)
                print(f"job {job.id} rejected: {mismatch}")
                return
            self._event(
                job.id,
                "finding_git_validated",
                "finding commit, file, and reviewed line matched the review diff",
                {"reviewed_target": job.target_commit},
            )
            decision = self.agent.validate_finding(
                repository, job, workspace.path, environment, workspace.base_commit
            )
            with StateStore(self.config.state_dir) as state:
                state.record_precheck(job.id, decision.valid, decision.reason)
            if not decision.valid:
                print(f"job {job.id} rejected: {decision.reason}")
                return

            self.agent.apply_fix(
                repository, job, workspace.path, environment, workspace.base_commit
            )
            self._event(
                job.id,
                "fix_applied",
                "Codex finished the initial workspace edit",
                {"workspace_base": workspace.base_commit},
            )
            summary = workspace.validate_diff()
            self._event(
                job.id,
                "diff_validated",
                "initial diff passed repository policy",
                {
                    "files": list(summary.files),
                    "added_lines": summary.added_lines,
                    "deleted_lines": summary.deleted_lines,
                },
            )
            workspace.stage_for_harness()
            with StateStore(self.config.state_dir) as state:
                state.mark_testing(job.id)
            tests = self._run_tests(repository, workspace.path, environment)
            with StateStore(self.config.state_dir) as state:
                state.record_tests(job.id, tests)
            failed = [test for test in tests if test["returncode"] != 0]
            if failed:
                raise FixAgentError("configured test command failed")
            postcheck = self.agent.validate_fix(
                repository, job, workspace.path, environment, workspace.base_commit
            )
            with StateStore(self.config.state_dir) as state:
                state.record_postcheck(job.id, postcheck.valid, postcheck.reason)
            if not postcheck.valid:
                print(f"job {job.id} fix rejected: {postcheck.reason}")
                return
            workspace.validate_diff()
            branch, commit = workspace.commit()
            self._event(
                job.id,
                "fix_committed",
                "validated fix was committed in the worktree",
                {"branch": branch, "commit": commit},
            )
            remote_merges = 0
            while True:
                current = workspace.fetch_target_head()
                if current != workspace.base_commit:
                    if remote_merges >= repository.max_remote_merge_attempts:
                        raise FixAgentError(
                            "target branch kept moving during fix; merge limit reached"
                        )
                    self._event(
                        job.id,
                        "target_moved",
                        "target branch moved; merging the latest target",
                        {
                            "previous_base": workspace.base_commit,
                            "current_target": current,
                            "merge_attempt": remote_merges + 1,
                        },
                    )
                    merge = workspace.merge_latest_target(current)
                    remote_merges += 1
                    if merge.conflict_files:
                        self._event(
                            job.id,
                            "merge_conflict_detected",
                            "target merge produced conflicts",
                            {
                                "previous_base": merge.previous_base,
                                "current_target": merge.current_target,
                                "files": list(merge.conflict_files),
                            },
                        )
                        resolution = self.agent.resolve_merge_conflicts(
                            repository,
                            job,
                            workspace.path,
                            environment,
                            merge.previous_base,
                            merge.current_target,
                            merge.conflict_files,
                        )
                        self._event(
                            job.id,
                            "merge_conflict_decided",
                            "Codex returned a merge conflict decision",
                            {
                                "resolved": resolution.valid,
                                "reason": resolution.reason,
                                "files": list(merge.conflict_files),
                            },
                        )
                        if not resolution.valid:
                            raise FixAgentError(
                                "Codex could not safely resolve merge conflicts: "
                                + resolution.reason
                            )
                        workspace.complete_conflicted_merge(merge.current_target)
                        self._event(
                            job.id,
                            "merge_conflict_resolved",
                            "merge conflicts were resolved and committed",
                            {
                                "current_target": merge.current_target,
                                "reason": resolution.reason,
                                "commit": workspace.head_commit(),
                            },
                        )
                    else:
                        self._event(
                            job.id,
                            "target_merged",
                            "latest target branch was merged without conflicts",
                            {
                                "previous_base": merge.previous_base,
                                "current_target": merge.current_target,
                                "commit": workspace.head_commit(),
                            },
                        )
                    summary = workspace.validate_diff()
                    with StateStore(self.config.state_dir) as state:
                        state.mark_testing(job.id)
                    tests = self._run_tests(repository, workspace.path, environment)
                    with StateStore(self.config.state_dir) as state:
                        state.record_tests(job.id, tests)
                    failed = [test for test in tests if test["returncode"] != 0]
                    if failed:
                        raise FixAgentError(
                            "configured test command failed after target merge"
                        )
                    workspace.require_clean_checkout()
                    postcheck = self.agent.validate_fix(
                        repository,
                        job,
                        workspace.path,
                        environment,
                        workspace.base_commit,
                    )
                    with StateStore(self.config.state_dir) as state:
                        state.record_postcheck(
                            job.id, postcheck.valid, postcheck.reason
                        )
                    if not postcheck.valid:
                        print(
                            f"job {job.id} merged fix rejected: {postcheck.reason}"
                        )
                        return
                    commit = workspace.head_commit()
                    self._event(
                        job.id,
                        "merged_fix_revalidated",
                        "merged fix passed policy, harness, and result validation",
                        {
                            "target_commit": workspace.base_commit,
                            "result_commit": commit,
                            "merge_attempt": remote_merges,
                        },
                    )
                    continue
                try:
                    self._event(
                        job.id,
                        "push_started",
                        "pushing the worktree result to the configured remote",
                        {"remote": repository.remote, "branch": branch},
                    )
                    self._push(repository, workspace.path, environment, branch)
                except FixAgentError as push_error:
                    current = workspace.fetch_target_head()
                    if (
                        repository.publish_mode == "direct"
                        and current == workspace.head_commit()
                    ):
                        commit = current
                        break
                    if current != workspace.base_commit:
                        self._event(
                            job.id,
                            "push_retry_after_target_move",
                            "push raced with a target update; merge will be retried",
                            {
                                "previous_base": workspace.base_commit,
                                "current_target": current,
                                "error": str(push_error),
                            },
                        )
                        continue
                    raise push_error
                commit = workspace.head_commit()
                break
            self._event(
                job.id,
                "push_completed",
                "worktree result was pushed to the configured remote",
                {"remote": repository.remote, "branch": branch, "commit": commit},
            )
            with StateStore(self.config.state_dir) as state:
                state.mark_pushed(job.id, branch, commit)
            job = Job(
                **{
                    **job.__dict__,
                    "fix_branch": branch,
                    "result_commit": commit,
                    "precheck_status": "valid",
                    "precheck_reason": decision.reason,
                    "postcheck_status": "resolved",
                    "postcheck_reason": postcheck.reason,
                    "tests_json": json.dumps(tests, ensure_ascii=False),
                }
            )
            print(
                f"job {job.id} changed {len(summary.files)} file(s), "
                f"{summary.added_lines + summary.deleted_lines} line(s)"
            )
        if not workspace.cleanup_complete:
            raise FixAgentError("worktree cleanup did not complete after push")
        if repository.publish_mode == "direct":
            with StateStore(self.config.state_dir) as state:
                state.mark_completed(job.id, None)
            print(
                f"job {job.id} completed: "
                f"{repository.remote}/{repository.target_branch}"
            )
            return
        pr_url = self._publish_pull_request(repository, job, branch)
        with StateStore(self.config.state_dir) as state:
            state.mark_completed(job.id, pr_url)
        print(f"job {job.id} completed: {pr_url}")

    def _event(
        self,
        job_id: int,
        event_type: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        with StateStore(self.config.state_dir) as state:
            state.record_event(job_id, event_type, message, details)

    def _run_tests(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
    ) -> list[dict[str, object]]:
        results = []
        for command in repository.test_commands:
            result = self.runner.run(
                command,
                cwd=workspace,
                environment=environment,
                timeout_seconds=repository.command_timeout_seconds,
                check=False,
            )
            results.append(
                {
                    "command": list(command),
                    "returncode": result.returncode,
                    "stdout": result.stdout[-20_000:],
                    "stderr": result.stderr[-20_000:],
                }
            )
        return results

    def _push(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
        branch: str,
    ) -> None:
        token = os.environ.get(repository.github_token_env)
        if not token:
            raise FixAgentError(
                f"required environment variable is not set: {repository.github_token_env}"
            )
        push_environment = dict(environment)
        expected_urls = {
            f"https://github.com/{repository.github}",
            f"https://github.com/{repository.github}.git",
        }
        remote_url = self.runner.run(
            ["git", "remote", "get-url", repository.remote],
            cwd=workspace,
            environment=environment,
        ).stdout.strip()
        if remote_url not in expected_urls:
            raise FixAgentError(
                f"Git remote does not match configured GitHub repository: {remote_url}"
            )
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        push_environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
        command = ["git", "push"]
        if repository.publish_mode == "pull_request":
            command.append("--set-upstream")
        command.extend(
            [repository.remote, f"HEAD:refs/heads/{branch}"]
        )
        self.runner.run(
            command,
            cwd=workspace,
            environment=push_environment,
            timeout_seconds=repository.command_timeout_seconds,
        )

    def _publish_pull_request(
        self, repository: RepositoryConfig, job: Job, branch: str
    ) -> str:
        token = os.environ.get(repository.github_token_env)
        if not token:
            raise FixAgentError(
                f"required environment variable is not set: {repository.github_token_env}"
            )
        environment = os.environ.copy()
        environment["GH_TOKEN"] = token
        existing = self.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository.github,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url",
                "--limit",
                "1",
            ],
            environment=environment,
            timeout_seconds=repository.command_timeout_seconds,
        )
        try:
            matches = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise FixAgentError("gh pr list returned invalid JSON") from exc
        if matches:
            return matches[0]["url"]
        title = f"fix: resolve review finding {job.fingerprint[-12:]}"
        body = (
            "## Review finding\n\n"
            f"- Fingerprint: `{job.fingerprint}`\n"
            f"- Reviewed target: `{job.target_commit}`\n"
            f"- Location: `{job.file}:{job.line}`\n"
            f"- Independent validation: {job.precheck_reason}\n"
            f"- Fix validation: {job.postcheck_reason}\n"
        )
        created = self.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository.github,
                "--base",
                repository.branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            environment=environment,
            timeout_seconds=repository.command_timeout_seconds,
        )
        url = created.stdout.strip()
        if not url:
            raise FixAgentError("gh pr create returned no URL")
        return url
