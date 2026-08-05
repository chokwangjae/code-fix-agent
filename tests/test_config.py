from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.config import load_config
from fix_agent.errors import FixAgentError


class ConfigTest(unittest.TestCase):
    def test_loads_repository_rules_and_exclusions(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            path.write_text(self._config(), encoding="utf-8")
            config = load_config(path)
        repository = config.repositories[0]
        self.assertEqual(config.server.port, 7081)
        self.assertEqual(repository.additional_instructions, "Preserve the public API.")
        self.assertEqual(repository.policy.max_changed_files, 3)
        self.assertEqual(repository.policy.skip_reason("Critical", "src/a.py", "sha256:" + "a" * 64), "severity is not enabled: Critical")
        self.assertIsNotNone(repository.policy.skip_reason("Major", ".github/workflows/a.yml", "sha256:" + "a" * 64))

    def test_rejects_parent_path_policy(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            path.write_text(self._config().replace('["src/**"]', '["../src/**"]'), encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "repository-relative"):
                load_config(path)

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
test_commands = [["python3", "-m", "unittest"]]
additional_instructions = "Preserve the public API."
[repositories.policy]
allowed_severities = ["Major", "Minor"]
allowed_paths = ["src/**"]
denied_paths = [".github/workflows/**"]
skipped_paths = []
skipped_fingerprints = []
max_changed_files = 3
max_changed_lines = 100
allow_new_files = false
allow_deletions = false
"""


if __name__ == "__main__":
    unittest.main()
