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

    def test_requires_all_configured_tokens(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(
                    FixAgentError,
                    "FIX_DISCORD_WEBHOOK_URL|FIX_GITHUB_TOKEN|FIX_TOKEN",
                ):
                    launchd_environment(config)

    @staticmethod
    def _config(root: Path):
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
[repositories.discord]
enabled = true
webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"
""",
            encoding="utf-8",
        )
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
