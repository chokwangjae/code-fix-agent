from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fix_agent.cli import main
from fix_agent.config import load_config
from fix_agent.contract import parse_review_event
from fix_agent.state import StateStore
from test_contract import event
from test_state import StateTest


class CliTest(unittest.TestCase):
    def test_events_json_uses_global_cursor_and_structured_details(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(StateTest._config(), encoding="utf-8")
            config = load_config(config_path)
            with StateStore(config.state_dir) as state:
                accepted = state.accept(
                    config.repositories[0], parse_review_event(event())
                )
                state.record_event(
                    accepted.job_ids[0],
                    "discord_candidate",
                    "ready to notify",
                    {"n": 1},
                )
                first_id = state.events()[0].id

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "events",
                        "--config",
                        str(config_path),
                        "--after-id",
                        str(first_id),
                        "--json",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["event_type"], "discord_candidate")
        self.assertEqual(payload[0]["details"], {"n": 1})
        self.assertNotIn("details_json", payload[0])

    def test_notify_once_reports_no_delivery_when_discord_is_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(StateTest._config(), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(["notify-once", "--config", str(config_path)])
        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"delivered": 0, "skipped": 0, "failed": 0, "deferred": 0},
        )

    def test_crontrol_once_is_noop_when_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(StateTest._config(), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(["crontrol-once", "--config", str(config_path)])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "Crontrol status unchanged")

    def test_crontrol_stage_requires_job_id(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "fix.toml"
            config_path.write_text(StateTest._config(), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "crontrol-once",
                        "--config",
                        str(config_path),
                        "--stage",
                        "수정 중",
                    ]
                )
        self.assertEqual(result, 1)
        self.assertIn("--stage requires --job-id", output.getvalue())


if __name__ == "__main__":
    unittest.main()
