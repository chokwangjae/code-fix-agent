from __future__ import annotations

from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.notify import DiscordNotifier
from fix_agent.state import StateStore
from test_contract import event


class FakeResponse(AbstractContextManager):
    def __init__(self, status: int = 204) -> None:
        self.status = status

    def __exit__(self, *args: object) -> None:
        return None


class RecordingOpener:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, int]] = []

    def __call__(self, request, *, timeout: int):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse()


class DiscordNotifierTest(unittest.TestCase):
    def test_first_activation_skips_existing_history(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                state.record_event(
                    accepted.job_ids[0], "push_completed", "old push", {}
                )
            opener = RecordingOpener()
            notifier = DiscordNotifier(config, opener=opener)
            with patch.dict(
                os.environ,
                {"FIX_DISCORD_WEBHOOK_URL": "https://discord.example/webhook"},
                clear=True,
            ):
                result = notifier.dispatch_pending()

        self.assertEqual(result.delivered, 0)
        self.assertEqual(opener.calls, [])

    def test_sends_candidate_once_and_advances_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            opener = RecordingOpener()
            notifier = DiscordNotifier(config, opener=opener)
            notifier.initialize_cursors()
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                state.record_event(
                    accepted.job_ids[0],
                    "push_completed",
                    "push complete @everyone",
                    {"commit": "abc"},
                )
                candidate_id = state.events(accepted.job_ids[0])[-1].id
            with patch.dict(
                os.environ,
                {"FIX_DISCORD_WEBHOOK_URL": "https://discord.example/webhook"},
                clear=True,
            ):
                first = notifier.dispatch_pending()
                second = notifier.dispatch_pending()
            with StateStore(config.state_dir) as state:
                cursor = state.discord_cursor("repo")

        self.assertEqual(first.delivered, 1)
        self.assertGreaterEqual(first.skipped, 1)
        self.assertEqual(second.delivered, 0)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://discord.example/webhook")
        self.assertEqual(timeout, 20)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("＠everyone", payload["embeds"][0]["fields"][4]["value"])
        self.assertEqual(cursor.last_event_id, candidate_id)
        self.assertIsNone(cursor.failed_event_id)

    def test_failure_keeps_cursor_and_force_retries_same_event(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            failing = RecordingOpener(error=URLError("offline"))
            notifier = DiscordNotifier(config, opener=failing)
            notifier.initialize_cursors()
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                state.record_event(
                    accepted.job_ids[0], "push_completed", "push complete", {}
                )
                candidate_id = state.events(accepted.job_ids[0])[-1].id
            environment = {
                "FIX_DISCORD_WEBHOOK_URL": "https://discord.example/webhook"
            }
            with patch.dict(os.environ, environment, clear=True):
                failed = notifier.dispatch_pending()
                deferred = notifier.dispatch_pending()
                successful = RecordingOpener()
                retried = DiscordNotifier(config, opener=successful).dispatch_pending(
                    force=True
                )
            with StateStore(config.state_dir) as state:
                cursor = state.discord_cursor("repo")

        self.assertEqual(failed.failed, 1)
        self.assertEqual(deferred.deferred, 1)
        self.assertEqual(retried.delivered, 1)
        self.assertEqual(len(successful.calls), 1)
        self.assertEqual(cursor.last_event_id, candidate_id)
        self.assertEqual(cursor.attempts, 0)
        self.assertIsNone(cursor.last_error)

    def test_missing_webhook_environment_is_recorded_without_secret(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            notifier = DiscordNotifier(config, opener=RecordingOpener())
            notifier.initialize_cursors()
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                state.record_event(
                    accepted.job_ids[0], "push_completed", "push complete", {}
                )
                initial_status = state.jobs()[0].status
            with patch.dict(os.environ, {}, clear=True):
                result = notifier.dispatch_pending()
            with StateStore(config.state_dir) as state:
                cursor = state.discord_cursor("repo")
                final_status = state.jobs()[0].status

        self.assertEqual(result.failed, 1)
        self.assertEqual(initial_status, final_status)
        self.assertIn("FIX_DISCORD_WEBHOOK_URL", cursor.last_error)
        self.assertNotIn("discord.example", cursor.last_error)

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
timeout_seconds = 20
""",
            encoding="utf-8",
        )
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
