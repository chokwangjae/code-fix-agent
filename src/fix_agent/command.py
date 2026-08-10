from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
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
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(environment) if environment is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise FixAgentError(f"required command not found: {command[0]}") from exc

        try:
            stdout, stderr = process.communicate(
                input=input_text,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_group(process)
            raise FixAgentError(
                f"command timed out after {timeout_seconds}s: {command[0]}"
            ) from exc
        result = CommandResult(stdout, stderr, process.returncode)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise FixAgentError(f"{command[0]} failed: {detail}")
        return result

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
