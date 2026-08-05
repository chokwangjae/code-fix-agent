import os
import unittest
from unittest.mock import patch

from fix_agent.command import CommandResult
from fix_agent.credentials import resolve_credential, resolve_github_credential
from fix_agent.errors import FixAgentError


class CredentialsTest(unittest.TestCase):
    def test_prefers_direct_credential(self) -> None:
        with patch.dict(os.environ, {"TOKEN_NAME": "environment"}, clear=True):
            value = resolve_credential(
                "direct", "TOKEN_NAME", "test token"
            )
        self.assertEqual(value, "direct")

    def test_reads_environment_credential(self) -> None:
        with patch.dict(os.environ, {"TOKEN_NAME": "environment"}, clear=True):
            value = resolve_credential(None, "TOKEN_NAME", "test token")
        self.assertEqual(value, "environment")

    def test_missing_required_credential_names_source_without_secret(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(FixAgentError, "TOKEN_NAME"):
                resolve_credential(None, "TOKEN_NAME", "test token")

    def test_github_credential_falls_back_to_pc_gh_auth(self) -> None:
        runner = FakeRunner(CommandResult("gh-secret\n", "", 0))
        with patch.dict(
            os.environ,
            {"CODE_FIX_TOKEN": "intake-secret", "GH_CONFIG_DIR": "/gh-config"},
            clear=True,
        ):
            value = resolve_github_credential(None, None, runner)
        self.assertEqual(value, "gh-secret")
        command, options = runner.calls[0]
        self.assertEqual(
            command, ["gh", "auth", "token", "--hostname", "github.com"]
        )
        self.assertEqual(options["environment"]["GH_CONFIG_DIR"], "/gh-config")
        self.assertNotIn("CODE_FIX_TOKEN", options["environment"])

    def test_github_environment_token_avoids_gh_lookup(self) -> None:
        runner = FakeRunner(CommandResult("unused", "", 0))
        with patch.dict(os.environ, {"REPO_TOKEN": "configured"}, clear=True):
            value = resolve_github_credential(None, "REPO_TOKEN", runner)
        self.assertEqual(value, "configured")
        self.assertEqual(runner.calls, [])

    def test_github_auth_failure_has_actionable_error(self) -> None:
        runner = FakeRunner(CommandResult("", "not logged in", 1))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(FixAgentError, "gh auth login"):
                resolve_github_credential(None, None, runner)


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return self.result


if __name__ == "__main__":
    unittest.main()
