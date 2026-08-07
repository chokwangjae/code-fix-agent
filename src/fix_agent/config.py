from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any
from urllib.parse import urlsplit

from .errors import FixAgentError


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SEVERITIES = ("Critical", "Major", "Minor")
_PUBLISH_MODES = ("pull_request", "direct")
_DEFAULT_SETUP_WATCH_PATHS = (
    "package.json",
    "package-lock.json",
    "**/package.json",
    "**/package-lock.json",
    "pubspec.yaml",
    "pubspec.lock",
    "**/pubspec.yaml",
    "**/pubspec.lock",
    "Podfile",
    "Podfile.lock",
    "**/Podfile",
    "**/Podfile.lock",
    "gradle/wrapper/gradle-wrapper.properties",
    "**/gradle/wrapper/gradle-wrapper.properties",
    "*.gradle",
    "*.gradle.kts",
    "**/*.gradle",
    "**/*.gradle.kts",
)


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    token: str | None = field(repr=False)
    token_env: str | None
    max_body_bytes: int = 1_048_576
    max_concurrent_jobs: int = 1


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool
    webhook_url: str | None = field(repr=False)
    webhook_url_env: str | None
    webhook_token_env: str | None
    timeout_seconds: int


@dataclass(frozen=True)
class CrontrolConfig:
    enabled: bool
    base_url: str
    job_id: str
    name: str
    branch: str
    token: str | None = field(repr=False)
    token_env: str | None
    timeout_seconds: int


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
    require_finding_file_changed: bool

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
    target_branch: str
    local_path: Path
    remote: str
    publish_mode: str
    github_token: str | None = field(repr=False)
    github_token_env: str | None
    discord: DiscordConfig
    setup_commands: tuple[tuple[str, ...], ...]
    setup_watch_paths: tuple[str, ...]
    test_commands: tuple[tuple[str, ...], ...]
    additional_instructions: str
    commit_message_template: str
    git_author_name: str
    git_author_email: str
    command_timeout_seconds: int
    setup_max_attempts: int
    setup_retry_delay_seconds: int
    max_attempts: int
    retry_delay_seconds: int
    max_remote_merge_attempts: int
    policy: RepositoryPolicy

    @property
    def branch(self) -> str:
        """Return the configured review and publish target branch."""
        return self.target_branch


