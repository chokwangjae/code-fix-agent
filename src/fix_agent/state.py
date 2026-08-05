from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

from .config import RepositoryConfig
from .contract import ReviewEvent


@dataclass(frozen=True)
class Job:
    id: int
    repository_id: str
    repository: str
    branch: str
    baseline_commit: str
    target_commit: str
    fingerprint: str
    severity: str
    introducing_commit: str
    file: str
    line: int
    cause: str
    solution: str
    status: str
    attempts: int
    last_error: str | None
    precheck_status: str | None
    precheck_reason: str | None
    postcheck_status: str | None
    postcheck_reason: str | None
    tests_json: str
    fix_branch: str | None
    result_commit: str | None
    pr_url: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IntakeResult:
    job_ids: tuple[int, ...]
    created: int
    duplicate: int
    skipped: int


@dataclass(frozen=True)
class JobEvent:
    id: int
    job_id: int
    event_type: str
    status: str
    message: str
    details_json: str
    created_at: str


@dataclass(frozen=True)
class DiscordCursor:
    repository_id: str
    last_event_id: int
    failed_event_id: int | None
    attempts: int
    last_error: str | None
    next_attempt_at: str | None
    updated_at: str


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "jobs.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                baseline_commit TEXT NOT NULL,
                target_commit TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                severity TEXT NOT NULL,
                introducing_commit TEXT NOT NULL,
                file TEXT NOT NULL,
                line INTEGER NOT NULL,
                cause TEXT NOT NULL,
                solution TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                precheck_status TEXT,
                precheck_reason TEXT,
                postcheck_status TEXT,
                postcheck_reason TEXT,
                tests_json TEXT NOT NULL DEFAULT '[]',
                fix_branch TEXT,
                result_commit TEXT,
                pr_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (repository, branch, fingerprint)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_cursors (
                repository_id TEXT PRIMARY KEY,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                failed_event_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS job_events_job_id_id
            ON job_events (job_id, id)
            """
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(jobs)")
        }
        for name, declaration in (
            ("precheck_status", "TEXT"),
            ("precheck_reason", "TEXT"),
            ("postcheck_status", "TEXT"),
            ("postcheck_reason", "TEXT"),
            ("tests_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def accept(self, repository: RepositoryConfig, event: ReviewEvent) -> IntakeResult:
        now = datetime.now(timezone.utc).isoformat()
        created = duplicate = skipped = 0
        job_ids: list[int] = []
        with self.connection:
            for finding in event.findings:
                reason = repository.policy.skip_reason(
                    finding.severity, finding.file, finding.fingerprint
                )
                status = "skipped" if reason else "queued"
                cursor = self.connection.execute(
                    """
                    INSERT INTO jobs (
                        repository_id, repository, branch, baseline_commit,
                        target_commit, fingerprint, severity, introducing_commit,
                        file, line, cause, solution, status, last_error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (repository, branch, fingerprint) DO NOTHING
                    """,
                    (
                        repository.id,
                        event.repository,
                        event.branch,
                        event.baseline,
                        event.target,
                        finding.fingerprint,
                        finding.severity,
                        finding.commit,
                        finding.file,
                        finding.line,
                        finding.cause,
                        finding.solution,
                        status,
                        reason,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 0:
                    duplicate += 1
                    row = self.connection.execute(
                        """SELECT id FROM jobs
                           WHERE repository = ? AND branch = ? AND fingerprint = ?""",
                        (event.repository, event.branch, finding.fingerprint),
                    ).fetchone()
                    if row is not None:
                        self._insert_event(
                            row["id"],
                            "duplicate_received",
                            "review finding matched an existing job",
                            {"fingerprint": finding.fingerprint},
                        )
                else:
                    created += 1
                    skipped += int(status == "skipped")
                    row = self.connection.execute(
                        "SELECT id FROM jobs WHERE rowid = last_insert_rowid()"
                    ).fetchone()
                    if row is not None:
                        self._insert_event(
                            row["id"],
                            "job_created",
                            f"job created with status {status}",
                            {"skip_reason": reason} if reason else {},
                            status=status,
                        )
                if row is not None:
                    job_ids.append(row["id"])
        return IntakeResult(tuple(job_ids), created, duplicate, skipped)

    def jobs(self, limit: int = 100) -> tuple[Job, ...]:
        rows = self.connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(Job(**dict(row)) for row in rows)

    def events(
        self, job_id: int | None = None, after_id: int = 0, limit: int = 500
    ) -> tuple[JobEvent, ...]:
        if job_id is None:
            rows = self.connection.execute(
                """
                SELECT * FROM job_events
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (after_id, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (job_id, after_id, limit),
            ).fetchall()
        return tuple(JobEvent(**dict(row)) for row in rows)

    def record_event(
        self,
        job_id: int,
        event_type: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        with self.connection:
            self._insert_event(job_id, event_type, message, details or {})

    def discord_cursor(self, repository_id: str) -> DiscordCursor:
        row = self.connection.execute(
            "SELECT * FROM discord_cursors WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        if row is not None:
            return DiscordCursor(**dict(row))
        return DiscordCursor(repository_id, 0, None, 0, None, None, _now())

    def initialize_discord_cursor(self, repository_id: str) -> DiscordCursor:
        """Start a new notifier after existing events to avoid historical floods."""

        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(event.id), 0) AS latest_event_id
            FROM job_events AS event
            JOIN jobs AS job ON job.id = event.job_id
            WHERE job.repository_id = ?
            """,
            (repository_id,),
        ).fetchone()
        latest_event_id = int(row["latest_event_id"]) if row is not None else 0
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO discord_cursors (
                    repository_id, last_event_id, failed_event_id, attempts,
                    last_error, next_attempt_at, updated_at
                ) VALUES (?, ?, NULL, 0, NULL, NULL, ?)
                ON CONFLICT (repository_id) DO NOTHING
                """,
                (repository_id, latest_event_id, now),
            )
        return self.discord_cursor(repository_id)

    def next_discord_event(
        self, repository_id: str
    ) -> tuple[Job, JobEvent] | None:
        cursor = self.discord_cursor(repository_id)
        event_row = self.connection.execute(
            """
            SELECT event.*
            FROM job_events AS event
            JOIN jobs AS job ON job.id = event.job_id
            WHERE job.repository_id = ? AND event.id > ?
            ORDER BY event.id
            LIMIT 1
            """,
            (repository_id, cursor.last_event_id),
        ).fetchone()
        if event_row is None:
            return None
        job_row = self.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (event_row["job_id"],)
        ).fetchone()
        if job_row is None:  # pragma: no cover - protected by the join
            raise sqlite3.DatabaseError("Discord event job does not exist")
        return Job(**dict(job_row)), JobEvent(**dict(event_row))

    def advance_discord_cursor(self, repository_id: str, event_id: int) -> None:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO discord_cursors (
                    repository_id, last_event_id, failed_event_id, attempts,
                    last_error, next_attempt_at, updated_at
                ) VALUES (?, ?, NULL, 0, NULL, NULL, ?)
                ON CONFLICT (repository_id) DO UPDATE SET
                    last_event_id = CASE
                        WHEN excluded.last_event_id > discord_cursors.last_event_id
                        THEN excluded.last_event_id
                        ELSE discord_cursors.last_event_id
                    END,
                    failed_event_id = NULL,
                    attempts = 0,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (repository_id, event_id, now),
            )

    def fail_discord_event(
        self, repository_id: str, event_id: int, error: str, delay_seconds: int
    ) -> DiscordCursor:
        current = self.discord_cursor(repository_id)
        attempts = current.attempts + 1 if current.failed_event_id == event_id else 1
        now = datetime.now(timezone.utc)
        next_attempt_at = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO discord_cursors (
                    repository_id, last_event_id, failed_event_id, attempts,
                    last_error, next_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repository_id) DO UPDATE SET
                    failed_event_id = excluded.failed_event_id,
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (
                    repository_id,
                    current.last_event_id,
                    event_id,
                    attempts,
                    error[:20_000],
                    next_attempt_at,
                    now.isoformat(),
                ),
            )
        return self.discord_cursor(repository_id)

    def claim_next(self, repositories: tuple[RepositoryConfig, ...]) -> Job | None:
        limits = {repository.id: repository.max_attempts for repository in repositories}
        with self.connection:
            rows = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'failed')
                ORDER BY id
                """
            ).fetchall()
            row = next(
                (
                    value
                    for value in rows
                    if value["repository_id"] in limits
                    and value["attempts"] < limits[value["repository_id"]]
                ),
                None,
            )
            if row is None:
                return None
            cursor = self.connection.execute(
                """
                UPDATE jobs
                SET status = 'validating', attempts = attempts + 1,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'failed')
                """,
                (_now(), row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = self.connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            self._insert_event(
                row["id"],
                "job_claimed",
                "worker claimed the job",
                {"attempt": row["attempts"] + 1},
                status="validating",
            )
        return Job(**dict(claimed)) if claimed is not None else None

    def record_precheck(self, job_id: int, valid: bool, reason: str) -> None:
        self._update(
            job_id,
            "fixing" if valid else "rejected",
            precheck_status="valid" if valid else "invalid",
            precheck_reason=reason,
        )

    def record_tests(self, job_id: int, results: list[dict[str, object]]) -> None:
        self._update(job_id, "testing", tests_json=json.dumps(results, ensure_ascii=False))

    def mark_testing(self, job_id: int) -> None:
        self._update(job_id, "testing")

    def record_postcheck(self, job_id: int, valid: bool, reason: str) -> None:
        self._update(
            job_id,
            "ready" if valid else "rejected",
            postcheck_status="resolved" if valid else "invalid",
            postcheck_reason=reason,
        )

    def mark_pushed(self, job_id: int, branch: str, commit: str) -> None:
        self._update(job_id, "pushed", fix_branch=branch, result_commit=commit)

    def mark_completed(self, job_id: int, pr_url: str | None) -> None:
        self._update(job_id, "completed", pr_url=pr_url)

    def mark_failed(self, job_id: int, error: str) -> None:
        self._update(job_id, "failed", last_error=error[:20_000])

    def _update(self, job_id: int, status: str, **values: object) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        parameters: list[object] = [status, _now()]
        for key, value in values.items():
            if key not in {
                "last_error",
                "precheck_status",
                "precheck_reason",
                "postcheck_status",
                "postcheck_reason",
                "tests_json",
                "fix_branch",
                "result_commit",
                "pr_url",
            }:
                raise ValueError(f"unsupported job field: {key}")
            assignments.append(f"{key} = ?")
            parameters.append(value)
        parameters.append(job_id)
        with self.connection:
            cursor = self.connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", parameters
            )
            if cursor.rowcount != 1:
                raise sqlite3.DatabaseError(f"job does not exist: {job_id}")
            event_details = {
                key: value for key, value in values.items() if key != "tests_json"
            }
            if "tests_json" in values:
                event_details["tests_recorded"] = True
            self._insert_event(
                job_id,
                "status_changed",
                f"job status changed to {status}",
                event_details,
                status=status,
            )

    def _insert_event(
        self,
        job_id: int,
        event_type: str,
        message: str,
        details: dict[str, object],
        *,
        status: str | None = None,
    ) -> None:
        if status is None:
            row = self.connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError(f"job does not exist: {job_id}")
            status = row["status"]
        self.connection.execute(
            """
            INSERT INTO job_events (
                job_id, event_type, status, message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                status,
                message,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
