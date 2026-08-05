from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import threading

from .codex_agent import CodexAgent
from .command import CommandRunner
from .config import AppConfig, RepositoryConfig
from .errors import FixAgentError
from .state import Job, StateStore
from .workspace import FixWorkspace


class FixWorker:
    def __init__(
        self,
        config: AppConfig,
        runner: CommandRunner | None = None,
        agent: CodexAgent | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.agent = agent or CodexAgent(self.runner, config.codex_executable)
        self._stop = threading.Event()

    def run_once(self) -> bool:
        with StateStore(self.config.state_dir) as state:
            job = state.claim_next(self.config.repositories)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            with StateStore(self.config.state_dir) as state:
                state.mark_failed(job.id, str(exc))
            print(f"job {job.id} failed: {exc}")
        return True

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _process(self, job: Job) -> None:
        repository = self.config.repository_by_id(job.repository_id)
        if job.fix_branch and job.result_commit:
            pr_url = self._publish_pull_request(repository, job, job.fix_branch)
            with StateStore(self.config.state_dir) as state:
                state.mark_completed(job.id, pr_url)
            return

        with FixWorkspace(
            self.runner, repository, job, self.config.state_dir
        ) as workspace:
            environment = workspace.safe_environment
            mismatch = workspace.finding_mismatch_reason()
            if mismatch:
                with StateStore(self.config.state_dir) as state:
                    state.record_precheck(job.id, False, mismatch)
                print(f"job {job.id} rejected: {mismatch}")
                return
            decision = self.agent.validate_finding(
                repository, job, workspace.path, environment
            )
            with StateStore(self.config.state_dir) as state:
                state.record_precheck(job.id, decision.valid, decision.reason)
            if not decision.valid:
                print(f"job {job.id} rejected: {decision.reason}")
                return

            self.agent.apply_fix(repository, job, workspace.path, environment)
            summary = workspace.validate_diff()
            tests = self._run_tests(repository, workspace.path, environment)
            with StateStore(self.config.state_dir) as state:
                state.record_tests(job.id, tests)
            failed = [test for test in tests if test["returncode"] != 0]
            if failed:
                raise FixAgentError("configured test command failed")
            postcheck = self.agent.validate_fix(
                repository, job, workspace.path, environment
            )
            with StateStore(self.config.state_dir) as state:
                state.record_postcheck(job.id, postcheck.valid, postcheck.reason)
            if not postcheck.valid:
                print(f"job {job.id} fix rejected: {postcheck.reason}")
                return
            workspace.fetch_and_require_fresh()
            workspace.validate_diff()
            branch, commit = workspace.commit()
            self._push(repository, workspace.path, environment, branch)
            with StateStore(self.config.state_dir) as state:
                state.mark_pushed(job.id, branch, commit)
            job = Job(
                **{
                    **job.__dict__,
                    "fix_branch": branch,
                    "result_commit": commit,
                    "precheck_status": "valid",
                    "precheck_reason": decision.reason,
                    "postcheck_status": "resolved",
                    "postcheck_reason": postcheck.reason,
                    "tests_json": json.dumps(tests, ensure_ascii=False),
                }
            )
            print(
                f"job {job.id} changed {len(summary.files)} file(s), "
                f"{summary.added_lines + summary.deleted_lines} line(s)"
            )
        pr_url = self._publish_pull_request(repository, job, branch)
        with StateStore(self.config.state_dir) as state:
            state.mark_completed(job.id, pr_url)
        print(f"job {job.id} completed: {pr_url}")

    def _run_tests(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
    ) -> list[dict[str, object]]:
        results = []
        for command in repository.test_commands:
            result = self.runner.run(
                command,
                cwd=workspace,
                environment=environment,
                timeout_seconds=repository.command_timeout_seconds,
                check=False,
            )
            results.append(
                {
                    "command": list(command),
                    "returncode": result.returncode,
                    "stdout": result.stdout[-20_000:],
                    "stderr": result.stderr[-20_000:],
                }
            )
        return results

    def _push(
        self,
        repository: RepositoryConfig,
        workspace: Path,
        environment: dict[str, str],
        branch: str,
    ) -> None:
        token = os.environ.get(repository.github_token_env)
        if not token:
            raise FixAgentError(
                f"required environment variable is not set: {repository.github_token_env}"
            )
        push_environment = dict(environment)
        expected_urls = {
            f"https://github.com/{repository.github}",
            f"https://github.com/{repository.github}.git",
        }
        remote_url = self.runner.run(
            ["git", "remote", "get-url", repository.remote],
            cwd=workspace,
            environment=environment,
        ).stdout.strip()
        if remote_url not in expected_urls:
            raise FixAgentError(
                f"Git remote does not match configured GitHub repository: {remote_url}"
            )
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        push_environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
        self.runner.run(
            [
                "git",
                "push",
                "--set-upstream",
                repository.remote,
                f"HEAD:refs/heads/{branch}",
            ],
            cwd=workspace,
            environment=push_environment,
            timeout_seconds=repository.command_timeout_seconds,
        )

    def _publish_pull_request(
        self, repository: RepositoryConfig, job: Job, branch: str
    ) -> str:
        token = os.environ.get(repository.github_token_env)
        if not token:
            raise FixAgentError(
                f"required environment variable is not set: {repository.github_token_env}"
            )
        environment = os.environ.copy()
        environment["GH_TOKEN"] = token
        existing = self.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository.github,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url",
                "--limit",
                "1",
            ],
            environment=environment,
            timeout_seconds=repository.command_timeout_seconds,
        )
        try:
            matches = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise FixAgentError("gh pr list returned invalid JSON") from exc
        if matches:
            return matches[0]["url"]
        title = f"fix: resolve review finding {job.fingerprint[-12:]}"
        body = (
            "## Review finding\n\n"
            f"- Fingerprint: `{job.fingerprint}`\n"
            f"- Reviewed target: `{job.target_commit}`\n"
            f"- Location: `{job.file}:{job.line}`\n"
            f"- Independent validation: {job.precheck_reason}\n"
            f"- Fix validation: {job.postcheck_reason}\n"
        )
        created = self.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repository.github,
                "--base",
                repository.branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            environment=environment,
            timeout_seconds=repository.command_timeout_seconds,
        )
        url = created.stdout.strip()
        if not url:
            raise FixAgentError("gh pr create returned no URL")
        return url