@dataclass(frozen=True)
class AppConfig:
    state_dir: Path
    codex_executable: str | None
    server: ServerConfig
    repositories: tuple[RepositoryConfig, ...]
    crontrol: CrontrolConfig

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
    crontrol = _crontrol(raw.get("crontrol"))
    codex_executable = raw.get("codex_executable")
    if codex_executable is not None and (
        not isinstance(codex_executable, str) or not codex_executable
    ):
        raise FixAgentError("codex_executable must be a non-empty string")
    raw_repositories = raw.get("repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        raise FixAgentError("repositories must be a non-empty array")
    repositories = tuple(
        _repository(item, base, index) for index, item in enumerate(raw_repositories)
    )
    identifiers = [repository.id for repository in repositories]
    coordinates = [
        (repository.github.casefold(), repository.target_branch)
        for repository in repositories
    ]
    if len(identifiers) != len(set(identifiers)):
        raise FixAgentError("repository ids must be unique")
    if len(coordinates) != len(set(coordinates)):
        raise FixAgentError("repository and branch pairs must be unique")
    return AppConfig(state_dir, codex_executable, server, repositories, crontrol)


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
    max_concurrent_jobs = _positive_integer(
        raw.get("max_concurrent_jobs", 1),
        "server.max_concurrent_jobs",
        maximum=32,
    )
    token, token_env = _credential_source(raw, "token", "token_env", "server")
    return ServerConfig(
        host, port, token, token_env, max_body_bytes, max_concurrent_jobs
    )


def _crontrol(raw: Any) -> CrontrolConfig:
    if raw is None:
        return CrontrolConfig(
            False,
            "http://127.0.0.1:7070",
            "code-fix-agent-server",
            "Code Fix Agent",
            "main",
            None,
            None,
            5,
        )
    if not isinstance(raw, dict):
        raise FixAgentError("crontrol must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise FixAgentError("crontrol.enabled must be a boolean")
    base_url = raw.get("base_url", "http://127.0.0.1:7070")
    if not isinstance(base_url, str):
        raise FixAgentError("crontrol.base_url must be an HTTP URL")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"https", "http"}
        or not parsed_url.netloc
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise FixAgentError("crontrol.base_url must be an HTTP URL")
    token, token_env = _credential_source(
        raw, "token", "token_env", "crontrol", required=False
    )
    return CrontrolConfig(
        enabled,
        base_url.rstrip("/"),
        _required_string(
            {"job_id": raw.get("job_id", "code-fix-agent-server")},
            "job_id",
            "crontrol",
        ),
        _required_string(
            {"name": raw.get("name", "Code Fix Agent")}, "name", "crontrol"
        ),
        _required_string(
            {"branch": raw.get("branch", "main")}, "branch", "crontrol"
        ),
        token,
        token_env,
        _positive_integer(
            raw.get("timeout_seconds", 5), "crontrol.timeout_seconds"
        ),
    )


def _repository(raw: Any, base: Path, index: int) -> RepositoryConfig:
    context = f"repositories[{index}]"
    if not isinstance(raw, dict):
        raise FixAgentError(f"{context} must be a table")
    github = _required_string(raw, "github", context)
    if not _GITHUB_REPOSITORY.fullmatch(github):
        raise FixAgentError(f"{context}.github must use owner/repository")
    setup_commands = _commands(
        raw.get("setup_commands", []), context, "setup_commands"
    )
    setup_watch_paths = _patterns(
        raw.get("setup_watch_paths", list(_DEFAULT_SETUP_WATCH_PATHS)),
        f"{context}.setup_watch_paths",
        allow_empty=True,
    )
    test_commands = _commands(raw.get("test_commands", []), context, "test_commands")
    additional_instructions = raw.get("additional_instructions", "")
    if not isinstance(additional_instructions, str):
        raise FixAgentError(f"{context}.additional_instructions must be a string")
    commit_message_template = raw.get(
        "commit_message_template", "fix: resolve review finding {fingerprint}"
    )
    if not isinstance(commit_message_template, str) or not commit_message_template.strip():
        raise FixAgentError(f"{context}.commit_message_template must be a string")
    try:
        commit_message_template.format(
            fingerprint="sha256:" + "0" * 64,
            fingerprint_short="0" * 12,
            file="path/to/file",
        )
    except (KeyError, ValueError) as exc:
        raise FixAgentError(
            f"{context}.commit_message_template contains an invalid placeholder"
        ) from exc
    remote = raw.get("remote", "origin")
    if not isinstance(remote, str) or not _GIT_REMOTE.fullmatch(remote):
        raise FixAgentError(f"{context}.remote must be a simple Git remote name")
    target_branch = raw.get("target_branch", raw.get("branch"))
    if not isinstance(target_branch, str) or not target_branch:
        raise FixAgentError(f"{context}.target_branch must be a non-empty string")
    legacy_branch = raw.get("branch")
    if legacy_branch is not None and legacy_branch != target_branch:
        raise FixAgentError(f"{context}.branch and target_branch must match")
    publish_mode = raw.get("publish_mode", "pull_request")
    if publish_mode not in _PUBLISH_MODES:
        raise FixAgentError(
            f"{context}.publish_mode must be one of: {', '.join(_PUBLISH_MODES)}"
        )
    execution = raw.get("execution", {})
    if not isinstance(execution, dict):
        raise FixAgentError(f"{context}.execution must be a table")
    github_token, github_token_env = _credential_source(
        raw, "github_token", "github_token_env", context, required=False
    )
    git_author_name = raw.get("git_author_name", "Code Fix Agent")
    git_author_email = raw.get(
        "git_author_email", "code-fix-agent@users.noreply.github.com"
    )
    if not isinstance(git_author_name, str) or not git_author_name.strip():
        raise FixAgentError(f"{context}.git_author_name must be a non-empty string")
    if not isinstance(git_author_email, str) or not git_author_email.strip():
        raise FixAgentError(f"{context}.git_author_email must be a non-empty string")
    return RepositoryConfig(
        id=_required_string(raw, "id", context),
        github=github,
        target_branch=target_branch,
        local_path=_resolve_path(base, _required_string(raw, "local_path", context)),
        remote=remote,
        publish_mode=publish_mode,
        github_token=github_token,
        github_token_env=github_token_env,
        discord=_discord(raw.get("discord"), context),
        setup_commands=setup_commands,
        setup_watch_paths=setup_watch_paths,
        test_commands=test_commands,
        additional_instructions=additional_instructions.strip(),
        commit_message_template=commit_message_template.strip(),
        git_author_name=git_author_name.strip(),
        git_author_email=git_author_email.strip(),
        command_timeout_seconds=_positive_integer(
            execution.get("command_timeout_seconds", 1800),
            f"{context}.execution.command_timeout_seconds",
        ),
        setup_max_attempts=_positive_integer(
            execution.get("setup_max_attempts", 3),
            f"{context}.execution.setup_max_attempts",
        ),
        setup_retry_delay_seconds=_nonnegative_integer(
            execution.get("setup_retry_delay_seconds", 15),
            f"{context}.execution.setup_retry_delay_seconds",
        ),
        max_attempts=_nonnegative_integer(
            execution.get("max_attempts", 1), f"{context}.execution.max_attempts"
        ),
        retry_delay_seconds=_nonnegative_integer(
            execution.get("retry_delay_seconds", 60),
            f"{context}.execution.retry_delay_seconds",
        ),
        max_remote_merge_attempts=_positive_integer(
            execution.get("max_remote_merge_attempts", 3),
            f"{context}.execution.max_remote_merge_attempts",
        ),
        policy=_policy(raw.get("policy", {}), context),
    )


def _discord(raw: Any, context: str) -> DiscordConfig:
    context += ".discord"
    if raw is None:
        return DiscordConfig(False, None, None, None, 30)
    if not isinstance(raw, dict):
        raise FixAgentError(f"{context} must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise FixAgentError(f"{context}.enabled must be a boolean")
    webhook_url = raw.get("webhook_url")
    if webhook_url is not None:
        if not isinstance(webhook_url, str):
            raise FixAgentError(f"{context}.webhook_url must be an HTTP URL")
        parsed_url = urlsplit(webhook_url)
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
            raise FixAgentError(f"{context}.webhook_url must be an HTTP URL")
    webhook_url_env = _optional_environment_name(
        raw.get("webhook_url_env"), f"{context}.webhook_url_env"
    )
    webhook_token_env = _optional_environment_name(
        raw.get("webhook_token_env"), f"{context}.webhook_token_env"
    )
    if webhook_url and webhook_url_env:
        raise FixAgentError(
            f"{context} must not set both webhook_url and webhook_url_env"
        )
    if enabled and not (webhook_url or webhook_url_env):
        raise FixAgentError(
            f"{context} must set webhook_url or webhook_url_env when enabled"
        )
    return DiscordConfig(
        enabled,
        webhook_url,
        webhook_url_env,
        webhook_token_env,
        _positive_integer(
            raw.get("timeout_seconds", 30), f"{context}.timeout_seconds"
        ),
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
                ".env*",
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
    require_finding_file_changed = raw.get("require_finding_file_changed", True)
    if (
        not isinstance(allow_new_files, bool)
        or not isinstance(allow_deletions, bool)
        or not isinstance(require_finding_file_changed, bool)
    ):
        raise FixAgentError(f"{context} file flags must be booleans")
    return RepositoryPolicy(
        allowed_severities=severities,
        allowed_paths=allowed_paths,
        denied_paths=denied_paths,
        skipped_paths=skipped_paths,
        skipped_fingerprints=fingerprints,
        max_changed_files=_nonnegative_integer(
            raw.get("max_changed_files", 0), f"{context}.max_changed_files"
        ),
        max_changed_lines=_nonnegative_integer(
            raw.get("max_changed_lines", 0), f"{context}.max_changed_lines"
        ),
        allow_new_files=allow_new_files,
        allow_deletions=allow_deletions,
        require_finding_file_changed=require_finding_file_changed,
    )


def _commands(
    raw: Any, context: str, key: str
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list):
        raise FixAgentError(f"{context}.{key} must be an array")
    commands = []
    for index, value in enumerate(raw):
        command = _string_array(value, f"{context}.{key}[{index}]")
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


def _optional_environment_name(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ENVIRONMENT_NAME.fullmatch(value):
        raise FixAgentError(f"{context} must be an environment variable name")
    return value


def _credential_source(
    raw: dict[str, Any],
    direct_key: str,
    environment_key: str,
    context: str,
    *,
    required: bool = True,
) -> tuple[str | None, str | None]:
    direct = raw.get(direct_key)
    if direct is not None and (not isinstance(direct, str) or not direct):
        raise FixAgentError(f"{context}.{direct_key} must be a non-empty string")
    environment = _optional_environment_name(
        raw.get(environment_key), f"{context}.{environment_key}"
    )
    if direct and environment:
        raise FixAgentError(
            f"{context} must not set both {direct_key} and {environment_key}"
        )
    if required and not direct and not environment:
        raise FixAgentError(
            f"{context} must set {direct_key} or {environment_key}"
        )
    return direct, environment


def _positive_integer(raw: Any, context: str, *, maximum: int | None = None) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise FixAgentError(f"{context} must be a positive integer")
    if maximum is not None and raw > maximum:
        raise FixAgentError(f"{context} must not exceed {maximum}")
    return raw


def _nonnegative_integer(raw: Any, context: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise FixAgentError(f"{context} must be a non-negative integer")
    return raw


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()
