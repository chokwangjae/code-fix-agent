from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
import re

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


class FixWorkspace:
    def __init__(
        self,
        runner: CommandRunner,
        repository: RepositoryConfig,
        job: Job,
        state_dir: Path,
    ) -> None:
        self.runner = runner
        self.repository = repository
        self.job = job
        self.state_dir = state_dir
        worktrees = state_dir / "worktrees"
        worktrees.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="fix-", dir=worktrees))
        self.path = self.root / "checkout"
        self._created = False
        self.base_commit: str | None = None
        self.cleanup_complete: bool | None = None
        self._github_token_loaded = False
        self._github_token: str | None = None

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
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    def create(self) -> None:
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
        self._record_event(
            "worktree_created",
            "detached worktree created from the latest target branch",
            {
                "path": str(self.path),
                "remote": self.repository.remote,
                "target_branch": self.repository.target_branch,
                "base_commit": self.base_commit,
            },
        )

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

    def finding_mismatch_reason(self) -> str | None:
        ancestry = self.runner.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                self.job.introducing_commit,
                self.job.target_commit,
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
                self.job.introducing_commit,
                "--",
                self.job.file,
            ],
            cwd=self.path,
            environment=self.safe_environment,
        ).stdout.splitlines()
        if self.job.file not in commit_files:
            return "introducing commit does not change the finding file"
        diff = self.runner.run(
            [
                "git",
                "diff",
                "--unified=0",
                "--no-color",
                self.job.baseline_commit,
                self.job.target_commit,
                "--",
                self.job.file,
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
        if self.job.line not in changed_lines:
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
                "user.name=Code Fix Agent",
                "-c",
                "user.email=code-fix-agent@users.noreply.github.com",
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
                "user.name=Code Fix Agent",
                "-c",
                "user.email=code-fix-agent@users.noreply.github.com",
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

    def validate_diff(self) -> DiffSummary:
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
        if len(changes) > policy.max_changed_files:
            raise FixAgentError(
                f"fix changed {len(changes)} files; limit is {policy.max_changed_files}"
            )
        files = tuple(file for _, file in changes)
        if policy.require_finding_file_changed and self.job.file not in files:
            raise FixAgentError("fix did not change the finding file")
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
        if added + deleted > policy.max_changed_lines:
            raise FixAgentError(
                f"fix changed {added + deleted} lines; limit is {policy.max_changed_lines}"
            )
        self.runner.run(
            ["git", "diff", "--check", self._base_commit(), "--"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        return DiffSummary(files, added, deleted)

    def commit(self) -> tuple[str, str]:
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
        message = self.repository.commit_message_template.format(
            fingerprint=self.job.fingerprint,
            fingerprint_short=digest[:12],
            file=self.job.file,
        )
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
                "user.name=Code Fix Agent",
                "-c",
                "user.email=code-fix-agent@users.noreply.github.com",
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

    def _base_commit(self) -> str:
        if self.base_commit is None:
            raise FixAgentError("worktree base commit is not initialized")
        return self.base_commit

    def stage_for_harness(self) -> None:
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )

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

    def close(self) -> None:
        remove_returncode: int | None = None
        if self._created:
            result = self.runner.run(
                ["git", "worktree", "remove", "--force", str(self.path)],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                check=False,
            )
            remove_returncode = result.returncode
            self._created = False
        shutil.rmtree(self.root, ignore_errors=True)
        prune_returncode: int | None = None
        if self.repository.local_path.is_dir():
            result = self.runner.run(
                ["git", "worktree", "prune"],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                check=False,
            )
            prune_returncode = result.returncode
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
            self.create()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
