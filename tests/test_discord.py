from dataclasses import replace
import json
import unittest

from fix_agent.discord import FAIL_COLOR, PASS_COLOR, discord_event_payloads
from fix_agent.state import JobEvent
from fakes import job


def event(**overrides: object) -> JobEvent:
    values = {
        "id": 17,
        "job_id": 1,
        "event_type": "push_completed",
        "status": "pushed",
        "message": "Pushed @everyone after @here validation.",
        "details_json": json.dumps({"commit": "a" * 40}),
        "created_at": "2026-08-05T00:00:00+00:00",
    }
    values.update(overrides)
    return JobEvent(**values)


class DiscordPayloadTest(unittest.TestCase):
    def test_formats_event_with_disabled_mentions_and_cursor(self) -> None:
        payload = discord_event_payloads(job(), event())[0]
        embed = payload["embeds"][0]

        self.assertEqual(payload["username"], "Code Fix Agent")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(embed["color"], PASS_COLOR)
        self.assertEqual(embed["fields"][1]["value"], "17")
        self.assertNotIn("@everyone", embed["fields"][4]["value"])
        self.assertNotIn("@here", embed["fields"][4]["value"])

    def test_formats_failed_status_and_limits_details(self) -> None:
        failed = event(
            event_type="status_changed",
            status="failed",
            details_json=json.dumps({"last_error": "x" * 5000}),
        )
        payload = discord_event_payloads(job(), failed)[0]
        embed = payload["embeds"][0]
        characters = len(embed["title"]) + len(embed["description"]) + sum(
            len(field["name"]) + len(field["value"])
            for field in embed["fields"]
        )

        self.assertEqual(embed["color"], FAIL_COLOR)
        self.assertLessEqual(characters, 5500)

    def test_skips_internal_progress_event(self) -> None:
        progress = replace(event(), event_type="diff_validated", status="fixing")
        self.assertEqual(discord_event_payloads(job(), progress), ())


if __name__ == "__main__":
    unittest.main()
