from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any

from .errors import FixAgentError


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SEVERITIES = ("Critical", "Major", "Minor")


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    token_env: str
    max_body_bytes: int = 1_048_576


@dataclass(frozen=True)
class RepositoryPolicy:
    allowed_severities: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    skipped_fingerprints: tuple[str, ...]
    max_changed_files: int
    max_changed_lines: int
    allow_new_files: bool
    allow_deletions: bool

    def skip_reason(self, severity: str, file: str, fingerprint: str) -> str | None:
        if severity not in self.allowed_severities:
            return f"severity is not enabled: {severity}"
        if fingerprint in self.skipped_fingerprints:
            return "fingerprint is excluded by repository policy"
        if any(fnmatch.fnmatchcase(file, pattern) for pattern in self.skipped_paths):
            return "finding path is excluded by repository policy"
        if not any(fnmatch.fnmatchcase(file, pattern) for pattern in self.allowed_paths):
            return "finding path is outside allowed_paths"
        if any(fnmatch.fnmatchcase(file, pattern) for pattern in self.denied_paths):
            return "finding path matches denied_paths"
        return None

    def allows_changed_path(self, file: str) -> bool:
        path = PurePosixPath(file)
        if path.is_absolute() or ".." in path.parts:
            return False
        return (
            any(fnmatch.fnmatchcase(file, pattern) for pattern in self.allowed_paths)
            and not any(fnmatch.fnmatchcase(file, pattern) for pattern in self.denied_paths)
        )


@dataclass(frozen=True)
class RepositoryConfig:
    id: str
    github: str
    branch: str
    local_path: Path
    remote: str
    github_token_env: str
    test_commands: tuple[tuple[str, ...], ...]
    additional_instructions: str
    command_timeout_seconds: int
    max_attempts: int
    policy: RepositoryPolicy


