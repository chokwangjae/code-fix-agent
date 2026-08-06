from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fix_agent.config import load_config
from fix_agent.errors import FixAgentError
from fix_agent.launchd import launchd_environment, launchd_payload


class LaunchdTest(unittest.TestCase):
    def test_payload_runs_one_persistent_server(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            with patch.dict(
                "os.environ",
                {
                    "FIX_TOKEN": "intake",
                    "FIX_GITHUB_TOKEN": "github",
                    "FIX_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                    "CRONTROL_API_TOKEN": "crontrol",
                },
                clear=True,
            ):
                environment = launchd_environment(config)
            payload = launchd_payload(
                config,
                root / "fix.toml",
                root / "bin/fix-agent",
                root / "logs",
                environment,
            )
        self.assertEqual(payload["ProgramArguments"][1], "serve")
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(environment["FIX_GITHUB_TOKEN"], "github")
        self.assertEqual(
            environment["FIX_DISCORD_WEBHOOK_URL"],
            "https://discord.example/webhook",
        )
        self.assertEqual(environment["CRONTROL_API_TOKEN"], "crontrol")

    def test_requires_all_configured_tokens(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(
                    FixAgentError,
                    "CRONTROL_API_TOKEN|FIX_DISCORD_WEBHOOK_URL|FIX_GITHUB_TOKEN|FIX_TOKEN",
                ):
                    launchd_environment(config)

    def test_direct_tokens_do_not_require_environment_variables(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fix.toml"
            value = (
                self._config_text()
                .replace('token_env = "FIX_TOKEN"', 'token = "server-direct"')
                .replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token = "github-direct"',
                )
                .replace("enabled = true", "enabled = false")
            )
            path.write_text(value, encoding="utf-8")
            config = load_config(path)
            with patch.dict("os.environ", {}, clear=True):
                environment = launchd_environment(config)
        self.assertEqual(set(environment), {"PATH"})

    def test_missing_github_environment_falls_back_without_blocking_install(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            values = {
                "FIX_TOKEN": "intake",
                "FIX_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                "CRONTROL_API_TOKEN": "crontrol",
            }
            with patch.dict("os.environ", values, clear=True):
                environment = launchd_environment(config)
        self.assertNotIn("FIX_GITHUB_TOKEN", environment)

    @staticmethod
    def _config(root: Path):
        path = root / "fix.toml"
        path.write_text(LaunchdTest._config_text(), encoding="utf-8")
        return load_config(path)

    @staticmethod
    def _config_text() -> str:
        return """
version = 1
state_dir = ".state"
[server]
token_env = "FIX_TOKEN"
[crontrol]
enabled = true
token_env = "CRONTROL_API_TOKEN"
[[repositories]]
id = "repo"
github = "owner/repo"
branch = "main"
local_path = "repo"
github_token_env = "FIX_GITHUB_TOKEN"
test_commands = []
[repositories.discord]
enabled = true
webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"
"""


if __name__ == "__main__":
    unittest.main()
