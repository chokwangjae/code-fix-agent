from __future__ import annotations

import os
from typing import Protocol, Sequence

from .command import CommandResult
from .errors import FixAgentError


class _Runner(Protocol):
    def run(self, command: Sequence[str], **kwargs: object) -> CommandResult: ...


def resolve_credential(
    direct: str | None,
    environment_name: str | None,
    description: str,
    *,
    required: bool = True,
) -> str | None:
    if direct:
        return direct
    value = os.environ.get(environment_name) if environment_name else None
    if value:
        return value
    if required:
        if environment_name:
            raise FixAgentError(
                f"required environment variable is not set: {environment_name}"
            )
        raise FixAgentError(f"{description} is not configured")
    return None


def resolve_github_credential(
    direct: str | None,
    environment_name: str | None,
    runner: _Runner,
    *,
    required: bool = True,
) -> str | None:
    if direct:
        return direct
    if environment_name:
        value = os.environ.get(environment_name)
        if value:
            return value
    try:
        result = runner.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            environment=_safe_gh_environment(),
            timeout_seconds=30,
            check=False,
        )
    except FixAgentError as exc:
        if not required:
            return None
        raise FixAgentError(
            "GitHub authentication is unavailable; run gh auth login or configure "
            "github_token/github_token_env"
        ) from exc
    value = result.stdout.strip() if result.returncode == 0 else ""
    if value:
        return value
    if required:
        raise FixAgentError(
            "GitHub authentication is unavailable; run gh auth login or configure "
            "github_token/github_token_env"
        )
    return None


def _safe_gh_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if (
            name in {"GH_TOKEN", "GITHUB_TOKEN"}
            or name.endswith(("_TOKEN", "_SECRET", "_PASSWORD"))
            or "WEBHOOK" in name
        ):
            environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment
