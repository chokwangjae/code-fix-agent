from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
import re

from .command import CommandRunner
from .config import RepositoryConfig
from .errors import FixAgentError
from .state import Job


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
        worktrees = state_dir / "worktrees"
        worktrees.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="fix-", dir=worktrees))
        self.path = self.root / "checkout"
        self._created = False

    @property
    def safe_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        credential_names = {
            name
            for name in environment
            if name.endswith(("_TOKEN", "_SECRET", "_PASSWORD"))
            or "WEBHOOK" in name
        }
        for name in _CREDENTIAL_ENVIRONMENT | credential_names | {self.repository.github_token_env}:
            environment.pop(name, None)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    def create(self) -> None:
        if not self.repository.local_path.is_dir():
            raise FixAgentError(
                f"local repository does not exist: {self.repository.local_path}"
            )
        self.fetch_and_require_fresh()
        self.runner.run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(self.path),
                self.job.target_commit,
            ],
            cwd=self.repository.local_path,
            environment=self.safe_environment,
            timeout_seconds=self.repository.command_timeout_seconds,
        )
        self._created = True

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

    def fetch_and_require_fresh(self) -> None:
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
            environment=self.safe_environment,
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
        head = result.stdout.strip().lower()
        if head != self.job.target_commit:
            raise FixAgentError(
                f"target branch moved: reviewed {self.job.target_commit}, current {head}"
            )

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
                self.job.target_commit,
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
            if candidate.exists() and not candidate.resolve().is_relative_to(self.path.resolve()):
                raise FixAgentError(f"changed path escapes the worktree: {file}")
        added = deleted = 0
        numstat = self.runner.run(
            ["git", "diff", "--numstat", self.job.target_commit, "--"],
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
            ["git", "diff", "--check", self.job.target_commit, "--"],
            cwd=self.path,
            environment=self.safe_environment,
        )
        return DiffSummary(files, added, deleted)

    def commit(self) -> tuple[str, str]:
        digest = self.job.fingerprint.removeprefix("sha256:")
        branch = f"autofix/{self.repository.id}/{digest[:12]}"
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

    def stage_for_harness(self) -> None:
        self.runner.run(
            ["git", "add", "--all"], cwd=self.path, environment=self.safe_environment
        )

    def close(self) -> None:
        if self._created:
            self.runner.run(
                ["git", "worktree", "remove", "--force", str(self.path)],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                check=False,
            )
            self._created = False
        shutil.rmtree(self.root, ignore_errors=True)
        if self.repository.local_path.is_dir():
            self.runner.run(
                ["git", "worktree", "prune"],
                cwd=self.repository.local_path,
                environment=self.safe_environment,
                check=False,
            )

    def __enter__(self) -> FixWorkspace:
        try:
            self.create()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
