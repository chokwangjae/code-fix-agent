from __future__ import annotations

import base64
from dataclasses import dataclass
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading

from .command import CommandRunner
from .config import RepositoryConfig
from .credentials import resolve_github_credential
from .errors import FixAgentError
from .state import Job, StateStore


_CREDENTIAL_ENVIRONMENT = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_REPOSITORY_LOCKS: dict[Path, threading.RLock] = {}
_REPOSITORY_LOCKS_GUARD = threading.Lock()


def _repository_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _REPOSITORY_LOCKS_GUARD:
        return _REPOSITORY_LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True)
class DiffSummary:
    files: tuple[str, ...]
    added_lines: int
    deleted_lines: int


@dataclass(frozen=True)
class MergeResult:
    updated: bool
    previous_base: str
    current_target: str
    conflict_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PathSnapshot:
    exists: bool
    contents: bytes | None = None
    link_target: str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class PermissionSummary:
    checked_files: int
    checked_directories: int
    repaired_files: int
    repaired_directories: int
    cache_root: str

    @property
    def repaired(self) -> bool:
        return bool(self.repaired_files or self.repaired_directories)


@dataclass(frozen=True)
class FindingCommit:
    fingerprints: tuple[str, ...]
    files: tuple[str, ...]
    title: str
    commit: str


