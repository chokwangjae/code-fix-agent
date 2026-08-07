from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.codex_agent import CodexAgent
from fix_agent.command import CommandResult
from fix_agent.config import load_config
from fix_agent.errors import FixAgentError
from fakes import job


class FakeRunner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return CommandResult(self.output, "", 0)


class CodexAgentTest(unittest.TestCase):
    def test_records_specific_independent_validation_reason(self) -> None:
        runner = FakeRunner('{"valid":true,"reason":"Caller drops exit 1."}')
        with TemporaryDirectory() as directory:
            repository = self._repository(Path(directory))
            decision = CodexAgent(runner, "/usr/bin/codex").validate_finding(
                repository, job(), Path(directory), {"PATH": "/usr/bin"}
            )
        self.assertTrue(decision.valid)
        self.assertEqual(decision.reason, "Caller drops exit 1.")
        command, options = runner.calls[0]
        self.assertIn("read-only", command)
        self.assertIn("applicable AGENTS.md", options["input_text"])

    def test_fix_uses_workspace_write_and_additional_rules(self) -> None:
        runner = FakeRunner("")
        with TemporaryDirectory() as directory:
            repository = self._repository(Path(directory))
            CodexAgent(runner, "/usr/bin/codex").apply_fix(
                repository, job(), Path(directory), {"PATH": "/usr/bin"}
            )
        command, options = runner.calls[0]
        self.assertIn("workspace-write", command)
        self.assertIn("Preserve the API.", options["input_text"])

    def test_fix_validation_returns_diff_based_commit_title(self) -> None:
        runner = FakeRunner(
            '{"resolved":true,"reason":"Disabled references now block deletion.",'
            '"commit_title":"fix(schedule): 비활성 스케줄의 실행 셋 삭제 차단"}'
        )
        with TemporaryDirectory() as directory:
            repository = self._repository(Path(directory))
            decision = CodexAgent(runner, "/usr/bin/codex").validate_fix(
                repository, job(), Path(directory), {"PATH": "/usr/bin"}
            )

        self.assertTrue(decision.valid)
        self.assertEqual(
            decision.commit_title,
            "fix(schedule): 비활성 스케줄의 실행 셋 삭제 차단",
        )
        prompt = runner.calls[0][1]["input_text"]
        self.assertIn("actual change", prompt)
        self.assertIn("target repository's", prompt)

    def test_fix_validation_rejects_generic_commit_title(self) -> None:
        runner = FakeRunner(
            '{"resolved":true,"reason":"The failure is fixed.",'
            '"commit_title":"fix(autofix): 리뷰 이슈 500d70119f90 수정"}'
        )
        with TemporaryDirectory() as directory:
            repository = self._repository(Path(directory))
            with self.assertRaisesRegex(FixAgentError, "generic review label"):
                CodexAgent(runner, "/usr/bin/codex").validate_fix(
                    repository, job(), Path(directory), {"PATH": "/usr/bin"}
                )

    def test_retry_fix_receives_previous_failure_and_harness_output(self) -> None:
        runner = FakeRunner("")
        previous_tests = (
            '[{"command":["bash","check.sh"],"returncode":1,'
            '"stdout":"artifact is stale","stderr":""}]'
        )
        with TemporaryDirectory() as directory:
            repository = self._repository(Path(directory))
            CodexAgent(runner, "/usr/bin/codex").apply_fix(
                repository,
                job(
                    attempts=2,
                    last_error="configured test command failed",
                    tests_json=previous_tests,
                ),
                Path(directory),
                {"PATH": "/usr/bin"},
            )
        prompt = runner.calls[0][1]["input_text"]
        self.assertIn("retry attempt 2", prompt)
        self.assertIn("configured test command failed", prompt)
        self.assertIn("bash check.sh (exit 1): artifact is stale", prompt)
        self.assertIn("Do not hide, skip, or weaken repository checks", prompt)

    @staticmethod
    def _repository(root: Path):
        path = root / "fix.toml"
        path.write_text(
            """
version = 1
state_dir = ".state"
[server]
token_env = "FIX_TOKEN"
[[repositories]]
id = "repo"
github = "owner/repo"
branch = "main"
local_path = "repo"
github_token_env = "FIX_GITHUB_TOKEN"
test_commands = []
additional_instructions = "Preserve the API."
""",
            encoding="utf-8",
        )
        return load_config(path).repositories[0]


if __name__ == "__main__":
    unittest.main()
