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
        self.assertFalse(config.crontrol.enabled)
        self.assertEqual(repository.target_branch, "main")
        self.assertEqual(repository.publish_mode, "direct")
        self.assertEqual(repository.max_attempts, 0)
        self.assertEqual(repository.retry_delay_seconds, 15)
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

    def test_rejects_negative_retry_settings(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            path.write_text(
                self._config().replace("max_attempts = 0", "max_attempts = -1"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FixAgentError, "non-negative"):
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

    def test_loads_direct_server_and_github_tokens_without_repr_leak(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = (
                self._config()
                .replace(
                    'token_env = "FIX_TOKEN"',
                    'token = "server-secret-value"',
                )
                .replace(
                    'github_token_env = "FIX_GITHUB_TOKEN"',
                    'github_token = "github-secret-value"',
                )
            )
            path.write_text(value, encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.server.token, "server-secret-value")
        self.assertIsNone(config.server.token_env)
        self.assertEqual(
            config.repositories[0].github_token, "github-secret-value"
        )
        self.assertIsNone(config.repositories[0].github_token_env)
        self.assertNotIn("server-secret-value", repr(config))
        self.assertNotIn("github-secret-value", repr(config))

    def test_rejects_two_server_token_sources(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'token_env = "FIX_TOKEN"',
                'token_env = "FIX_TOKEN"\ntoken = "direct-value"',
            )
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "must not set both"):
                load_config(path)

    def test_rejects_missing_server_token_source(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace('token_env = "FIX_TOKEN"', "")
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "token or token_env"):
                load_config(path)

    def test_loads_crontrol_status_sync(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                "[server]\ntoken_env = \"FIX_TOKEN\"",
                "[server]\ntoken_env = \"FIX_TOKEN\"\n"
                "[crontrol]\n"
                "enabled = true\n"
                "base_url = \"http://127.0.0.1:7070\"\n"
                "job_id = \"code-fix-agent-server\"\n"
                "name = \"Code Fix Agent\"\n"
                "branch = \"main\"\n"
                "token = \"crontrol-secret\"\n"
                "timeout_seconds = 7",
            )
            path.write_text(value, encoding="utf-8")
            config = load_config(path)
        self.assertTrue(config.crontrol.enabled)
        self.assertEqual(config.crontrol.name, "Code Fix Agent")
        self.assertEqual(config.crontrol.token, "crontrol-secret")
        self.assertIsNone(config.crontrol.token_env)
        self.assertEqual(config.crontrol.timeout_seconds, 7)
        self.assertNotIn("crontrol-secret", repr(config))

    def test_rejects_two_github_token_sources(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'github_token_env = "FIX_GITHUB_TOKEN"',
                'github_token_env = "FIX_GITHUB_TOKEN"\n'
                'github_token = "direct-value"',
            )
            path.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(FixAgentError, "must not set both"):
                load_config(path)

    def test_allows_repository_to_fall_back_to_gh_auth(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fix.toml"
            value = self._config().replace(
                'github_token_env = "FIX_GITHUB_TOKEN"', ""
            )
            path.write_text(value, encoding="utf-8")
            config = load_config(path)
        self.assertIsNone(config.repositories[0].github_token)
        self.assertIsNone(config.repositories[0].github_token_env)

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
max_attempts = 0
retry_delay_seconds = 15
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
