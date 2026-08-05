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
        self.assertEqual(repository.target_branch, "main")
        self.assertEqual(repository.publish_mode, "direct")
        self.assertEqual(repository.max_remote_merge_attempts, 4)
        self.assertTrue(repository.discord.enabled)
        self.assertEqual(repository.discord.webhook_url_env, "FIX_DISCORD_WEBHOOK_URL")
        self.assertEqual(repository.discord.timeout_seconds, 20)
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

    def test_rejects_unknown_publish_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            path.write_text(
                self._config().replace(
                    'publish_mode = "direct"', 'publish_mode = "merge"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FixAgentError, "publish_mode"):
                load_config(path)

    def test_enabled_discord_requires_one_webhook_source(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"', ""
            )
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "webhook_url or webhook_url_env"):
                load_config(path)

    def test_discord_rejects_two_webhook_sources(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"',
                'webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"\n'
                'webhook_url = "https://discord.example/webhook"',
            )
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "must not set both"):
                load_config(path)

    def test_discord_rejects_webhook_without_host(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"',
                'webhook_url = "https://"',
            )
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "must be an HTTP URL"):
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
target_branch = "main"
local_path = "repo"
publish_mode = "direct"
github_token_env = "FIX_GITHUB_TOKEN"
test_commands = [["python3", "-m", "unittest"]]
additional_instructions = "Preserve the public API."
[repositories.discord]
enabled = true
webhook_url_env = "FIX_DISCORD_WEBHOOK_URL"
timeout_seconds = 20
[repositories.execution]
max_remote_merge_attempts = 4
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
