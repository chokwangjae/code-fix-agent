from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.config import load_config
from fix_agent.errors import FixAgentError
from fix_agent.server import IntakeApplication
from test_contract import event


class IntakeApplicationTest(unittest.TestCase):
    def test_accepts_configured_repository(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            result = IntakeApplication(load_config(config_path)).submit(event())
        self.assertEqual(result["created"], 1)

    def test_rejects_unconfigured_repository(self) -> None:
        value = event()
        value["repository"] = "other/repo"
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(self._config(), encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "not configured"):
                IntakeApplication(load_config(config_path)).submit(value)

    @staticmethod
    def _config() -> str:
        return """
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
"""


if __name__ == "__main__":
    unittest.main()
