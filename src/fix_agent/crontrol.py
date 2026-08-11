from __future__ import annotations

import json
import os
import threading
from typing import Callable, ContextManager, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import AppConfig, CrontrolConfig
from .errors import FixAgentError
from .state import Job, StateStore


class _Response(Protocol):
    status: int


_Open = Callable[..., ContextManager[_Response]]
_TERMINAL_STATUSES = {"completed", "rejected", "skipped", "failed"}
_RUNNING_STATUSES = {"validating", "fixing", "testing", "ready", "pushed"}


class CrontrolReporter:
    def __init__(
        self,
        config: AppConfig,
        *,
        opener: _Open = urlopen,
    ) -> None:
        self.config = config
        self.settings = config.crontrol
        self.opener = opener
        self._last_payload: str | None = None
        self.last_error: str | None = None
        self._lock = threading.RLock()
        self._stages: dict[int, str] = {}

    def sync(self, current_job_id: int | None, stage: str | None = None) -> bool:
        if not self.settings.enabled:
            return False
        with self._lock:
            if current_job_id is not None and stage is not None:
                self._stages[current_job_id] = stage
            try:
                payload = self._payload(current_job_id, stage)
                serialized = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                if serialized == self._last_payload:
                    return False
                self._send(payload)
                self._last_payload = serialized
                self.last_error = None
                return True
            except (FixAgentError, OSError, HTTPError, URLError, ValueError) as exc:
                self.last_error = _safe_error(exc)
                print(f"Crontrol status sync failed: {self.last_error}")
                return False

    def _payload(
        self, current_job_id: int | None, stage: str | None
    ) -> dict[str, object]:
        with StateStore(self.config.state_dir) as state:
            jobs = state.jobs(10_000)
        current = next((job for job in jobs if job.id == current_job_id), None)
        if current_job_id is not None and current is None:
            raise FixAgentError(f"job does not exist: {current_job_id}")
        raw_running_jobs = [job for job in jobs if job.status in _RUNNING_STATUSES]
        running_ids = {job.id for job in raw_running_jobs}
        self._stages = {
            job_id: value
            for job_id, value in self._stages.items()
            if job_id in running_ids
        }
        if current is None or current.id not in running_ids:
            current = raw_running_jobs[-1] if raw_running_jobs else current
        running_by_unit: dict[tuple[str, object], Job] = {}
        for job in raw_running_jobs:
            running_by_unit.setdefault(_execution_key(job), job)
        if current is not None and current.id in running_ids:
            running_by_unit[_execution_key(current)] = current
        running_jobs = list(running_by_unit.values())
        retryable = {
            job.id
            for job in jobs
            if job.status == "failed" and self._retryable(job)
        }
        queued = len(
            {
                _execution_key(job)
                for job in jobs
                if job.status == "queued" or job.id in retryable
            }
        )
        latest_terminal = next(
            (
                job
                for job in jobs
                if job.status in _TERMINAL_STATUSES and job.id not in retryable
            ),
            None,
        )
        running = bool(running_jobs)
        result = "FAIL" if latest_terminal and latest_terminal.status == "failed" else "PASS"
        current_stage = "idle"
        if current is not None:
            requested_stage = stage if current.id == current_job_id else None
            current_stage = _display(
                self._stages.get(current.id)
                or requested_stage
                or self._job_stage(current),
                120,
            )
        schedule = self._schedule(current, current_stage, queued, len(running_jobs))
        branch = current.branch if current is not None else self.settings.branch
        payload: dict[str, object] = {
            "id": self.settings.job_id,
            "name": self.settings.name,
            "type": "launchd",
            "scope": "external",
            "sessionId": "launchd",
            "status": "active",
            "schedule": schedule,
            "branch": branch,
            "running": running,
            "lastResult": result,
            "currentJobId": current.id if current is not None else None,
            "currentRepository": current.repository if current is not None else None,
            "currentStage": current_stage,
            "currentTimingStatus": (
                current.timing_status if current is not None else None
            ),
            "currentOverdueReason": (
                current.overdue_reason if current is not None else None
            ),
            "queuedJobs": queued,
            "runningJobCount": len(running_jobs),
            "maxConcurrentJobs": self.config.server.max_concurrent_jobs,
            "runningJobs": [
                {
                    "jobId": job.id,
                    "repository": job.repository,
                    "branch": job.branch,
                    "stage": _display(
                        self._stages.get(job.id) or self._job_stage(job), 120
                    ),
                    "timingStatus": job.timing_status,
                    "overdueReason": job.overdue_reason,
                }
                for job in reversed(running_jobs)
            ],
            "launchdLabel": "com.inswave.code-fix-agent",
            "healthUrl": _health_url(self.config),
        }
        if latest_terminal is not None:
            payload["lastRun"] = latest_terminal.updated_at
        return payload

    def _schedule(
        self, current: Job | None, stage: str, queued: int, running_count: int
    ) -> str:
        if current is None:
            return f"유휴 · 대기 {queued}건"
        repository = current.repository.rsplit("/", 1)[-1]
        prefix = f"동시 {running_count}건 · " if running_count > 1 else ""
        return f"{prefix}{repository} #{current.id} · {stage} · 대기 {queued}건"

    def _retryable(self, job: Job) -> bool:
        repository = self.config.repository_by_id(job.repository_id)
        return repository.max_attempts == 0 or job.attempts < repository.max_attempts

    def _job_stage(self, job: Job) -> str:
        if job.status == "failed" and self._retryable(job):
            return "재시도 대기"
        stage = _status_stage(job.status)
        if job.timing_status == "overdue":
            return f"목표 시간 초과 · {stage}"
        return stage

    def _send(self, payload: dict[str, object]) -> None:
        job_url = (
            self.settings.base_url
            + "/api/jobs/"
            + quote(self.settings.job_id, safe="")
        )
        try:
            self._request(job_url, "PATCH", payload)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
            self._request(self.settings.base_url + "/api/jobs", "POST", payload)

    def _request(
        self, url: str, method: str, payload: dict[str, object]
    ) -> None:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "code-fix-agent/0.1",
        }
        token = _token(self.settings)
        if token:
            headers["X-Crontrol-API-Token"] = token
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method=method,
        )
        with self.opener(request, timeout=self.settings.timeout_seconds) as response:
            if not 200 <= response.status < 300:
                raise FixAgentError(
                    f"Crontrol API returned HTTP {response.status}"
                )


def _status_stage(status: str) -> str:
    return {
        "validating": "finding 검증 중",
        "fixing": "수정 중",
        "testing": "테스트 중",
        "ready": "push 준비",
        "pushed": "push 완료 처리 중",
        "completed": "완료",
        "rejected": "검증 제외",
        "skipped": "정책 제외",
        "failed": "실패",
    }.get(status, status)


def _execution_key(job: Job) -> tuple[str, object]:
    if job.batch_id and not job.fallback_finding:
        return "batch", job.batch_id
    return "job", job.id


def _token(config: CrontrolConfig) -> str | None:
    if config.token_env:
        value = os.environ.get(config.token_env)
        if not value:
            raise FixAgentError(
                "required Crontrol token environment variable is not set: "
                + config.token_env
            )
        return value
    return config.token


def _health_url(config: AppConfig) -> str:
    host = config.server.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{config.server.port}/health"


def _display(value: str, maximum: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= maximum else normalized[: maximum - 1] + "…"


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"Crontrol API returned HTTP {exc.code}"
    if isinstance(exc, URLError):
        return f"Crontrol API connection failed: {exc.reason}"
    return str(exc)
