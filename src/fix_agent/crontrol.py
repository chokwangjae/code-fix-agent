from __future__ import annotations

import json
import os
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

    def sync(self, current_job_id: int | None, stage: str | None = None) -> bool:
        if not self.settings.enabled:
            return False
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
        retryable = {
            job.id
            for job in jobs
            if job.status == "failed" and self._retryable(job)
        }
        queued = sum(job.status == "queued" or job.id in retryable for job in jobs)
        latest_terminal = next(
            (
                job
                for job in jobs
                if job.status in _TERMINAL_STATUSES and job.id not in retryable
            ),
            None,
        )
        running = current is not None and current.status in _RUNNING_STATUSES
        result = "FAIL" if latest_terminal and latest_terminal.status == "failed" else "PASS"
        current_stage = "idle"
        if current is not None:
            current_stage = _display(stage or self._job_stage(current), 120)
        schedule = self._schedule(current, stage, queued)
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
            "queuedJobs": queued,
            "launchdLabel": "com.inswave.code-fix-agent",
            "healthUrl": _health_url(self.config),
        }
        if latest_terminal is not None:
            payload["lastRun"] = latest_terminal.updated_at
        return payload

    def _schedule(self, current: Job | None, stage: str | None, queued: int) -> str:
        if current is None:
            return f"유휴 · 대기 {queued}건"
        repository = current.repository.rsplit("/", 1)[-1]
        current_stage = _display(stage or self._job_stage(current), 120)
        return f"{repository} #{current.id} · {current_stage} · 대기 {queued}건"

    def _retryable(self, job: Job) -> bool:
        repository = self.config.repository_by_id(job.repository_id)
        return repository.max_attempts == 0 or job.attempts < repository.max_attempts

    def _job_stage(self, job: Job) -> str:
        if job.status == "failed" and self._retryable(job):
            return "재시도 대기"
        return _status_stage(job.status)

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
