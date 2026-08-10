from datetime import datetime, timezone

from fix_agent.state import Job


def job(**overrides: object) -> Job:
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "id": 1,
        "repository_id": "repo",
        "repository": "owner/repo",
        "branch": "main",
        "baseline_commit": "a" * 40,
        "target_commit": "b" * 40,
        "fingerprint": "sha256:" + "c" * 64,
        "severity": "Major",
        "introducing_commit": "b" * 40,
        "file": "src/app.py",
        "line": 1,
        "cause": "Failure is swallowed.",
        "solution": "Return the failure.",
        "status": "queued",
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
        "precheck_status": None,
        "precheck_reason": None,
        "postcheck_status": None,
        "postcheck_reason": None,
        "tests_json": "[]",
        "fix_branch": None,
        "result_commit": None,
        "pr_url": None,
        "batch_id": None,
        "fallback_finding": 0,
        "execution_started_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return Job(**values)
