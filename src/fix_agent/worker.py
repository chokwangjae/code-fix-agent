from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

from .codex_agent import BatchChangeGroup, BatchFixDecision, CodexAgent, Decision
from .command import CommandResult, CommandRunner
from .config import AppConfig, RepositoryConfig
from .crontrol import CrontrolReporter
from .credentials import resolve_github_credential
from .errors import (
    EnvironmentSetupError,
    FixAgentError,
    JobTerminalError,
    JobTimeBudgetExceeded,
)
from .notify import DiscordNotifier
from .state import BatchClaim, Job, PublishCheckpoint, StateStore
from .workspace import (
    DiffSummary,
    FixWorkspace,
    PermissionSummary,
)


_CRONTROL_EVENT_STAGES = {
    "processing_started": "작업 준비",
    "batch_processing_started": "리뷰 배치 준비",
    "batch_validation_started": "리뷰 배치 검증 중",
    "batch_validation_completed": "리뷰 배치 검증 완료",
    "batch_fix_started": "리뷰 배치 수정 중",
    "batch_fallback_started": "문제 finding 분리 중",
    "batch_metrics_recorded": "리뷰 배치 사용량 기록",
    "finding_git_validated": "Git 검증 완료",
    "environment_setup_started": "환경 준비 중",
    "environment_setup_failed": "환경 준비 재시도 중",
    "environment_setup_completed": "환경 준비 완료",
    "worktree_permissions_repaired": "worktree 권한 복구 완료",
    "finding_validation_started": "finding 검증 중",
    "finding_validation_completed": "finding 검증 완료",
    "fix_started": "수정 중",
    "fix_applied": "수정안 생성 완료",
    "fix_iteration_failed": "검증 실패 보완 중",
    "retry_scheduled": "재시도 대기",
    "diff_validated": "변경 정책 검증 완료",
    "tests_started": "테스트 중",
    "tests_conditional_pass": "OS 차이 조건부 통과",
    "result_validation_started": "수정 결과 검증 중",
    "result_validation_completed": "수정 결과 검증 완료",
    "fix_committed": "커밋 완료",
    "publish_checkpoint_recorded": "push 준비 완료",
    "publish_retry_resumed": "push 재시도 중",
    "target_moved": "원격 target 병합 중",
    "merge_conflict_detected": "merge 충돌 해결 중",
    "merge_conflict_resolved": "merge 충돌 해결 완료",
    "target_merged": "원격 target 병합 완료",
    "merged_fix_revalidated": "병합 결과 재검증 완료",
    "push_started": "push 중",
    "push_completed": "push 완료",
}

_PUBLISH_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_PUBLISH_LOCKS_GUARD = threading.Lock()


def _publish_lock(repository: RepositoryConfig) -> threading.RLock:
    key = (repository.github.casefold(), repository.target_branch)
    with _PUBLISH_LOCKS_GUARD:
        return _PUBLISH_LOCKS.setdefault(key, threading.RLock())


def _serialized_publish(repository_index: int):
    def decorate(method):
        @wraps(method)
        def locked(self, *args, **kwargs):
            repository = args[repository_index]
            with _publish_lock(repository):
                return method(self, *args, **kwargs)

        return locked

    return decorate


@dataclass
class _SetupState:
    signature: str | None = None


@dataclass(frozen=True)
class _PendingFallback:
    jobs: tuple[Job, ...]
    reason: str