@dataclass(frozen=True)
class AppConfig:
    state_dir: Path
    server: ServerConfig
    repositories: tuple[RepositoryConfig, ...]

    def repository(self, github: str, branch: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.github.casefold() == github.casefold() and repository.branch == branch:
                return repository
        raise FixAgentError(f"repository is not configured: {github}@{branch}")

    def repository_by_id(self, repository_id: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.id == repository_id:
                return repository
        raise FixAgentError(f"unknown repository id: {repository_id}")


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FixAgentError(f"cannot load configuration {path}: {exc}") from exc
    if raw.get("version") != 1:
        raise FixAgentError("configuration version must be 1")

    base = path.resolve().parent
    state_dir = _resolve_path(base, _required_string(raw, "state_dir"))
    server = _server(raw.get("server"))
    raw_repositories = raw.get("repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise FixAgentError("repositories must be a non-empty array")
    repositories = tuple(
        _repository(item, base, index) for index, item in enumerate(raw_repositories)
    )
    identifiers = [repository.id for repository in repositories]
    coordinates = [
        (repository.github.casefold(), repository.branch) for repository in repositories
    ]
    if len(identifiers) != len(set(identifiers)):
        raise FixAgentError("repository ids must be unique")
    if len(coordinates) != len(set(coordinates)):
        raise FixAgentError("repository and branch pairs must be unique")
    return AppConfig(state_dir, server, repositories)


def _server(raw: Any) -> ServerConfig:
    if not isinstance(raw, dict):
        raise FixAgentError("server must be a table")
    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise FixAgentError("server.host must be a non-empty string")
    port = _positive_integer(raw.get("port", 7081), "server.port", maximum=65535)
    max_body_bytes = _positive_integer(
        raw.get("max_body_bytes", 1_048_576), "server.max_body_bytes"
    )
    return ServerConfig(
        host,
        port,
        _environment_name(raw, "token_env", "server"),
        max_body_bytes,
    )


def _repository(raw: Any, base: Path, index: int) -> RepositoryConfig:
    context = f"repositories[{index}]"
    if not isinstance(raw, dict):
        raise FixAgentError(f"{context} must be a table")
    github = _required_string(raw, "github", context)
    if not _GITHUB_REPOSITORY.fullmatch(github):
        raise FixAgentError(f"{context}.github must use owner/repository")
    test_commands = _commands(raw.get("test_commands", []), context)
    additional_instructions = raw.get("additional_instructions", "")
    if not isinstance(additional_instructions, str):
        raise FixAgentError(f"{context}.additional_instructions must be a string")
    remote = raw.get("remote", "origin")
    if not isinstance(remote, str) or not remote:
        raise FixAgentError(f"{context}.remote must be a non-empty string")
    execution = raw.get("execution", {})
    if not isinstance(execution, dict):
        raise FixAgentError(f"{context}.execution must be a table")
    return RepositoryConfig(
        id=_required_string(raw, "id", context),
        github=github,
        branch=_required_string(raw, "branch", context),
        local_path=_resolve_path(base, _required_string(raw, "local_path", context)),
        remote=remote,
        github_token_env=_environment_name(raw, "github_token_env", context),
        test_commands=test_commands,
        additional_instructions=additional_instructions.strip(),
        command_timeout_seconds=_positive_integer(
            execution.get("command_timeout_seconds", 1800),
            f"{context}.execution.command_timeout_seconds",
        ),
        max_attempts=_positive_integer(
            execution.get("max_attempts", 1), f"{context}.execution.max_attempts"
        ),
        policy=_policy(raw.get("policy", {}), context),
    )


def _policy(raw: Any, context: str) -> RepositoryPolicy:
    context = context + ".policy"
    if not isinstance(raw, dict):
        raise FixAgentError(f"{context} must be a table")
    severities = _string_array(
        raw.get("allowed_severities", ["Major", "Minor"]),
        f"{context}.allowed_severities",
    )
    if any(value not in _SEVERITIES for value in severities):
        raise FixAgentError(f"{context}.allowed_severities contains an invalid severity")
    allowed_paths = _patterns(raw.get("allowed_paths", ["**"]), f"{context}.allowed_paths")
    denied_paths = _patterns(
        raw.get(
            "denied_paths",
            [
                ".github/workflows/**",
                "**/*.p12",
                "**/*.mobileprovision",
                "**/.env*",
            ],
        ),
        f"{context}.denied_paths",
        allow_empty=True,
    )
    skipped_paths = _patterns(
        raw.get("skipped_paths", []), f"{context}.skipped_paths", allow_empty=True
    )
    fingerprints = _string_array(
        raw.get("skipped_fingerprints", []),
        f"{context}.skipped_fingerprints",
        allow_empty=True,
    )
    if any(not _FINGERPRINT.fullmatch(value) for value in fingerprints):
        raise FixAgentError(f"{context}.skipped_fingerprints contains an invalid value")
    allow_new_files = raw.get("allow_new_files", False)
    allow_deletions = raw.get("allow_deletions", False)
    if not isinstance(allow_new_files, bool) or not isinstance(allow_deletions, bool):
        raise FixAgentError(f"{context} file flags must be booleans")
    return RepositoryPolicy(
        allowed_severities=severities,
        allowed_paths=allowed_paths,
        denied_paths=denied_paths,
        skipped_paths=skipped_paths,
        skipped_fingerprints=fingerprints,
        max_changed_files=_positive_integer(
            raw.get("max_changed_files", 10), f"{context}.max_changed_files"
        ),
        max_changed_lines=_positive_integer(
            raw.get("max_changed_lines", 500), f"{context}.max_changed_lines"
        ),
        allow_new_files=allow_new_files,
        allow_deletions=allow_deletions,
    )


def _commands(raw: Any, context: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list):
        raise FixAgentError(f"{context}.test_commands must be an array")
    commands = []
    for index, value in enumerate(raw):
        command = _string_array(value, f"{context}.test_commands[{index}]")
        commands.append(command)
    return tuple(commands)


def _patterns(raw: Any, context: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    values = _string_array(raw, context, allow_empty=allow_empty)
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise FixAgentError(f"{context} must contain repository-relative patterns")
    return values


def _string_array(raw: Any, context: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or (not raw and not allow_empty)
        or any(not isinstance(value, str) or not value for value in raw)
    ):
        suffix = "a string array" if allow_empty else "a non-empty string array"
        raise FixAgentError(f"{context} must be {suffix}")
    values = tuple(raw)
    if len(values) != len(set(values)):
        raise FixAgentError(f"{context} must not contain duplicates")
    return values


def _required_string(raw: dict[str, Any], key: str, context: str = "config") -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise FixAgentError(f"{context}.{key} must be a non-empty string")
    return value


def _environment_name(raw: dict[str, Any], key: str, context: str) -> str:
    value = _required_string(raw, key, context)
    if not _ENVIRONMENT_NAME.fullmatch(value):
        raise FixAgentError(f"{context}.{key} must be an environment variable name")
    return value


def _positive_integer(raw: Any, context: str, *, maximum: int | None = None) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise FixAgentError(f"{context} must be a positive integer")
    if maximum is not None and raw > maximum:
        raise FixAgentError(f"{context} must not exceed {maximum}")
    return raw


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()
