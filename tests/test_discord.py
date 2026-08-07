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

    def test_retryable_failure_uses_retry_event_instead_of_final_failure(self) -> None:
        retrying = job(next_attempt_at="2026-08-05T00:01:00+00:00")
        failed = event(event_type="status_changed", status="failed")
        self.assertEqual(discord_event_payloads(retrying, failed), ())

    def test_formats_validation_and_fix_milestones(self) -> None:
        expected = {
            "finding_validation_started": "🔎 코드 수정 finding 검증 시작",
            "finding_validation_completed": "✅ 코드 수정 finding 검증 완료",
            "fix_started": "🛠️ 코드 수정 시작",
            "fix_applied": "🛠️ 코드 수정안 생성 완료",
            "fix_iteration_failed": "🔄 코드 수정 보완 계속",
            "restart_recovery_scheduled": "🔄 코드 수정 재시작 복구",
            "retry_scheduled": "🔄 코드 수정 재시도 예정",
        }
        for event_type, title in expected.items():
            with self.subTest(event_type=event_type):
                payload = discord_event_payloads(
                    job(), replace(event(), event_type=event_type)
                )[0]
                self.assertEqual(payload["embeds"][0]["title"], title)


if __name__ == "__main__":
    unittest.main()