class _BatchCorrectionRequired(FixAgentError):
    def __init__(self, message: str, groups: tuple[BatchChangeGroup, ...]) -> None:
        super().__init__(message)
        self.groups = groups


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
        self._deadline: datetime | None = None

    def run_once(self) -> bool:
        with StateStore(self.config.state_dir) as state:
            if state.worker_control().paused:
                batch = None
                job = None
            else:
                claim_kind = state.next_claim_kind(self.config.repositories)
                if claim_kind == "batch":
                    batch = state.claim_next_batch(self.config.repositories)
                    job = None
                elif claim_kind == "finding":
                    batch = None
                    job = state.claim_next(self.config.repositories)
                else:
                    batch = None
                    job = None
        if batch is not None:
            return self._run_batch(batch)
        if job is None:
            self.crontrol.sync(None)
            self._dispatch_notifications()
            return False
        repository = self.config.repository_by_id(job.repository_id)
        self._current_job_id = job.id
        self.crontrol.sync(job.id, "작업 준비")
        try:
            self._start_budget(
                repository, None if job.result_commit else job.execution_started_at
            )
            self._process(job)
        except Exception as exc:
            retryable = not isinstance(exc, JobTerminalError) and (
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
            self._deadline = None
            self.crontrol.sync(None)
        return True

    def _run_batch(self, batch: BatchClaim) -> bool:
        primary = batch.jobs[0]
        repository = self.config.repository_by_id(primary.repository_id)
        self._current_job_id = primary.id
        self.crontrol.sync(primary.id, "리뷰 배치 준비")
        try:
            self._start_budget(
                repository,
                None if any(job.result_commit for job in batch.jobs) else batch.started_at,
            )
            self._process_batch(batch, repository)
        except Exception as exc:
            retryable = not isinstance(exc, JobTerminalError) and (
                repository.max_attempts == 0
                or batch.attempt < repository.max_attempts
            )
            next_attempt_at: str | None = None
            with StateStore(self.config.state_dir) as state:
                for claimed in batch.jobs:
                    current = state.job(claimed.id)
                    if current is None or current.status in {
                        "completed",
                        "rejected",
                        "skipped",
                        "queued",
                        "fallback_pending",
                    }:
                        continue
                    scheduled = state.mark_failed(
                        current.id,
                        str(exc),
                        repository.retry_delay_seconds if retryable else None,
                    )
                    next_attempt_at = next_attempt_at or scheduled
                state.mark_batch_failed(batch.id, str(exc))
            if next_attempt_at is not None:
                self.crontrol.sync(primary.id, "재시도 대기")
            print(f"batch {batch.id} failed: {exc}")
        finally:
            self.crontrol.sync(primary.id)
            self._dispatch_notifications()
            self._current_job_id = None
            self._deadline = None
            self.crontrol.sync(None)
        return True

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while not self._stop.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:
                print(f"worker loop recovered from an unexpected error: {exc}")
                processed = False
            if not processed:
                self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _start_budget(
        self, repository: RepositoryConfig, started_at: str | None
    ) -> None:
        started = (
            datetime.fromisoformat(started_at)
            if started_at
            else datetime.now(timezone.utc)
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        self._deadline = started + timedelta(seconds=repository.job_timeout_seconds)
        self._remaining_timeout(repository.job_timeout_seconds)

    def _remaining_timeout(self, maximum: int) -> int:
        if self._deadline is None:
            return maximum
        remaining = (self._deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise JobTimeBudgetExceeded("job execution time budget was exhausted")
        return min(maximum, max(1, math.ceil(remaining)))

    def _codex_repository(
        self, repository: RepositoryConfig
    ) -> RepositoryConfig:
        return replace(
            repository,
            command_timeout_seconds=self._remaining_timeout(
                repository.codex_timeout_seconds
            ),
        )

    def _dispatch_notifications(self) -> None:
        try:
            result = self.notifier.dispatch_pending()
        except Exception as exc:
            print(f"Discord notification dispatch failed: {exc}")
            return
        if result.failed:
            print("Discord notification delivery failed; retry is scheduled")

    def _process_batch(
        self, batch: BatchClaim, repository: RepositoryConfig
    ) -> None:
        batch_started = time.monotonic()
        if repository.publish_mode != "direct":
            raise FixAgentError("review_batch processing requires publish_mode = direct")
        primary = batch.jobs[0]
        self._batch_event(
            batch.jobs,
            "batch_processing_started",
            "worker started processing the review batch",
            {
                "batch_id": batch.id,
                "batch_size": len(batch.jobs),
                "attempt": batch.attempt,
                "remote": repository.remote,
                "target_branch": repository.target_branch,
            },
        )
        with StateStore(self.config.state_dir) as state:
            resumable_worktree = state.resumable_batch_worktree(batch.id)
            checkpoints = state.publish_checkpoints(f"batch:{batch.id}")
        pending_fallbacks: tuple[_PendingFallback, ...] = ()
        try:
            with FixWorkspace(
                self.runner,
                repository,
                primary,
                self.config.state_dir,
                resumable_worktree=resumable_worktree,
                worktree_scope="batch",
            ) as workspace:
                environment = workspace.safe_environment
                setup_state = _SetupState()
                current_target = workspace.fetch_target_head()
                already_published = tuple(
                    job
                    for job in batch.jobs
                    if job.result_commit
                    and workspace.is_ancestor(job.result_commit, current_target)
                )
                if already_published:
                    with StateStore(self.config.state_dir) as state:
                        for job in already_published:
                            state.mark_pushed(
                                job.id,
                                repository.target_branch,
                                job.result_commit or current_target,
                            )
                        for checkpoint in checkpoints:
                            if workspace.is_ancestor(
                                checkpoint.commit, current_target
                            ):
                                state.mark_publish_checkpoint_pushed(
                                    f"batch:{batch.id}", checkpoint.fingerprints
                                )
                    published_ids = {job.id for job in already_published}
                    pending = tuple(
                        job for job in batch.jobs if job.id not in published_ids
                    )
                    if (
                        pending
                        and not checkpoints
                        and workspace.head_commit() != current_target
                    ):
                        workspace.flatten_batch_to_target(current_target)
                else:
                    pending = batch.jobs

                if checkpoints:
                    valid_jobs = pending
                    self._resume_batch_publish(
                        batch,
                        repository,
                        pending,
                        checkpoints,
                        workspace,
                        environment,
                        setup_state,
                    )
                else:
                    valid_jobs = self._validate_batch_findings(
                        batch,
                        repository,
                        pending,
                        workspace,
                        environment,
                        setup_state,
                    )
                if valid_jobs and not checkpoints:
                    (
                        groups,
                        tests,
                        postcheck,
                        pending_fallbacks,
                    ) = self._fix_batch_until_valid(
                        batch,
                        repository,
                        valid_jobs,
                        workspace,
                        environment,
                        setup_state,
                    )
                    if groups:
                        grouped_fingerprints = {
                            fingerprint
                            for group in groups
                            for fingerprint in group.fingerprints
                        }
                        publish_jobs = tuple(
                            job
                            for job in valid_jobs
                            if job.fingerprint in grouped_fingerprints
                        )
                        while groups:
                            try:
                                self._publish_batch_groups(
                                    batch,
                                    repository,
                                    publish_jobs,
                                    groups,
                                    workspace,
                                    environment,
                                    setup_state,
                                )
                                break
                            except _BatchCorrectionRequired as exc:
                                current_target = workspace.fetch_target_head()
                                workspace.flatten_batch_to_target(current_target)
                                correction_fingerprints = {
                                    fingerprint
                                    for group in exc.groups
                                    for fingerprint in group.fingerprints
                                }
                                publish_jobs = tuple(
                                    job
                                    for job in publish_jobs
                                    if job.fingerprint in correction_fingerprints
                                )
                                (
                                    groups,
                                    tests,
                                    postcheck,
                                    correction_fallbacks,
                                ) = self._fix_batch_until_valid(
                                    batch,
                                    repository,
                                    publish_jobs,
                                    workspace,
                                    environment,
                                    setup_state,
                                    initial_error=str(exc),
                                )
                                pending_fallbacks += correction_fallbacks
                                grouped_fingerprints = {
                                    fingerprint
                                    for group in groups
                                    for fingerprint in group.fingerprints
                                }
                                publish_jobs = tuple(
                                    job
                                    for job in publish_jobs
                                    if job.fingerprint in grouped_fingerprints
                                )
            if not workspace.cleanup_complete:
                raise FixAgentError("batch worktree cleanup did not complete after push")
            with StateStore(self.config.state_dir) as state:
                for fallback in pending_fallbacks:
                    state.activate_finding_fallback(
                        tuple(job.id for job in fallback.jobs), fallback.reason
                    )
                for job in (*already_published, *valid_jobs):
                    current = state.job(job.id)
                    if current is not None and current.status == "pushed":
                        state.mark_completed(job.id, None)
                state.mark_batch_completed(batch.id)
        finally:
            metrics = self.agent.take_batch_metrics()
            duration_ms = round((time.monotonic() - batch_started) * 1000)
            with StateStore(self.config.state_dir) as state:
                state.record_batch_metrics(
                    batch.id,
                    codex_calls=metrics.calls,
                    input_tokens=metrics.input_tokens,
                    cached_input_tokens=metrics.cached_input_tokens,
                    cache_write_input_tokens=metrics.cache_write_input_tokens,
                    output_tokens=metrics.output_tokens,
                    reasoning_output_tokens=metrics.reasoning_output_tokens,
                    total_tokens=metrics.total_tokens,
                    duration_ms=duration_ms,
                )
            self._event(
                primary.id,
                "batch_metrics_recorded",
                "review batch Codex usage and execution time were recorded",
                {
                    "batch_id": batch.id,
                    "codex_calls": metrics.calls,
                    "total_tokens": metrics.total_tokens,
                    "duration_ms": duration_ms,
                },
            )

    def _validate_batch_findings(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        jobs: tuple[Job, ...],
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> tuple[Job, ...]:
        candidates: list[Job] = []
        for job in jobs:
            mismatch = workspace.finding_mismatch_reason(job)
            if mismatch:
                with StateStore(self.config.state_dir) as state:
                    state.record_precheck(job.id, False, mismatch)
                continue
            self._event(
                job.id,
                "finding_git_validated",
                "finding commit, file, and reviewed line matched the review diff",
                {"batch_id": batch.id, "reviewed_target": job.target_commit},
            )
            candidates.append(job)
        if not candidates:
            return ()
        self._prepare_environment(
            repository, candidates[0], workspace, environment, setup_state
        )
        reused = {
            job.fingerprint: job.precheck_reason
            for job in candidates
            if job.precheck_status == "valid" and job.precheck_reason
        }
        undecided = tuple(job for job in candidates if job.fingerprint not in reused)
        decisions = ()
        if undecided:
            self._batch_event(
                undecided,
                "batch_validation_started",
                "Codex started independent review batch validation",
                {"batch_id": batch.id, "workspace_base": workspace.base_commit},
                notify=True,
            )
            decisions = self.agent.validate_findings(
                self._codex_repository(repository),
                undecided,
                workspace.path,
                environment,
                workspace.base_commit or undecided[0].target_commit,
            )
        by_fingerprint = {decision.fingerprint: decision for decision in decisions}
        valid: list[Job] = []
        for job in candidates:
            if job.fingerprint in reused:
                reason = reused[job.fingerprint]
                accepted = True
            else:
                decision = by_fingerprint[job.fingerprint]
                reason = decision.reason
                accepted = decision.valid
            with StateStore(self.config.state_dir) as state:
                state.record_precheck(job.id, accepted, reason or "")
            self._event(
                job.id,
                "batch_validation_completed",
                "Codex completed independent finding validation in the review batch",
                {
                    "batch_id": batch.id,
                    "valid": accepted,
                    "reason": reason,
                    "reused": job.fingerprint in reused,
                },
                notify=True,
            )
            if accepted:
                valid.append(job)
        return tuple(valid)

    def _fix_batch_until_valid(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        jobs: tuple[Job, ...],
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
        initial_error: str | None = None,
    ) -> tuple[
        tuple[BatchChangeGroup, ...],
        list[dict[str, object]],
        BatchFixDecision | None,
        tuple[_PendingFallback, ...],
    ]:
        active = jobs
        pending_fallbacks: list[_PendingFallback] = []
        iteration = 1
        previous_error = initial_error or next(
            (job.last_error for job in jobs if job.last_error), None
        )
        repeated_contract_error: str | None = None
        repeated_contract_failures = 0
        while active:
            self._remaining_timeout(repository.job_timeout_seconds)
            self._batch_event(
                active,
                "batch_fix_started",
                "Codex started the review batch edit",
                {
                    "batch_id": batch.id,
                    "iteration": iteration,
                    "same_worktree": iteration > 1 or batch.attempt > 1,
                    "previous_error": previous_error[:4_000] if previous_error else None,
                },
                notify=True,
            )
            groups: tuple[BatchChangeGroup, ...] = ()
            tests: list[dict[str, object]] = []
            before_edit = workspace.working_tree_fingerprint()
            try:
                self._ensure_workspace_permissions(
                    active[0], workspace, f"batch_fix_{iteration}_before_edit"
                )
                groups = self.agent.apply_batch_fixes(
                    self._codex_repository(repository),
                    active,
                    workspace.path,
                    environment,
                    workspace.base_commit or active[0].target_commit,
                    previous_error,
                )
                self._prepare_environment(
                    repository, active[0], workspace, environment, setup_state
                )
                self._ensure_workspace_permissions(
                    active[0], workspace, f"batch_fix_{iteration}_before_harness"
                )
                summary = workspace.validate_diff(tuple(job.file for job in active))
                grouped_files = {file for group in groups for file in group.files}
                if set(summary.files) != grouped_files:
                    raise FixAgentError(
                        "batch change groups do not match the working diff"
                    )
                workspace.stage_for_harness()
                for job in active:
                    with StateStore(self.config.state_dir) as state:
                        state.mark_testing(job.id)
                self._event(
                    active[0].id,
                    "tests_started",
                    "configured repository harness started for the review batch",
                    {
                        "batch_id": batch.id,
                        "commands": len(repository.test_commands),
                        "iteration": iteration,
                    },
                )
                tests = self._run_tests(
                    repository,
                    workspace.path,
                    environment,
                    tuple(job.id for job in active),
                )
                failed = [test for test in tests if test["returncode"] != 0]
                for job in active:
                    with StateStore(self.config.state_dir) as state:
                        state.record_tests(job.id, tests)
                if failed:
                    raise FixAgentError(_test_failure_error(failed))
                self._batch_event(
                    active,
                    "result_validation_started",
                    "Codex started independent review batch result validation",
                    {
                        "batch_id": batch.id,
                        "iteration": iteration,
                        "workspace_base": workspace.base_commit,
                    },
                    notify=True,
                )
                postcheck = self.agent.validate_batch_fix(
                    self._codex_repository(repository),
                    active,
                    groups,
                    workspace.path,
                    environment,
                    workspace.base_commit or active[0].target_commit,
                )
                decisions = {
                    decision.fingerprint: decision for decision in postcheck.findings
                }
                for job in active:
                    decision = decisions[job.fingerprint]
                    with StateStore(self.config.state_dir) as state:
                        state.record_postcheck(
                            job.id,
                            decision.valid,
                            decision.reason,
                            retry_on_failure=True,
                        )
                self._batch_event(
                    active,
                    "result_validation_completed",
                    "Codex completed independent review batch result validation",
                    {
                        "batch_id": batch.id,
                        "iteration": iteration,
                        "resolved": postcheck.resolved,
                        "reason": postcheck.reason,
                    },
                    notify=True,
                )
                if not postcheck.resolved or not all(
                    decision.valid for decision in postcheck.findings
                ):
                    raise FixAgentError(
                        "batch fix did not pass independent validation: "
                        + postcheck.reason
                    )
                workspace.validate_diff(tuple(job.file for job in active))
                return postcheck.groups, tests, postcheck, tuple(pending_fallbacks)
            except (EnvironmentSetupError, JobTerminalError):
                raise
            except FixAgentError as exc:
                previous_error = str(exc)
                if not groups and _is_batch_contract_error(previous_error):
                    if repeated_contract_error == previous_error:
                        repeated_contract_failures += 1
                    else:
                        repeated_contract_error = previous_error
                        repeated_contract_failures = 1
                else:
                    repeated_contract_error = None
                    repeated_contract_failures = 0
                for job in active:
                    with StateStore(self.config.state_dir) as state:
                        state.record_fix_iteration_failure(job.id, previous_error, tests)
                self._batch_event(
                    active,
                    "fix_iteration_failed",
                    "review batch validation failed; editing continues in the same worktree",
                    {
                        "batch_id": batch.id,
                        "iteration": iteration,
                        "next_iteration": iteration + 1,
                        "error": previous_error[:4_000],
                        "same_worktree": True,
                    },
                    notify=True,
                )
                if repeated_contract_failures >= 2:
                    with StateStore(self.config.state_dir) as state:
                        state.mark_finding_fallback_pending(
                            tuple(job.id for job in active), previous_error
                        )
                    pending_fallbacks.append(
                        _PendingFallback(active, previous_error)
                    )
                    self._batch_event(
                        active,
                        "batch_fallback_started",
                        "repeated batch response error moved findings to finding mode",
                        {
                            "batch_id": batch.id,
                            "fingerprints": [job.fingerprint for job in active],
                            "reason": previous_error[:4_000],
                            "contract_failures": repeated_contract_failures,
                        },
                        notify=True,
                    )
                    return (), [], None, tuple(pending_fallbacks)
                if (
                    repeated_contract_failures == 0
                    and workspace.working_tree_fingerprint() == before_edit
                ):
                    with StateStore(self.config.state_dir) as state:
                        state.mark_finding_fallback_pending(
                            tuple(job.id for job in active), previous_error
                        )
                    pending_fallbacks.append(_PendingFallback(active, previous_error))
                    self._batch_event(
                        active,
                        "batch_fallback_started",
                        "batch edit made no change; findings moved to finding mode",
                        {
                            "batch_id": batch.id,
                            "fingerprints": [job.fingerprint for job in active],
                            "reason": previous_error[:4_000],
                        },
                        notify=True,
                    )
                    return (), [], None, tuple(pending_fallbacks)
                if iteration >= 2 and groups:
                    problem = self.agent.diagnose_batch_failure(
                        self._codex_repository(repository),
                        active,
                        groups,
                        workspace.path,
                        environment,
                        previous_error,
                    )
                    problem_set = set(problem)
                    problem_group = next(
                        group
                        for group in groups
                        if problem_set.intersection(group.fingerprints)
                    )
                    isolated = tuple(
                        job for job in active if job.fingerprint in problem_set
                    )
                    workspace.discard_group_changes(problem_group.files)
                    with StateStore(self.config.state_dir) as state:
                        state.mark_finding_fallback_pending(
                            tuple(job.id for job in isolated), previous_error
                        )
                    pending_fallbacks.append(
                        _PendingFallback(isolated, previous_error)
                    )
                    self._batch_event(
                        isolated,
                        "batch_fallback_started",
                        "repeated failure isolated the finding group for finding mode",
                        {
                            "batch_id": batch.id,
                            "fingerprints": list(problem),
                            "files": list(problem_group.files),
                            "reason": previous_error[:4_000],
                        },
                    )
                    active = tuple(
                        job for job in active if job.fingerprint not in problem_set
                    )
                    iteration = 1
                    continue
                iteration += 1
        return (), [], None, tuple(pending_fallbacks)

    @_serialized_publish(1)
    def _resume_batch_publish(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        jobs: tuple[Job, ...],
        checkpoints: tuple[PublishCheckpoint, ...],
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> None:
        jobs_by_fingerprint = {job.fingerprint: job for job in jobs}
        remaining = tuple(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.status != "pushed"
            and any(
                fingerprint in jobs_by_fingerprint
                for fingerprint in checkpoint.fingerprints
            )
        )
        if not remaining:
            return
        self._batch_event(
            jobs,
            "publish_retry_resumed",
            "batch publish retry resumed from validated commit checkpoints",
            {
                "batch_id": batch.id,
                "commits": [checkpoint.commit for checkpoint in remaining],
            },
        )
        for index, checkpoint in enumerate(remaining):
            current = workspace.fetch_target_head()
            if workspace.is_ancestor(checkpoint.commit, current):
                self._complete_batch_checkpoint(
                    batch, repository, checkpoint, jobs_by_fingerprint
                )
                continue
            expected_parent = workspace.commit_parent(checkpoint.commit)
            if current != expected_parent:
                pending_checkpoints = remaining[index:]
                groups = tuple(
                    BatchChangeGroup(
                        value.fingerprints, value.files, value.title
                    )
                    for value in pending_checkpoints
                )
                pending_jobs = tuple(
                    job
                    for job in jobs
                    if any(
                        job.fingerprint in value.fingerprints
                        for value in pending_checkpoints
                    )
                )
                self._reconcile_batch_target(
                    batch,
                    repository,
                    pending_jobs,
                    groups,
                    groups,
                    workspace,
                    environment,
                    setup_state,
                    0,
                )
                self._publish_batch_groups(
                    batch,
                    repository,
                    pending_jobs,
                    groups,
                    workspace,
                    environment,
                    setup_state,
                )
                return
            try:
                checkpoint_jobs = tuple(
                    jobs_by_fingerprint[fingerprint]
                    for fingerprint in checkpoint.fingerprints
                    if fingerprint in jobs_by_fingerprint
                )
                self._batch_event(
                    checkpoint_jobs,
                    "push_started",
                    "pushing a checkpointed finding change group",
                    {
                        "batch_id": batch.id,
                        "remote": repository.remote,
                        "branch": repository.target_branch,
                        "commit": checkpoint.commit,
                    },
                )
                self._push_commit(
                    repository,
                    workspace.path,
                    environment,
                    repository.target_branch,
                    checkpoint.commit,
                )
            except FixAgentError as exc:
                workspace.preserve(str(exc))
                raise
            self._complete_batch_checkpoint(
                batch, repository, checkpoint, jobs_by_fingerprint
            )

    def _complete_batch_checkpoint(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        checkpoint: PublishCheckpoint,
        jobs_by_fingerprint: dict[str, Job],
    ) -> None:
        for fingerprint in checkpoint.fingerprints:
            job = jobs_by_fingerprint.get(fingerprint)
            if job is None:
                continue
            self._event(
                job.id,
                "push_completed",
                "finding change group was pushed from its checkpoint",
                {
                    "batch_id": batch.id,
                    "remote": repository.remote,
                    "branch": repository.target_branch,
                    "commit": checkpoint.commit,
                    "fingerprints": list(checkpoint.fingerprints),
                },
            )
            with StateStore(self.config.state_dir) as state:
                state.mark_pushed(
                    job.id, repository.target_branch, checkpoint.commit
                )
        with StateStore(self.config.state_dir) as state:
            state.mark_publish_checkpoint_pushed(
                f"batch:{batch.id}", checkpoint.fingerprints
            )

    @_serialized_publish(1)
    def _publish_batch_groups(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        jobs: tuple[Job, ...],
        groups: tuple[BatchChangeGroup, ...],
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> None:
        jobs_by_fingerprint = {job.fingerprint: job for job in jobs}
        remaining = groups
        remote_merges = 0
        while remaining:
            current_round = remaining
            commit_inputs = tuple(
                (
                    group.fingerprints,
                    group.files,
                    group.commit_title or "fix: 리뷰 finding 수정",
                    jobs_by_fingerprint[group.fingerprints[0]],
                )
                for group in current_round
            )
            commits = workspace.commit_finding_groups(commit_inputs)
            for sequence, finding_commit in enumerate(commits, start=1):
                checkpoint_jobs = tuple(
                    jobs_by_fingerprint[value].id
                    for value in finding_commit.fingerprints
                )
                with StateStore(self.config.state_dir) as state:
                    state.record_publish_checkpoint(
                        checkpoint_jobs,
                        batch_id=batch.id,
                        sequence=sequence,
                        branch=repository.target_branch,
                        commit=finding_commit.commit,
                        fingerprints=finding_commit.fingerprints,
                        files=finding_commit.files,
                        title=finding_commit.title,
                    )
            restart = False
            for index, finding_commit in enumerate(commits):
                expected_parent = workspace.commit_parent(finding_commit.commit)
                current = workspace.fetch_target_head()
                if current != expected_parent:
                    remaining = current_round[index:]
                    remote_merges = self._reconcile_batch_target(
                        batch,
                        repository,
                        jobs,
                        groups,
                        remaining,
                        workspace,
                        environment,
                        setup_state,
                        remote_merges,
                    )
                    restart = True
                    break
                self._batch_event(
                    tuple(
                        jobs_by_fingerprint[value]
                        for value in finding_commit.fingerprints
                    ),
                    "push_started",
                    "pushing one finding change group to the configured target",
                    {
                        "batch_id": batch.id,
                        "remote": repository.remote,
                        "branch": repository.target_branch,
                        "commit": finding_commit.commit,
                    },
                )
                try:
                    self._push_commit(
                        repository,
                        workspace.path,
                        environment,
                        repository.target_branch,
                        finding_commit.commit,
                    )
                except FixAgentError as push_error:
                    try:
                        current = workspace.fetch_target_head()
                    except FixAgentError:
                        workspace.preserve(str(push_error))
                        raise push_error
                    if current != finding_commit.commit:
                        if current == expected_parent:
                            workspace.preserve(str(push_error))
                            raise push_error
                        remaining = current_round[index:]
                        remote_merges = self._reconcile_batch_target(
                            batch,
                            repository,
                            jobs,
                            groups,
                            remaining,
                            workspace,
                            environment,
                            setup_state,
                            remote_merges,
                        )
                        restart = True
                        break
                for fingerprint in finding_commit.fingerprints:
                    job = jobs_by_fingerprint[fingerprint]
                    self._event(
                        job.id,
                        "push_completed",
                        "finding change group was pushed to the configured target",
                        {
                            "batch_id": batch.id,
                            "remote": repository.remote,
                            "branch": repository.target_branch,
                            "commit": finding_commit.commit,
                            "fingerprints": list(finding_commit.fingerprints),
                        },
                    )
                    with StateStore(self.config.state_dir) as state:
                        state.mark_pushed(
                            job.id, repository.target_branch, finding_commit.commit
                        )
                        state.mark_publish_checkpoint_pushed(
                            f"batch:{batch.id}", finding_commit.fingerprints
                        )
            if not restart:
                remaining = ()
                break

    def _reconcile_batch_target(
        self,
        batch: BatchClaim,
        repository: RepositoryConfig,
        jobs: tuple[Job, ...],
        all_groups: tuple[BatchChangeGroup, ...],
        remaining: tuple[BatchChangeGroup, ...],
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
        remote_merges: int,
    ) -> int:
        if remote_merges >= repository.max_remote_merge_attempts:
            raise FixAgentError("target branch kept moving during batch push")
        current = workspace.fetch_target_head()
        self._batch_event(
            jobs,
            "target_moved",
            "target moved; the whole review batch will be revalidated",
            {
                "batch_id": batch.id,
                "previous_base": workspace.base_commit,
                "current_target": current,
                "merge_attempt": remote_merges + 1,
            },
        )
        merge = workspace.merge_latest_target(current)
        if merge.conflict_files:
            self._batch_event(
                jobs,
                "merge_conflict_detected",
                "target merge produced review batch conflicts",
                {"batch_id": batch.id, "files": list(merge.conflict_files)},
            )
            resolution = self.agent.resolve_batch_merge_conflicts(
                self._codex_repository(repository),
                jobs,
                workspace.path,
                environment,
                merge.previous_base,
                merge.current_target,
                merge.conflict_files,
            )
            if not resolution.valid:
                raise FixAgentError(
                    "Codex could not safely resolve batch merge conflicts: "
                    + resolution.reason
                )
            workspace.complete_conflicted_merge(merge.current_target)
            self._batch_event(
                jobs,
                "merge_conflict_resolved",
                "review batch merge conflicts were resolved and recorded",
                {"batch_id": batch.id, "reason": resolution.reason},
            )
        else:
            self._batch_event(
                jobs,
                "target_merged",
                "latest target was merged into the review batch",
                {
                    "batch_id": batch.id,
                    "previous_base": merge.previous_base,
                    "current_target": merge.current_target,
                    "commit": workspace.head_commit(),
                },
            )
        required_files = tuple(
            jobs_by_file.file
            for group in remaining
            for jobs_by_file in jobs
            if jobs_by_file.fingerprint in group.fingerprints
        )
        correction_groups = remaining
        try:
            workspace.validate_diff(required_files)
            self._prepare_environment(
                repository, jobs[0], workspace, environment, setup_state
            )
            tests = self._run_tests(
                repository,
                workspace.path,
                environment,
                tuple(job.id for job in jobs),
            )
            failed = [test for test in tests if test["returncode"] != 0]
            for job in jobs:
                with StateStore(self.config.state_dir) as state:
                    current_job = state.job(job.id)
                    if current_job is not None and current_job.status == "pushed":
                        state.record_event(
                            job.id,
                            "batch_target_tests_recorded",
                            "target-move harness result retained for a pushed finding",
                            {"batch_id": batch.id, "tests": len(tests)},
                        )
                    else:
                        state.record_tests(job.id, tests)
            if failed:
                raise FixAgentError(
                    _test_failure_error(failed, after_target_merge=True)
                )
            validation = self.agent.validate_batch_fix(
                self._codex_repository(repository),
                jobs,
                all_groups,
                workspace.path,
                environment,
                workspace.base_commit or current,
            )
            if not validation.resolved or not all(
                decision.valid for decision in validation.findings
            ):
                invalid_fingerprints = {
                    decision.fingerprint
                    for decision in validation.findings
                    if not decision.valid
                }
                if invalid_fingerprints:
                    correction_fingerprints = invalid_fingerprints.union(
                        fingerprint
                        for group in remaining
                        for fingerprint in group.fingerprints
                    )
                    correction_groups = tuple(
                        group
                        for group in all_groups
                        if correction_fingerprints.intersection(group.fingerprints)
                    )
                raise FixAgentError(
                    "target-integrated batch did not pass validation: "
                    + validation.reason
                )
        except EnvironmentSetupError:
            raise
        except FixAgentError as exc:
            raise _BatchCorrectionRequired(str(exc), correction_groups) from exc
        workspace.flatten_batch_to_target(current)
        self._batch_event(
            jobs,
            "merged_fix_revalidated",
            "target-integrated review batch passed the full harness and result validation",
            {
                "batch_id": batch.id,
                "target_commit": current,
                "merge_attempt": remote_merges + 1,
            },
        )
        return remote_merges + 1

    def _batch_event(
        self,
        jobs: tuple[Job, ...],
        event_type: str,
        message: str,
        details: dict[str, object],
        *,
        notify: bool = False,
    ) -> None:
        if not jobs:
            return
        self._event(jobs[0].id, event_type, message, details)
        with StateStore(self.config.state_dir) as state:
            for job in jobs[1:]:
                state.record_event(job.id, event_type, message, details)
        if notify:
            self._dispatch_notifications()

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
        with StateStore(self.config.state_dir) as state:
            resumable_worktree = state.resumable_worktree(
                job.id, scope="finding"
            )
        with FixWorkspace(
            self.runner,
            repository,
            job,
            self.config.state_dir,
            resumable_worktree=resumable_worktree,
        ) as workspace:
            environment = workspace.safe_environment
            setup_state = _SetupState()
            with StateStore(self.config.state_dir) as state:
                checkpoints = state.publish_checkpoints(f"job:{job.id}")
            checkpoint = checkpoints[0] if checkpoints else None
            if checkpoint is not None:
                branch = checkpoint.branch
                commit = checkpoint.commit
                summary = workspace.validate_diff()
                tests = json.loads(job.tests_json)
                decision = Decision(True, job.precheck_reason or "checkpointed")
                postcheck = Decision(True, job.postcheck_reason or "checkpointed")
                self._event(
                    job.id,
                    "publish_retry_resumed",
                    "publish retry resumed from the validated commit checkpoint",
                    {"branch": branch, "commit": commit},
                )
            else:
                prepared = self._prepare_finding_commit(
                    repository, job, workspace, environment, setup_state
                )
                if prepared is None:
                    return
                branch, commit, summary, tests, decision, postcheck = prepared
            workspace.hold_publish_lock(_publish_lock(repository))
            remote_merges = 0
            while True:
                current = workspace.fetch_target_head()
                if workspace.is_ancestor(commit, current):
                    break
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
                            self._codex_repository(repository),
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
                    self._ensure_workspace_permissions(
                        job, workspace, f"target_merge_{remote_merges}_before_harness"
                    )
                    with StateStore(self.config.state_dir) as state:
                        state.mark_testing(job.id)
                    tests = self._run_tests(
                        repository, workspace.path, environment, (job.id,)
                    )
                    with StateStore(self.config.state_dir) as state:
                        state.record_tests(job.id, tests)
                    failed = [test for test in tests if test["returncode"] != 0]
                    if failed:
                        raise FixAgentError(
                            _test_failure_error(failed, after_target_merge=True)
                        )
                    workspace.require_clean_checkout()
                    postcheck = self.agent.validate_fix(
                        self._codex_repository(repository),
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
                    with StateStore(self.config.state_dir) as state:
                        state.record_publish_checkpoint(
                            (job.id,),
                            batch_id=None,
                            sequence=1,
                            branch=branch,
                            commit=commit,
                            fingerprints=(job.fingerprint,),
                            files=summary.files,
                            title=checkpoint.title if checkpoint else "merged fix",
                        )
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
                    try:
                        current = workspace.fetch_target_head()
                    except FixAgentError:
                        workspace.preserve(str(push_error))
                        raise push_error
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
                    workspace.preserve(str(push_error))
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
                state.mark_publish_checkpoint_pushed(
                    f"job:{job.id}", (job.fingerprint,)
                )
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

    def _prepare_finding_commit(
        self,
        repository: RepositoryConfig,
        job: Job,
        workspace: FixWorkspace,
        environment: dict[str, str],
        setup_state: _SetupState,
    ) -> tuple[
        str,
        str,
        DiffSummary,
        list[dict[str, object]],
        Decision,
        Decision,
    ] | None:
        mismatch = workspace.finding_mismatch_reason()
        if mismatch:
            with StateStore(self.config.state_dir) as state:
                state.record_precheck(job.id, False, mismatch)
            print(f"job {job.id} rejected: {mismatch}")
            return None
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
                self._codex_repository(repository),
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
                return None
        summary, tests, postcheck = self._fix_until_valid(
            repository, job, workspace, environment, setup_state
        )
        if postcheck.commit_title is None:
            raise FixAgentError("fix validation returned no commit title")
        branch, commit = workspace.commit(postcheck.commit_title)
        self._event(
            job.id,
            "fix_committed",
            "validated fix was committed in the worktree",
            {"branch": branch, "commit": commit, "title": postcheck.commit_title},
        )
        with StateStore(self.config.state_dir) as state:
            state.record_publish_checkpoint(
                (job.id,),
                batch_id=None,
                sequence=1,
                branch=branch,
                commit=commit,
                fingerprints=(job.fingerprint,),
                files=summary.files,
                title=postcheck.commit_title,
            )
        return branch, commit, summary, tests, decision, postcheck

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
        job_ids: tuple[int, ...] = (),
    ) -> list[dict[str, object]]:
        results = []
        host_os = _host_operating_system()
        for command, allowed_host_os in zip(
            repository.test_commands, repository.test_command_host_os, strict=True
        ):
            if allowed_host_os and host_os not in allowed_host_os:
                reason = (
                    f"command requires host_os={','.join(allowed_host_os)}; "
                    f"current host_os={host_os}"
                )
                results.append(
                    {
                        "command": list(command),
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "outcome": "conditional_pass",
                        "reason": reason,
                        "host_os": host_os,
                        "required_host_os": list(allowed_host_os),
                    }
                )
                for job_id in job_ids:
                    self._event(
                        job_id,
                        "tests_conditional_pass",
                        "현재 OS에서 실행할 수 없는 하네스 명령 조건부 통과",
                        {
                            "command": list(command),
                            "host_os": host_os,
                            "required_host_os": list(allowed_host_os),
                            "reason": reason,
                        },
                    )
                continue
            result = self.runner.run(
                command,
                cwd=workspace,
                environment=environment,
                timeout_seconds=self._remaining_timeout(
                    repository.harness_timeout_seconds
                ),
                check=False,
            )
            results.append(
                {
                    "command": list(command),
                    "returncode": result.returncode,
                    "stdout": result.stdout[-20_000:],
                    "stderr": result.stderr[-20_000:],
                    "outcome": "passed" if result.returncode == 0 else "failed",
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
        self._ensure_workspace_permissions(
            job, workspace, "environment_setup_before_commands"
        )
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
                    timeout_seconds=self._remaining_timeout(
                        repository.command_timeout_seconds
                    ),
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
                permissions = self._ensure_workspace_permissions(
                    job, workspace, "environment_setup"
                )
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
                        "permission_repairs": {
                            "files": permissions.repaired_files,
                            "directories": permissions.repaired_directories,
                        },
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

    def _ensure_workspace_permissions(
        self, job: Job, workspace: FixWorkspace, stage: str
    ) -> PermissionSummary:
        permissions = workspace.ensure_writable()
        if permissions.repaired:
            self._event(
                job.id,
                "worktree_permissions_repaired",
                "관리 worktree 소유자 쓰기 권한 복구 완료",
                {
                    "stage": stage,
                    "repaired_files": permissions.repaired_files,
                    "repaired_directories": permissions.repaired_directories,
                    "checked_files": permissions.checked_files,
                    "checked_directories": permissions.checked_directories,
                },
            )
        return permissions

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
        repeated_error = retry_error
        repeated_failures = 1 if retry_error else 0
        while True:
            self._remaining_timeout(repository.job_timeout_seconds)
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
            before_edit = workspace.working_tree_fingerprint()
            try:
                self._ensure_workspace_permissions(
                    job, workspace, f"fix_iteration_{iteration}_before_edit"
                )
                self.agent.apply_fix(
                    self._codex_repository(repository),
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
                self._ensure_workspace_permissions(
                    job, workspace, f"fix_iteration_{iteration}_before_harness"
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
                tests = self._run_tests(
                    repository, workspace.path, environment, (job.id,)
                )
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
                    self._codex_repository(repository),
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
            except (EnvironmentSetupError, JobTerminalError):
                raise
            except FixAgentError as exc:
                retry_error = str(exc)
                if retry_error == repeated_error:
                    repeated_failures += 1
                else:
                    repeated_error = retry_error
                    repeated_failures = 1
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
                if workspace.working_tree_fingerprint() == before_edit:
                    raise JobTerminalError(
                        "Codex edit made no worktree change after a failed validation"
                    ) from exc
                if repeated_failures >= 2:
                    raise JobTerminalError(
                        "the same fix validation error occurred twice"
                    ) from exc
                total_attempt = job.attempts + iteration - 1
                if (
                    repository.max_attempts != 0
                    and total_attempt >= repository.max_attempts
                ):
                    raise
                iteration += 1

    def _push(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
        branch: str,
    ) -> None:
        self._push_commit(repository, workspace, environment, branch, "HEAD")

    @_serialized_publish(0)
    def _push_commit(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
        branch: str,
        commit: str,
    ) -> None:
        token = self._github_token(repository)
        push_environment = dict(environment)
        expected_urls = {
            f"https://github.com/{repository.github}",
            f"https://github.com/{repository.github}.git",
        }
        fetch_url = self.runner.run(
            ["git", "remote", "get-url", repository.remote],
            cwd=workspace,
            environment=environment,
        ).stdout.strip()
        push_url = self.runner.run(
            ["git", "remote", "get-url", "--push", repository.remote],
            cwd=workspace,
            environment=environment,
        ).stdout.strip()
        if fetch_url not in expected_urls:
            raise FixAgentError(
                f"Git fetch URL does not match configured GitHub repository: {fetch_url}"
            )
        if push_url not in expected_urls:
            raise FixAgentError(
                f"Git push URL does not match configured GitHub repository: {push_url}"
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
        command.extend([repository.remote, f"{commit}:refs/heads/{branch}"])
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


def _host_operating_system() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith(("win32", "cygwin")):
        return "windows"
    return sys.platform


def _is_batch_contract_error(error: str) -> bool:
    return error.startswith(
        (
            "Codex batch call",
            "Codex batch response",
            "Codex batch groups",
            "same-file findings",
        )
    )
