from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Mapping, Sequence

from .errors import FixAgentError


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int = 1800,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=dict(environment) if environment is not None else None,
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise FixAgentError(f"required command not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FixAgentError(
                f"command timed out after {timeout_seconds}s: {command[0]}"
            ) from exc
        result = CommandResult(completed.stdout, completed.stderr, completed.returncode)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise FixAgentError(f"{command[0]} failed: {detail}")
        return result
