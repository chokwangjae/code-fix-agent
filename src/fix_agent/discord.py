from __future__ import annotations

import json
from typing import Any

from .state import Job, JobEvent


PASS_COLOR = 0x2ECC71
FAIL_COLOR = 0xE74C3C
INFO_COLOR = 0x3498DB
WARNING_COLOR = 0xE67E22

_NOTIFIABLE_EVENTS = {
    "finding_validation_started",
    "finding_validation_completed",
    "fix_started",
    "fix_applied",
    "target_moved",
    "merge_conflict_detected",
    "merge_conflict_resolved",
    "push_completed",
    "worktree_cleanup_incomplete",
}


def discord_event_payloads(
    job: Job, event: JobEvent
) -> tuple[dict[str, Any], ...]:
    """Format a durable job event without performing any network request."""

    if not _is_notifiable(event):
        return ()
    title, color = _presentation(event)
    details = json.loads(event.details_json)
    embed = {
        "title": title,
        "description": f"**{_limit(job.repository, 300)}** · `{_limit(job.branch, 100)}`",
        "color": color,
        "fields": [
            {"name": "작업", "value": str(job.id), "inline": True},
            {"name": "Event ID", "value": str(event.id), "inline": True},
            {"name": "상태", "value": _limit(event.status, 100), "inline": True},
            {
                "name": "Finding",
                "value": (
                    f"{job.severity} · `{_limit(job.file, 300)}:{job.line}`\n"
                    f"`{job.fingerprint}`"
                ),
                "inline": False,
            },
            {
                "name": "처리 내용",
                "value": _limit(event.message, 1000),
                "inline": False,
            },
            {
                "name": "세부 정보",
                "value": _details(details),
                "inline": False,
            },
        ],
        "timestamp": event.created_at,
    }
    return (_payload([embed]),)


def _is_notifiable(event: JobEvent) -> bool:
    return event.event_type in _NOTIFIABLE_EVENTS or (
        event.event_type == "status_changed"
        and event.status in {"completed", "rejected", "failed"}
    ) or (
        event.event_type == "job_created" and event.status == "skipped"
    )


def _presentation(event: JobEvent) -> tuple[str, int]:
    if event.event_type == "finding_validation_started":
        return "🔎 코드 수정 finding 검증 시작", INFO_COLOR
    if event.event_type == "finding_validation_completed":
        return "✅ 코드 수정 finding 검증 완료", PASS_COLOR
    if event.event_type == "fix_started":
        return "🛠️ 코드 수정 시작", INFO_COLOR
    if event.event_type == "fix_applied":
        return "✅ 코드 수정 적용 완료", PASS_COLOR
    if event.event_type == "target_moved":
        return "🔄 코드 수정 target 변경 감지", INFO_COLOR
    if event.event_type == "merge_conflict_detected":
        return "⚠️ 코드 수정 merge 충돌", WARNING_COLOR
    if event.event_type == "merge_conflict_resolved":
        return "✅ 코드 수정 merge 충돌 해결", PASS_COLOR
    if event.event_type == "push_completed":
        return "✅ 코드 수정 push 완료", PASS_COLOR
    if event.event_type == "worktree_cleanup_incomplete":
        return "❌ 코드 수정 worktree 정리 실패", FAIL_COLOR
    if event.status == "completed":
        return "✅ 코드 수정 완료", PASS_COLOR
    if event.status == "rejected":
        return "ℹ️ 코드 수정 제외", INFO_COLOR
    if event.status == "skipped":
        return "ℹ️ 코드 수정 정책 제외", INFO_COLOR
    return "❌ 코드 수정 실패", FAIL_COLOR


def _details(details: object) -> str:
    text = json.dumps(details, ensure_ascii=False, sort_keys=True, indent=2)
    text = text.replace("```", "'''")
    return f"```json\n{_limit(text, 3000)}\n```"


def _limit(value: str, maximum: int) -> str:
    value = value.replace("@everyone", "＠everyone").replace("@here", "＠here")
    return value if len(value) <= maximum else value[: maximum - 1] + "…"


def _payload(embeds: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "username": "Code Fix Agent",
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
    }
