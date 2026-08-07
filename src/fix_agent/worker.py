from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import threading

from .codex_agent import CodexAgent, Decision
from .command import CommandResult, CommandRunner
from .config import AppConfig, RepositoryConfig
from .crontrol import CrontrolReporter
from .credentials import resolve_github_credential
from .errors import EnvironmentSetupError, FixAgentError
from .notify import DiscordNotifier
from .state import Job, StateStore
from .workspace import DiffSummary, FixWorkspace, reconcile_recorded_worktree


_CRONTROL_EVENT_STAGES = {
    "processing_started": "작업 준비",
    "finding_git_validated": "Git 검증 완료",
    "environment_setup_started": "환경 준비 중",
    "environment_setup_failed": "환경 준비 재시도 중",
    "environment_setup_completed": "환경 준비 완료",
    "finding_validation_started": "finding 검증 중",
    "finding_validation_completed": "finding 검증 완료",
    "fix_started": "수정 중",
    "fix_applied": "수정안 생성 완료",
    "fix_iteration_failed": "검증 실패 보완 중",
    "retry_scheduled": "재시도 대기",
    "diff_validated": "변경 정책 검증 완료",
    "tests_started": "테스트 중",
    "result_validation_started": "수정 결과 검증 중",
    "result_validation_completed": "수정 결과 검증 완료",
    "fix_committed": "커밋 완료",
    "target_moved": "원격 target 병합 중",
    "merge_conflict_detected": "merge 충돌 해결 중",
    "merge_conflict_resolved": "merge 충돌 해결 완료",
    "target_merged": "원격 target 병합 완료",
    "merged_fix_revalidated": "병합 결과 재검증 완료",
    "push_started": "push 중",
    "push_completed": "push 완료",
}


@dataclass
class _SetupState:
    signature: str | None = None