class FixWorkspace:
    def __init__(
        self,
        runner: CommandRunner,
        repository: RepositoryConfig,
        job: Job,
        state_dir: Path,
        *,
        resumable_worktree: tuple[str, str] | None = None,
        worktree_scope: str = "finding",
    ) -> None:
        if worktree_scope not in {"batch", "finding"}:
            raise ValueError(f"invalid worktree scope: {worktree_scope}")
        self.runner = runner
        self.repository = repository
        self.job = job
        self.state_dir = state_dir
        worktrees = state_dir / "worktrees"
        worktrees.mkdir(parents=True, exist_ok=True)
        self._resumable_worktree = resumable_worktree
        self.worktree_scope = worktree_scope
        if resumable_worktree is None:
            self.root = Path(tempfile.mkdtemp(prefix="fix-", dir=worktrees))
            self.path = self.root / "checkout"
        else:
            self.path = Path(resumable_worktree[0]).resolve()
            self.root = self.path.parent
        self._created = False
        self.base_commit: str | None = None
        self.cleanup_complete: bool | None = None
        self._preserve_on_exit = False
        self._held_publish_lock: threading.RLock | None = None
        self._github_token_loaded = False
        self._github_token: str | None = None
        self._cache_environment: dict[str, str] | None = None

    def resume(self) -> None:
        if self._resumable_worktree is None:
            raise FixAgentError("no recorded worktree is available to resume")
        worktree_root = (self.state_dir / "worktrees").resolve()
        recorded_path, recorded_base = self._resumable_worktree
        path = Path(recorded_path).resolve()
        if (
            path != self.path
            or path.name != "checkout"
            or not path.is_relative_to(worktree_root)
            or not path.parent.name.startswith("fix-")
        ):
            raise FixAgentError("recorded worktree path is outside the managed root")
        if not path.is_dir() or not (path / ".git").is_file():
            raise FixAgentError(f"recorded worktree does not exist: {path}")
        with _repository_lock(self.repository.local_path):
            self._ensure_local_repository()
            listed = self.runner.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
            )
            if not any(
                line == f"worktree {path}" for line in listed.stdout.splitlines()
            ):
                raise FixAgentError("recorded worktree is not registered")
            current_target = self.fetch_target_head()
            merge_base = self.runner.run(
                ["git", "merge-base", "HEAD", current_target],
                cwd=path,
                environment=self.safe_environment,
            ).stdout.strip().lower()
        self._created = True
        self.base_commit = merge_base or recorded_base.lower()
        permissions = self.ensure_writable()
        self._record_event(
            "worktree_resumed",
            "recorded worktree and its existing changes were resumed",
            {
                "path": str(path),
                "recorded_base_commit": recorded_base,
                "base_commit": self.base_commit,
                "scope": self.worktree_scope,
                "checked_files": permissions.checked_files,
                "checked_directories": permissions.checked_directories,
                "repaired_files": permissions.repaired_files,
                "repaired_directories": permissions.repaired_directories,
            },
        )

    @property
    def safe_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        credential_names = {
            name
            for name in environment
            if name.endswith(("_TOKEN", "_SECRET", "_PASSWORD"))
            or "WEBHOOK" in name
        }
        configured_names = (
            {self.repository.github_token_env}
            if self.repository.github_token_env
            else set()
        )
        for name in _CREDENTIAL_ENVIRONMENT | credential_names | configured_names:
            environment.pop(name, None)
        environment.update(self._runtime_cache_environment())
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    def _runtime_cache_environment(self) -> dict[str, str]:
        if self._cache_environment is not None:
            return self._cache_environment
        cache_key = hashlib.sha256(self.repository.id.encode("utf-8")).hexdigest()[:16]
        cache_root = self.state_dir / "runtime-cache" / cache_key
        paths = {
            "NPM_CONFIG_CACHE": cache_root / "npm",
            "GRADLE_USER_HOME": cache_root / "gradle",
            "PUB_CACHE": cache_root / "pub",
            "PLAYWRIGHT_BROWSERS_PATH": cache_root / "playwright",
            "CP_HOME_DIR": cache_root / "cocoapods",
            "TMPDIR": self.root / "tmp",
        }
        for path in (cache_root, *paths.values()):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._grant_owner_directory_access(path)
        self._sync_gradle_init_scripts(paths["GRADLE_USER_HOME"])
        self._cache_environment = {
            name: str(path.resolve()) for name, path in paths.items()
        }
        return self._cache_environment

    def _sync_gradle_init_scripts(self, gradle_home: Path) -> None:
        source_root = Path.home() / ".gradle" / "init.d"
        if not source_root.is_dir():
            return
        target_root = gradle_home / "init.d"
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._grant_owner_directory_access(target_root)
        for source in source_root.iterdir():
            if (
                not source.is_file()
                or source.is_symlink()
                or source.suffix not in {".gradle", ".kts"}
            ):
                continue
            target = target_root / source.name
            if target.is_symlink():
                target.unlink()
            if not target.exists() or target.read_bytes() != source.read_bytes():
                shutil.copyfile(source, target)
            self._grant_owner_file_access(target)

    def create(self) -> None:
        with _repository_lock(self.repository.local_path):
            self._ensure_local_repository()
            self.base_commit = self.fetch_target_head()
            ancestry = self.runner.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    self.job.target_commit,
                    self.base_commit,
                ],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                check=False,
            )
            if ancestry.returncode != 0:
                raise FixAgentError(
                    "reviewed target is not an ancestor of the current target branch"
                )
            self.runner.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(self.path),
                    self.base_commit,
                ],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                timeout_seconds=self.repository.command_timeout_seconds,
            )
        self._created = True
        permissions = self.ensure_writable()
        self._record_event(
            "worktree_created",
            "detached worktree created from the latest target branch",
            {
                "path": str(self.path),
                "remote": self.repository.remote,
                "target_branch": self.repository.target_branch,
                "base_commit": self.base_commit,
                "scope": self.worktree_scope,
            },
        )
        self._record_event(
            "worktree_permissions_ready",
            "managed worktree and runtime cache permissions verified",
            {
                "checked_files": permissions.checked_files,
                "checked_directories": permissions.checked_directories,
                "repaired_files": permissions.repaired_files,
                "repaired_directories": permissions.repaired_directories,
                "cache_root": permissions.cache_root,
            },
        )

    def ensure_writable(self) -> PermissionSummary:
        if not self.path.is_dir():
            raise FixAgentError(f"managed worktree does not exist: {self.path}")
        files = tuple(
            sorted(
                file
                for file in self.runner.run(
                    [
                        "git",
                        "ls-files",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                        "-z",
                    ],
                    cwd=self.path,
                    environment=self.safe_environment,
                ).stdout.split("\0")
                if file
            )
        )
        directories = {self.path}
        regular_files: list[Path] = []
        for file in files:
            candidate = self.path / file
            parent = candidate.parent
            while parent != self.path:
                if parent.is_dir():
                    directories.add(parent)
                parent = parent.parent
            if candidate.is_symlink() or not candidate.exists():
                continue
            if candidate.is_dir():
                directories.add(candidate)
                continue
            if not candidate.is_file():
                raise FixAgentError(
                    f"managed worktree path is not a regular file: {file}"
                )
            regular_files.append(candidate)
        git_file = self.path / ".git"
        repaired_directories = sum(
            self._grant_owner_directory_access(directory)
            for directory in sorted(directories, key=lambda value: len(value.parts))
        )
        repaired_files = 0
        if git_file.is_file():
            repaired_files += self._grant_owner_file_access(git_file)
        for candidate in regular_files:
            repaired_files += self._grant_owner_file_access(candidate)
        probe = self.path / f".fix-agent-write-probe-{os.getpid()}-{threading.get_ident()}"
        try:
            probe.write_text("writable\n", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise FixAgentError(
                f"managed worktree is not writable: {self.path}: {exc}"
            ) from exc
        cache_root = Path(self.safe_environment["NPM_CONFIG_CACHE"]).parent
        return PermissionSummary(
            checked_files=len(regular_files) + int(git_file.is_file()),
            checked_directories=len(directories),
            repaired_files=repaired_files,
            repaired_directories=repaired_directories,
            cache_root=str(cache_root),
        )

    @staticmethod
    def _grant_owner_directory_access(path: Path) -> int:
        try:
            current = stat.S_IMODE(path.stat().st_mode)
            required = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
            desired = current | required
            if desired != current:
                path.chmod(desired)
                return 1
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise FixAgentError(f"directory remains inaccessible: {path}")
            return 0
        except FileNotFoundError:
            # A tracked file can disappear with its parent during a concurrent edit.
            return 0
        except OSError as exc:
            raise FixAgentError(f"cannot make directory writable: {path}: {exc}") from exc

    @staticmethod
    def _grant_owner_file_access(path: Path) -> int:
        try:
            current = stat.S_IMODE(path.stat().st_mode)
            required = stat.S_IRUSR | stat.S_IWUSR
            desired = current | required
            if desired != current:
                path.chmod(desired)
                return 1
            if not os.access(path, os.R_OK | os.W_OK):
                raise FixAgentError(f"file remains inaccessible: {path}")
            return 0
        except OSError as exc:
            raise FixAgentError(f"cannot make file writable: {path}: {exc}") from exc

    def _ensure_local_repository(self) -> None:
        path = self.repository.local_path
        if path.exists():
            if not path.is_dir():
                raise FixAgentError(f"local repository is not a directory: {path}")
            self.runner.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=path,
                environment=self.safe_environment,
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--origin",
                self.repository.remote,
                "--",
                f"https://github.com/{self.repository.github}.git",
                str(path),
            ],
            cwd=path.parent,
            environment=self.network_environment,
            timeout_seconds=self.repository.command_timeout_seconds,
        )

    def finding_mismatch_reason(self, job: Job | None = None) -> str | None:
        job = job or self.job
        ancestry = self.runner.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                job.introducing_commit,
                job.target_commit,
            ],
            cwd=self.path,
            environment=self.safe_environment,
            check=False,
        )
        if ancestry.returncode != 0:
            return "introducing commit is not an ancestor of the reviewed target"
        commit_files = self.runner.run(
            [
                "git",
                "diff-tree",
                "-m",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                job.introducing_commit,
                "--",
                job.file,
            ],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.splitlines()
        if job.file not in commit_files:
            return "introducing commit does not change the finding file"
        diff = self.runner.run(
            [
                "git",
                "diff",
                "--unified=0",
                "--no-color",
                job.baseline_commit,
                job.target_commit,
                "--",
                job.file,
            ],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout
        changed_lines: set[int] = set()
        for line in diff.splitlines():
            match = _HUNK.match(line)
            if match:
                start = int(match.group(1))
                length = int(match.group(2) or "1")
                changed_lines.update(range(start, start + length))
        if job.line not in changed_lines:
            return "finding line is outside the reviewed diff"
        return None

    @property
    def network_environment(self) -> dict[str, str]:
        environment = self.safe_environment
        if not self._github_token_loaded:
            self._github_token = resolve_github_credential(
                self.repository.github_token,
                self.repository.github_token_env,
                self.runner,
                required=False,
            )
            self._github_token_loaded = True
        token = self._github_token
        if not token:
            return environment
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
        return environment

    def fetch_target_head(self) -> str:
        repository = self.repository
        with _repository_lock(repository.local_path):
            self.runner.run(
                ["git", "check-ref-format", "--branch", repository.branch],
                cwd=repository.local_path,
                environment=self.safe_environment,
            )
            self.runner.run(
                [
                    "git",
                    "fetch",
                    "--prune",
                    "--no-tags",
                    "--",
                    repository.remote,
                    f"+refs/heads/{repository.branch}:refs/remotes/"
                    f"{repository.remote}/{repository.branch}",
                ],
                cwd=repository.local_path,
                environment=self.network_environment,
                timeout_seconds=repository.command_timeout_seconds,
            )
            result = self.runner.run(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    f"refs/remotes/{repository.remote}/{repository.branch}^{{commit}}",
                ],
                cwd=repository.local_path,
                environment=self.safe_environment,
            )
        return result.stdout.strip().lower()

    def merge_latest_target(self, current: str | None = None) -> MergeResult:
        current = current or self.fetch_target_head()
        previous_base = self._base_commit()
        if current == previous_base:
            return MergeResult(False, previous_base, current)
        result = self.runner.run(
            [
                "git",
                "-c",
                f"user.name={self.repository.git_author_name}",
                "-c",
                f"user.email={self.repository.git_author_email}",
                "-c",
                "commit.gpgsign=false",
                "merge",
                "--no-edit",
                current,
            ],
            cwd=self.path,
            environment=self.safe_environment,
            timeout_seconds=self.repository.command_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            conflicts = self.unmerged_files()
            if conflicts:
                return MergeResult(True, previous_base, current, conflicts)
            detail = (result.stderr or result.stdout or "merge failed").strip()
            raise FixAgentError(f"git merge failed: {detail}")
        self.base_commit = current
        return MergeResult(True, previous_base, current)

    def unmerged_files(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in self.runner.run(
                ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
                cwd=self.path,
                environment=self.safe_environment,
            ).stdout.split("\0")
            if value
        )

    def complete_conflicted_merge(self, current_target: str) -> None:
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )
        conflicts = self.unmerged_files()
        if conflicts:
            raise FixAgentError(
                "merge conflict resolution left unmerged files: "
                + ", ".join(conflicts)
            )
        self.runner.run(
            ["git", "diff", "--cached", "--check"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        self.runner.run(
            [
                "git",
                "-c",
                f"user.name={self.repository.git_author_name}",
                "-c",
                f"user.email={self.repository.git_author_email}",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-edit",
            ],
            cwd=self.path,
            environment=self.safe_environment,
            timeout_seconds=self.repository.command_timeout_seconds,
        )
        self.base_commit = current_target

    def validate_diff(
        self, required_finding_files: tuple[str, ...] | None = None
    ) -> DiffSummary:
        untracked = tuple(
            path
            for path in self.runner.run(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                cwd=self.path,
                environment=self.safe_environment,
            ).stdout.split("\0")
            if path
        )
        if untracked:
            self.runner.run(
                ["git", "add", "-N", "--", *untracked],
                cwd=self.path,
                environment=self.safe_environment,
            )
        raw_status = self.runner.run(
            [
                "git",
                "diff",
                "--name-status",
                "--no-renames",
                "-z",
                self._base_commit(),
                "--",
            ],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout
        parts = [value for value in raw_status.split("\0") if value]
        if len(parts) % 2:
            raise FixAgentError("Git returned an invalid name-status result")
        changes = tuple(zip(parts[0::2], parts[1::2]))
        if not changes:
            raise FixAgentError("fix produced no file changes")
        policy = self.repository.policy
        if policy.max_changed_files and len(changes) > policy.max_changed_files:
            raise FixAgentError(
                f"fix changed {len(changes)} files; limit is {policy.max_changed_files}"
            )
        files = tuple(file for _, file in changes)
        required = (
            (self.job.file,)
            if required_finding_files is None
            else required_finding_files
        )
        if policy.require_finding_file_changed:
            missing = sorted(set(required).difference(files))
            if missing:
                raise FixAgentError(
                    "fix did not change the finding file(s): " + ", ".join(missing)
                )
        for status, file in changes:
            if status not in {"M", "A", "D"}:
                raise FixAgentError(f"unsupported Git change type {status}: {file}")
            if not policy.allows_changed_path(file):
                raise FixAgentError(f"changed path is not allowed: {file}")
            if status == "A" and not policy.allow_new_files:
                raise FixAgentError(f"new files are not allowed: {file}")
            if status == "D" and not policy.allow_deletions:
                raise FixAgentError(f"file deletions are not allowed: {file}")
            candidate = self.path / file
            if candidate.exists() and candidate.is_symlink():
                raise FixAgentError(f"symbolic link changes are not allowed: {file}")
            if candidate.exists() and not candidate.resolve().is_relative_to(
                self.path.resolve()
            ):
                raise FixAgentError(f"changed path escapes the worktree: {file}")
        added = deleted = 0
        numstat = self.runner.run(
            ["git", "diff", "--numstat", self._base_commit(), "--"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout
        for line in numstat.splitlines():
            columns = line.split("\t", 2)
            if len(columns) != 3 or "-" in columns[:2]:
                raise FixAgentError("binary or malformed diff is not allowed")
            added += int(columns[0])
            deleted += int(columns[1])
        if policy.max_changed_lines and added + deleted > policy.max_changed_lines:
            raise FixAgentError(
                f"fix changed {added + deleted} lines; limit is {policy.max_changed_lines}"
            )
        self.runner.run(
            ["git", "diff", "--check", self._base_commit(), "--"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        return DiffSummary(files, added, deleted)

    def commit_finding_groups(
        self,
        groups: tuple[tuple[tuple[str, ...], tuple[str, ...], str, Job], ...],
    ) -> tuple[FindingCommit, ...]:
        changed = set(self.validate_diff(tuple()).files)
        grouped = {file for _, files, _, _ in groups for file in files}
        if changed != grouped:
            missing = sorted(changed.difference(grouped))
            extra = sorted(grouped.difference(changed))
            raise FixAgentError(
                "batch change groups do not match the working diff: "
                f"missing={missing}, extra={extra}"
            )
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )
        validated_tree = self.runner.run(
            ["git", "write-tree"], cwd=self.path, environment=self.safe_environment
        ).stdout.strip()
        self.runner.run(
            ["git", "reset", "--mixed", "HEAD"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        commits: list[FindingCommit] = []
        for fingerprints, files, title, job in groups:
            self.runner.run(
                ["git", "add", "--all", "--", *files],
                cwd=self.path,
                environment=self.safe_environment,
            )
            staged = {
                value
                for value in self.runner.run(
                    ["git", "diff", "--cached", "--name-only", "-z"],
                    cwd=self.path,
                    environment=self.safe_environment,
                ).stdout.split("\0")
                if value
            }
            if staged != set(files):
                raise FixAgentError(
                    "finding commit staged unexpected files: "
                    f"expected={sorted(files)}, actual={sorted(staged)}"
                )
            message = self._commit_message(job, title)
            self.runner.run(
                [
                    "git",
                    "-c",
                    f"user.name={self.repository.git_author_name}",
                    "-c",
                    f"user.email={self.repository.git_author_email}",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    message,
                ],
                cwd=self.path,
                environment=self.safe_environment,
                timeout_seconds=self.repository.command_timeout_seconds,
            )
            commits.append(
                FindingCommit(
                    fingerprints,
                    files,
                    title,
                    self.head_commit(),
                )
            )
        final_tree = self.runner.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.strip()
        if final_tree != validated_tree:
            raise FixAgentError("finding commit chain differs from the validated tree")
        return tuple(commits)

    def discard_group_changes(self, files: tuple[str, ...]) -> None:
        if not files:
            return
        self.runner.run(
            ["git", "reset", "--mixed", "HEAD", "--", *files],
            cwd=self.path,
            environment=self.safe_environment,
        )
        tracked: list[str] = []
        for file in files:
            exists = self.runner.run(
                ["git", "cat-file", "-e", f"{self._base_commit()}:{file}"],
                cwd=self.path,
                environment=self.safe_environment,
                check=False,
            )
            if exists.returncode == 0:
                tracked.append(file)
        if tracked:
            self.runner.run(
                [
                    "git",
                    "restore",
                    "--source",
                    self._base_commit(),
                    "--staged",
                    "--worktree",
                    "--",
                    *tracked,
                ],
                cwd=self.path,
                environment=self.safe_environment,
            )
        untracked = sorted(set(files).difference(tracked))
        if untracked:
            self.runner.run(
                ["git", "clean", "-fd", "--", *untracked],
                cwd=self.path,
                environment=self.safe_environment,
            )

    def flatten_batch_to_target(self, target_commit: str) -> None:
        final_tree = self.runner.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.strip()
        self.runner.run(
            ["git", "reset", "--mixed", target_commit],
            cwd=self.path,
            environment=self.safe_environment,
        )
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )
        working_tree = self.runner.run(
            ["git", "write-tree"], cwd=self.path, environment=self.safe_environment
        ).stdout.strip()
        self.runner.run(
            ["git", "reset", "--mixed", "HEAD"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        if working_tree != final_tree:
            raise FixAgentError("target integration changed the validated batch tree")
        self.base_commit = target_commit

    def commit_parent(self, commit: str) -> str:
        return self.runner.run(
            ["git", "rev-parse", f"{commit}^"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.strip().lower()

    def is_ancestor(self, commit: str, descendant: str) -> bool:
        result = self.runner.run(
            ["git", "merge-base", "--is-ancestor", commit, descendant],
            cwd=self.path,
            environment=self.safe_environment,
            check=False,
        )
        return result.returncode == 0

    def commit(self, title: str) -> tuple[str, str]:
        digest = self.job.fingerprint.removeprefix("sha256:")
        if self.repository.publish_mode == "pull_request":
            branch = f"autofix/{self.repository.id}/{digest[:12]}"
        else:
            branch = self.repository.target_branch
        self.runner.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=self.path,
            environment=self.safe_environment,
        )
        message = self._commit_message(self.job, title)
        if self.repository.publish_mode == "pull_request":
            self.runner.run(
                ["git", "switch", "-c", branch],
                cwd=self.path,
                environment=self.safe_environment,
            )
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )
        self.runner.run(
            [
                "git",
                "-c",
                f"user.name={self.repository.git_author_name}",
                "-c",
                f"user.email={self.repository.git_author_email}",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                message,
            ],
            cwd=self.path,
            environment=self.safe_environment,
            timeout_seconds=self.repository.command_timeout_seconds,
        )
        commit = self.runner.run(
            ["git", "rev-parse", "HEAD"], cwd=self.path, environment=self.safe_environment
        ).stdout.strip()
        return branch, commit

    def _commit_message(self, job: Job, title: str) -> str:
        digest = job.fingerprint.removeprefix("sha256:")
        message = self.repository.commit_message_template.format(
            title=title,
            fingerprint=job.fingerprint,
            fingerprint_short=digest[:12],
            file=job.file,
        )
        _, separator, body = message.partition("\n")
        return title + (separator + body if separator else "")

    def _base_commit(self) -> str:
        if self.base_commit is None:
            raise FixAgentError("worktree base commit is not initialized")
        return self.base_commit

    def stage_for_harness(self) -> None:
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )

    def setup_signature(self, patterns: tuple[str, ...]) -> str:
        files = self.runner.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.split("\0")
        selected = tuple(
            sorted(
                file
                for file in files
                if file
                and any(
                    fnmatch.fnmatchcase(file, pattern)
                    or PurePosixPath(file).match(pattern)
                    for pattern in patterns
                )
            )
        )
        return self._files_signature(selected, patterns)

    def working_tree_fingerprint(self) -> str:
        digest = hashlib.sha256()
        diff = self.runner.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout
        digest.update(diff.encode("utf-8"))
        untracked = tuple(
            sorted(
                file
                for file in self.runner.run(
                    ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                    cwd=self.path,
                    environment=self.safe_environment,
                ).stdout.split("\0")
                if file
            )
        )
        digest.update(self._files_signature(untracked).encode("ascii"))
        return digest.hexdigest()

    def snapshot_working_changes(self) -> dict[str, _PathSnapshot]:
        return {
            file: self._path_snapshot(file) for file in self._changed_worktree_paths()
        }

    def restore_working_changes(
        self, snapshots: dict[str, _PathSnapshot]
    ) -> tuple[str, ...]:
        after_paths = set(self._changed_worktree_paths())
        before_paths = set(snapshots)
        tracked = set(
            file
            for file in self.runner.run(
                ["git", "ls-files", "-z"],
                cwd=self.path,
                environment=self.safe_environment,
            ).stdout.split("\0")
            if file
        )
        restored: list[str] = []
        restore_from_head: list[str] = []
        for file in sorted(before_paths | after_paths):
            expected = snapshots.get(file)
            current = self._path_snapshot(file)
            if expected is not None and current == expected:
                continue
            restored.append(file)
            if expected is None:
                if file in tracked:
                    restore_from_head.append(file)
                else:
                    self._remove_setup_file(file)
                continue
            self._restore_path_snapshot(file, expected)
        if restore_from_head:
            self.runner.run(
                [
                    "git",
                    "restore",
                    "--source=HEAD",
                    "--worktree",
                    "--",
                    *restore_from_head,
                ],
                cwd=self.path,
                environment=self.safe_environment,
            )
        self.runner.run(
            ["git", "reset", "--mixed", "HEAD"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        return tuple(restored)

    def _changed_worktree_paths(self) -> tuple[str, ...]:
        tracked = self.runner.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", "HEAD", "--"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.split("\0")
        untracked = self.runner.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.split("\0")
        return tuple(sorted({file for file in tracked + untracked if file}))

    def _path_snapshot(self, file: str) -> _PathSnapshot:
        candidate = self.path / file
        if candidate.is_symlink():
            return _PathSnapshot(True, link_target=os.readlink(candidate))
        if not candidate.exists():
            return _PathSnapshot(False)
        if not candidate.is_file():
            raise FixAgentError(
                f"setup guard does not support repository directory changes: {file}"
            )
        stat = candidate.stat()
        return _PathSnapshot(True, candidate.read_bytes(), mode=stat.st_mode & 0o777)

    def _restore_path_snapshot(self, file: str, snapshot: _PathSnapshot) -> None:
        candidate = self.path / file
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_dir() and not candidate.is_symlink():
                raise FixAgentError(
                    f"setup command replaced a file with a directory: {file}"
                )
            candidate.unlink()
        if not snapshot.exists:
            return
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.link_target is not None:
            candidate.symlink_to(snapshot.link_target)
            return
        candidate.write_bytes(snapshot.contents or b"")
        if snapshot.mode is not None:
            candidate.chmod(snapshot.mode)

    def _remove_setup_file(self, file: str) -> None:
        candidate = self.path / file
        if not (candidate.exists() or candidate.is_symlink()):
            return
        if candidate.is_dir() and not candidate.is_symlink():
            raise FixAgentError(
                f"setup command created an untracked directory entry: {file}"
            )
        candidate.unlink()

    def _files_signature(
        self, files: tuple[str, ...], patterns: tuple[str, ...] = ()
    ) -> str:
        digest = hashlib.sha256()
        for pattern in patterns:
            digest.update(b"pattern\0")
            digest.update(pattern.encode("utf-8"))
            digest.update(b"\0")
        for file in files:
            digest.update(b"file\0")
            digest.update(file.encode("utf-8"))
            digest.update(b"\0")
            candidate = self.path / file
            if candidate.is_symlink():
                digest.update(b"link\0")
                digest.update(os.readlink(candidate).encode("utf-8"))
                digest.update(b"\0")
                continue
            if not candidate.is_file():
                digest.update(b"missing\0")
                continue
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    def require_clean_checkout(self) -> None:
        status = self.runner.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout
        if status:
            raise FixAgentError("test harness changed the committed worktree")

    def head_commit(self) -> str:
        return self.runner.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.strip().lower()

    def preserve(self, reason: str) -> None:
        self._preserve_on_exit = True
        self.cleanup_complete = False
        self._record_event(
            "worktree_preserved",
            "worktree was preserved for publish retry",
            {
                "path": str(self.path),
                "scope": self.worktree_scope,
                "head_commit": self.head_commit(),
                "reason": reason[:4_000],
            },
        )

    def hold_publish_lock(self, lock: threading.RLock) -> None:
        if self._held_publish_lock is lock:
            return
        if self._held_publish_lock is not None:
            raise FixAgentError("worktree already holds a different publish lock")
        lock.acquire()
        self._held_publish_lock = lock

    def close(self) -> None:
        remove_returncode: int | None = None
        permission_error: str | None = None
        with _repository_lock(self.repository.local_path):
            if self._created:
                try:
                    self.ensure_writable()
                except FixAgentError as exc:
                    permission_error = str(exc)
                result = self.runner.run(
                    ["git", "worktree", "remove", "--force", str(self.path)],
                    cwd=self.repository.local_path,
                    environment=self.safe_environment,
                    check=False,
                )
                remove_returncode = result.returncode
                self._created = False
            prune_returncode: int | None = None
            if self.repository.local_path.is_dir():
                result = self.runner.run(
                    ["git", "worktree", "prune"],
                    cwd=self.repository.local_path,
                    environment=self.safe_environment,
                    check=False,
                )
                prune_returncode = result.returncode
        shutil.rmtree(self.root, ignore_errors=True)
        self.cleanup_complete = (
            not self.root.exists()
            and remove_returncode in {None, 0}
            and prune_returncode in {None, 0}
        )
        self._record_event(
            "worktree_removed"
            if self.cleanup_complete
            else "worktree_cleanup_incomplete",
            "worktree cleanup finished",
            {
                "path": str(self.path),
                "remove_returncode": remove_returncode,
                "prune_returncode": prune_returncode,
                "root_exists": self.root.exists(),
                "permission_error": permission_error,
            },
        )

    def _record_event(
        self, event_type: str, message: str, details: dict[str, object]
    ) -> None:
        try:
            with StateStore(self.state_dir) as state:
                state.record_event(self.job.id, event_type, message, details)
        except Exception as exc:
            if "job does not exist" not in str(exc):
                print(f"job {self.job.id} event log failed: {exc}")

    def __enter__(self) -> FixWorkspace:
        try:
            if self._resumable_worktree is None:
                self.create()
            else:
                self.resume()
        except BaseException:
            if self._resumable_worktree is None:
                self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        try:
            if not self._preserve_on_exit:
                self.close()
        finally:
            if self._held_publish_lock is not None:
                self._held_publish_lock.release()
                self._held_publish_lock = None


def reconcile_recorded_worktree(
    runner: CommandRunner,
    repository: RepositoryConfig,
    state_dir: Path,
    job_id: int,
    recorded_path: str,
) -> bool:
    worktree_root = (state_dir / "worktrees").resolve()
    path = Path(recorded_path).resolve()
    if (
        path.name != "checkout"
        or not path.is_relative_to(worktree_root)
        or not path.parent.name.startswith("fix-")
    ):
        raise FixAgentError("recorded worktree path is outside the managed root")
    with _repository_lock(repository.local_path):
        result = runner.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=repository.local_path,
            check=False,
        )
        shutil.rmtree(path.parent, ignore_errors=True)
        prune = runner.run(
            ["git", "worktree", "prune"],
            cwd=repository.local_path,
            check=False,
        )
        listed = runner.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repository.local_path,
            check=False,
        )
    registered = any(
        line == f"worktree {path}" for line in listed.stdout.splitlines()
    )
    complete = (
        not path.parent.exists()
        and prune.returncode == 0
        and listed.returncode == 0
        and not registered
    )
    with StateStore(state_dir) as state:
        state.record_event(
            job_id,
            "worktree_removed" if complete else "worktree_cleanup_incomplete",
            "recorded worktree cleanup was retried",
            {
                "path": str(path),
                "remove_returncode": result.returncode,
                "prune_returncode": prune.returncode,
                "list_returncode": listed.returncode,
                "root_exists": path.parent.exists(),
                "registered": registered,
                "reconciliation": True,
            },
        )
    return complete
