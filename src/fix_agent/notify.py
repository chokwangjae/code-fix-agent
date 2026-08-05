from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Callable, ContextManager, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import AppConfig, RepositoryConfig
from .discord import discord_event_payloads
from .errors import FixAgentError
from .state import StateStore


_RETRY_DELAYS = (5, 30, 120, 600)


class _Response(Protocol):
    status: int


_Open = Callable[..., ContextManager[_Response]]


@dataclass(frozen=True)
class DiscordDispatchResult:
    delivered: int = 0
    skipped: int = 0
    failed: int = 0
    deferred: int = 0

    def add(self, **values: int) -> DiscordDispatchResult:
        current = {
            "delivered": self.delivered,
            "skipped": self.skipped,
            "failed": self.failed,
            "deferred": self.deferred,
        }
        current.update(
            {key: current[key] + value for key, value in values.items()}
        )
        return DiscordDispatchResult(**current)


class DiscordNotifier:
    def __init__(
        self,
        config: AppConfig,
        *,
        opener: _Open = urlopen,
    ) -> None:
        self.config = config
        self.opener = opener

    def initialize_cursors(self) -> None:
        with StateStore(self.config.state_dir) as state:
            for repository in self.config.repositories:
                if repository.discord.enabled:
                    state.initialize_discord_cursor(repository.id)

    def dispatch_pending(
        self, *, force: bool = False, max_events: int = 100
    ) -> DiscordDispatchResult:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        result = DiscordDispatchResult()
        with StateStore(self.config.state_dir) as state:
            for repository in self.config.repositories:
                if not repository.discord.enabled:
                    continue
                state.initialize_discord_cursor(repository.id)
                cursor = state.discord_cursor(repository.id)
                if not force and _is_deferred(cursor.next_attempt_at):
                    result = result.add(deferred=1)
                    continue
                for _ in range(max_events):
                    candidate = state.next_discord_event(repository.id)
                    if candidate is None:
                        break
                    job, event = candidate
                    payloads = discord_event_payloads(job, event)
                    if not payloads:
                        state.advance_discord_cursor(repository.id, event.id)
                        result = result.add(skipped=1)
                        continue
                    try:
                        url, token = _webhook_access(repository)
                        for payload in payloads:
                            self._send(repository, url, token, payload)
                    except (FixAgentError, OSError, HTTPError, URLError) as exc:
                        cursor = state.discord_cursor(repository.id)
                        attempt = (
                            cursor.attempts + 1
                            if cursor.failed_event_id == event.id
                            else 1
                        )
                        delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                        state.fail_discord_event(
                            repository.id, event.id, _safe_error(exc), delay
                        )
                        result = result.add(failed=1)
                        break
                    state.advance_discord_cursor(repository.id, event.id)
                    result = result.add(delivered=1)
        return result

    def _send(
        self,
        repository: RepositoryConfig,
        url: str,
        token: str | None,
        payload: dict[str, object],
    ) -> None:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "code-fix-agent/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self.opener(
            request, timeout=repository.discord.timeout_seconds
        ) as response:
            if not 200 <= response.status < 300:
                raise FixAgentError(
                    f"Discord webhook returned HTTP {response.status}"
                )


def _webhook_access(repository: RepositoryConfig) -> tuple[str, str | None]:
    discord = repository.discord
    url = discord.webhook_url
    if discord.webhook_url_env:
        url = os.environ.get(discord.webhook_url_env)
        if not url:
            raise FixAgentError(
                "required Discord webhook environment variable is not set: "
                + discord.webhook_url_env
            )
    if not url:
        raise FixAgentError("Discord webhook URL is not configured")
    token = None
    if discord.webhook_token_env:
        token = os.environ.get(discord.webhook_token_env)
        if not token:
            raise FixAgentError(
                "required Discord webhook token environment variable is not set: "
                + discord.webhook_token_env
            )
    return url, token


def _is_deferred(value: str | None) -> bool:
    if value is None:
        return False
    try:
        next_attempt = datetime.fromisoformat(value)
    except ValueError:
        return False
    if next_attempt.tzinfo is None:
        return False
    return next_attempt > datetime.now(timezone.utc)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"Discord webhook returned HTTP {exc.code}"
    if isinstance(exc, URLError):
        return f"Discord webhook connection failed: {exc.reason}"
    return str(exc)