class FixWorker:
    def __init__(
        self,
        config: AppConfig,
        runner: CommandRunner | None = None,
        agent: CodexAgent | None = None,
        crontrol: CrontrolReporter | None = None,
        notifier: DiscordNotifier | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.agent = agent or CodexAgent(self.runner, config.codex_executable)
        self.notifier = notifier or DiscordNotifier(config)
        self.crontrol = crontrol or CrontrolReporter(config)
        self.notifier.initialize_cursors()
        self._stop = threading.Event()
        self._current_job_id: int | None = None

    def run_once(self) -> bool:
        with StateStore(self.config.state_dir) as state:
            job = state.claim_next(self.config.repositories)
        if job is None:
            self.crontrol.sync(None)
            self._dispatch_notifications()
            return False
        self._current_job_id = job.id
        self.crontrol.sync(job.id, "작업 준비")
        try:
            self._process(job)
        except Exception as exc:
            repository = self.config.repository_by_id(job.repository_id)
            retryable = (
                repository.max_attempts == 0
                or job.attempts < repository.max_attempts
            )
            with StateStore(self.config.state_dir) as state:
                next_attempt_at = state.mark_failed(
                    job.id,
                    str(exc),
                    repository.retry_delay_seconds if retryable else None,
                )
                if next_attempt_at is not None:
                    state.record_event(
                        job.id,
                        "retry_scheduled",
                        "failed attempt will be retried before later jobs",
                        {
                            "attempt": job.attempts,
                            "max_attempts": repository.max_attempts,
                            "next_attempt_at": next_attempt_at,
                            "error": str(exc)[:4_000],
                        },
                    )
            if next_attempt_at is not None:
                self.crontrol.sync(job.id, "재시도 대기")
            print(f"job {job.id} failed: {exc}")
        finally:
            self.crontrol.sync(job.id)
            self._dispatch_notifications()
            self._current_job_id = None
            self.crontrol.sync(None)
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
                    worktree_events = [
                        event
                        for event in state.events(job.id)
                        if event.event_type == "worktree_created"
                    ]
                if (
                    not cleanup_events
                    or cleanup_events[-1].event_type != "worktree_removed"
                ):
                    if not worktree_events:
                        raise FixAgentError(
                            "pushed job has no recorded worktree path for cleanup"
                        )
                    details = json.loads(worktree_events[-1].details_json)
                    path = details.get("path") if isinstance(details, dict) else None
                    if not isinstance(path, str) or not path:
                        raise FixAgentError(
                            "pushed job has an invalid recorded worktree path"
                        )
                    if not reconcile_recorded_worktree(
                        self.runner,
                        repository,
                        self.config.state_dir,
                        job.id,
                        path,
                    ):
                        raise FixAgentError(
                            "direct push succeeded but worktree cleanup is incomplete"
                        )
                with StateStore(self.config.state_dir) as state:
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
            setup_state = _SetupState()
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
            self._prepare_environment(
                repository, job, workspace, environment, setup_state
            )
            if job.precheck_status == "valid" and job.precheck_reason:
                decision = Decision(True, job.precheck_reason)
                with StateStore(self.config.state_dir) as state:
                    state.record_precheck(job.id, True, decision.reason)
                self._event(
                    job.id,
                    "finding_validation_reused",
                    "previous valid finding decision was retained for the retry",
                    {"reason": decision.reason, "attempt": job.attempts},
                )
            else:
                self._event(
                    job.id,
                    "finding_validation_started",
                    "Codex started independent finding validation",
                    {"workspace_base": workspace.base_commit},
                    notify=True,
                )
                decision = self.agent.validate_finding(
                    repository,
                    job,
                    workspace.path,
                    environment,
                    workspace.base_commit,
                )
                with StateStore(self.config.state_dir) as state:
                    state.record_precheck(job.id, decision.valid, decision.reason)
                self._event(
                    job.id,
                    "finding_validation_completed",
                    "Codex completed independent finding validation",
                    {"valid": decision.valid, "reason": decision.reason},
                    notify=True,
                )
                if not decision.valid:
                    print(f"job {job.id} rejected: {decision.reason}")
                    return

            summary, tests, postcheck = self._fix_until_valid(
                repository, job, workspace, environment, setup_state
            )
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
                    self._prepare_environment(
                        repository, job, workspace, environment, setup_state
                    )
                    with StateStore(self.config.state_dir) as state:
                        state.mark_testing(job.id)
                    tests = self._run_tests(repository, workspace.path, environment)
                    with StateStore(self.config.state_dir) as state:
                        state.record_tests(job.id, tests)
                    failed = [test for test in tests if test["returncode"] != 0]
                    if failed:
                        raise FixAgentError(
                            _test_failure_error(failed, after_target_merge=True)
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
                        raise FixAgentError(
                            "merged fix did not pass independent validation: "
                            + postcheck.reason
                        )
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
        *,
        notify: bool = False,
    ) -> None:
        with StateStore(self.config.state_dir) as state:
            state.record_event(job_id, event_type, message, details)
        stage = _CRONTROL_EVENT_STAGES.get(event_type)
        if stage is not None:
            self.crontrol.sync(job_id, stage)
        if notify:
            self._dispatch_notifications()

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

    def _prepare_environment(
        self,
        repository: RepositoryConfig,
        job: Job,
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> None:
        if not repository.setup_commands:
            return
        signature = workspace.setup_signature(repository.setup_watch_paths)
        if setup_state.signature == signature:
            return
        head = workspace.head_commit()
        before = workspace.working_tree_fingerprint()
        snapshots = workspace.snapshot_working_changes()
        self._event(
            job.id,
            "environment_setup_started",
            "대상 저장소 의존성·실행 환경 준비 시작",
            {
                "commands": len(repository.setup_commands),
                "watch_paths": list(repository.setup_watch_paths),
            },
        )
        for attempt in range(1, repository.setup_max_attempts + 1):
            failure: tuple[int, tuple[str, ...], CommandResult] | None = None
            for index, command in enumerate(repository.setup_commands, start=1):
                result = self.runner.run(
                    command,
                    cwd=workspace.path,
                    environment=environment,
                    timeout_seconds=repository.command_timeout_seconds,
                    check=False,
                )
                if result.returncode != 0:
                    failure = (index, command, result)
                    break
            if failure is None:
                if workspace.head_commit() != head:
                    raise EnvironmentSetupError(
                        "environment setup changed the worktree HEAD"
                    )
                restored = workspace.restore_working_changes(snapshots)
                after = workspace.working_tree_fingerprint()
                if after != before:
                    raise EnvironmentSetupError(
                        "environment setup changes could not be isolated from the fix"
                    )
                setup_state.signature = workspace.setup_signature(
                    repository.setup_watch_paths
                )
                self._event(
                    job.id,
                    "environment_setup_completed",
                    "대상 저장소 의존성·실행 환경 준비 완료",
                    {
                        "attempt": attempt,
                        "commands": len(repository.setup_commands),
                        "restored_paths": list(restored),
                    },
                )
                return
            index, command, result = failure
            will_retry = attempt < repository.setup_max_attempts
            detail = (result.stderr or result.stdout or "command failed").strip()
            self._event(
                job.id,
                "environment_setup_failed",
                (
                    "환경 준비 명령 실패, 같은 worktree에서 재시도 예정"
                    if will_retry
                    else "환경 준비 명령 최종 실패"
                ),
                {
                    "attempt": attempt,
                    "max_attempts": repository.setup_max_attempts,
                    "command_index": index,
                    "command": list(command),
                    "returncode": result.returncode,
                    "stdout": result.stdout[-20_000:],
                    "stderr": result.stderr[-20_000:],
                    "will_retry": will_retry,
                },
            )
            if not will_retry:
                raise EnvironmentSetupError(
                    f"environment setup command failed: {command[0]}: {detail}"
                )
            if self._stop.wait(repository.setup_retry_delay_seconds):
                raise EnvironmentSetupError(
                    "environment setup stopped before the next retry"
                )

    def _fix_until_valid(
        self,
        repository: RepositoryConfig,
        job: Job,
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> tuple[DiffSummary, list[dict[str, object]], Decision]:
        iteration = 1
        retry_error = job.last_error
        retry_tests = job.tests_json
        while True:
            iteration_job = replace(
                job,
                attempts=job.attempts + iteration - 1,
                last_error=retry_error,
                tests_json=retry_tests,
            )
            retrying = bool(retry_error)
            self._event(
                job.id,
                "fix_started",
                (
                    f"{iteration}차 보완 수정 시작"
                    if retrying
                    else "검증된 finding 수정 시작"
                ),
                {
                    "workspace_base": workspace.base_commit,
                    "iteration": iteration,
                    "same_worktree": iteration > 1,
                    "previous_error": retry_error[:4_000] if retry_error else None,
                },
                notify=True,
            )
            tests: list[dict[str, object]] = []
            try:
                self.agent.apply_fix(
                    repository,
                    iteration_job,
                    workspace.path,
                    environment,
                    workspace.base_commit,
                )
                self._event(
                    job.id,
                    "fix_applied",
                    f"{iteration}차 수정안 생성 완료, 정책·하네스 검증 시작",
                    {
                        "workspace_base": workspace.base_commit,
                        "iteration": iteration,
                    },
                    notify=True,
                )
                self._prepare_environment(
                    repository, job, workspace, environment, setup_state
                )
                summary = workspace.validate_diff()
                self._event(
                    job.id,
                    "diff_validated",
                    "수정안이 저장소 변경 정책 통과",
                    {
                        "files": list(summary.files),
                        "added_lines": summary.added_lines,
                        "deleted_lines": summary.deleted_lines,
                        "iteration": iteration,
                    },
                )
                workspace.stage_for_harness()
                with StateStore(self.config.state_dir) as state:
                    state.mark_testing(job.id)
                self._event(
                    job.id,
                    "tests_started",
                    "설정된 저장소 하네스 실행 시작",
                    {
                        "commands": len(repository.test_commands),
                        "iteration": iteration,
                    },
                )
                tests = self._run_tests(repository, workspace.path, environment)
                with StateStore(self.config.state_dir) as state:
                    state.record_tests(job.id, tests)
                failed = [test for test in tests if test["returncode"] != 0]
                if failed:
                    raise FixAgentError(_test_failure_error(failed))
                self._event(
                    job.id,
                    "result_validation_started",
                    "수정 결과 독립 검증 시작",
                    {"workspace_base": workspace.base_commit, "iteration": iteration},
                )
                postcheck = self.agent.validate_fix(
                    repository,
                    iteration_job,
                    workspace.path,
                    environment,
                    workspace.base_commit,
                )
                with StateStore(self.config.state_dir) as state:
                    state.record_postcheck(
                        job.id,
                        postcheck.valid,
                        postcheck.reason,
                        retry_on_failure=True,
                    )
                self._event(
                    job.id,
                    "result_validation_completed",
                    "수정 결과 독립 검증 완료",
                    {
                        "valid": postcheck.valid,
                        "reason": postcheck.reason,
                        "iteration": iteration,
                    },
                )
                if not postcheck.valid:
                    raise FixAgentError(
                        "completed fix did not pass independent validation: "
                        + postcheck.reason
                    )
                workspace.validate_diff()
                return summary, tests, postcheck
            except EnvironmentSetupError:
                raise
            except FixAgentError as exc:
                total_attempt = job.attempts + iteration - 1
                if (
                    repository.max_attempts != 0
                    and total_attempt >= repository.max_attempts
                ):
                    raise
                retry_error = str(exc)
                if tests:
                    retry_tests = json.dumps(tests, ensure_ascii=False)
                with StateStore(self.config.state_dir) as state:
                    state.record_fix_iteration_failure(job.id, retry_error, tests)
                self._event(
                    job.id,
                    "fix_iteration_failed",
                    "검증 실패 내용을 반영해 같은 worktree에서 수정 계속",
                    {
                        "iteration": iteration,
                        "next_iteration": iteration + 1,
                        "error": retry_error[:4_000],
                        "same_worktree": True,
                        "path": str(workspace.path),
                    },
                    notify=True,
                )
                iteration += 1

    def _push(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
        branch: str,
    ) -> None:
        token = self._github_token(repository)
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
        token = self._github_token(repository)
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

    def _github_token(self, repository: RepositoryConfig) -> str:
        resolved = resolve_github_credential(
            repository.github_token,
            repository.github_token_env,
            self.runner,
        )
        if resolved is None:  # pragma: no cover - required resolver contract
            raise FixAgentError("GitHub authentication is unavailable")
        return resolved


def _test_failure_error(
    failed: list[dict[str, object]], *, after_target_merge: bool = False
) -> str:
    phase = " after target merge" if after_target_merge else ""
    lines = [f"configured test command failed{phase}:"]
    for test in failed:
        command = " ".join(str(part) for part in test.get("command", []))
        output = str(test.get("stderr") or test.get("stdout") or "no output").strip()
        lines.append(
            f"- {command} (exit {test.get('returncode')}): {output[-2_000:]}"
        )
    return "\n".join(lines)[:10_000]
