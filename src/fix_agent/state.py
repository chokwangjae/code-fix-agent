from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import uuid

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
    next_attempt_at: str | None
    precheck_status: str | None
    precheck_reason: str | None
    postcheck_status: str | None
    postcheck_reason: str | None
    tests_json: str
    fix_branch: str | None
    result_commit: str | None
    pr_url: str | None
    batch_id: str | None
    fallback_finding: int
    execution_started_at: str | None
    timing_status: str
    target_exceeded_at: str | None
    overdue_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IntakeResult:
    job_ids: tuple[int, ...]
    created: int
    duplicate: int
    skipped: int
    batch_id: str | None = None


@dataclass(frozen=True)
class BatchClaim:
    id: str
    jobs: tuple[Job, ...]
    attempt: int
    started_at: str


@dataclass(frozen=True)
class BatchRun:
    id: str
    repository_id: str
    status: str
    attempts: int
    codex_calls: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    duration_ms: int
    last_error: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PublishCheckpoint:
    scope_id: str
    group_key: str
    batch_id: str | None
    sequence: int
    branch: str
    commit: str
    fingerprints: tuple[str, ...]
    files: tuple[str, ...]
    title: str
    status: str
    created_at: str
    updated_at: str


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


@dataclass(frozen=True)
class WorkerControl:
    paused: bool
    reason: str | None
    updated_at: str


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "jobs.db"
        self.connection = sqlite3.connect(self.path, timeout=30)
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
                next_attempt_at TEXT,
                precheck_status TEXT,
                precheck_reason TEXT,
                postcheck_status TEXT,
                postcheck_reason TEXT,
                tests_json TEXT NOT NULL DEFAULT '[]',
                fix_branch TEXT,
                result_commit TEXT,
                pr_url TEXT,
                batch_id TEXT,
                fallback_finding INTEGER NOT NULL DEFAULT 0,
                execution_started_at TEXT,
                timing_status TEXT NOT NULL DEFAULT 'on_time',
                target_exceeded_at TEXT,
                overdue_reason TEXT,
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
            CREATE TABLE IF NOT EXISTS batch_runs (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                codex_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS worker_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                paused INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_checkpoints (
                scope_id TEXT NOT NULL,
                group_key TEXT NOT NULL,
                batch_id TEXT,
                sequence INTEGER NOT NULL,
                branch TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                fingerprints_json TEXT NOT NULL,
                files_json TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_id, group_key)
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO worker_control (id, paused, reason, updated_at)
            VALUES (1, 0, NULL, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            (_now(),),
        )
        legacy_pause = self.connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'trigger' AND name = 'manual_pause_claims_20260810'
            """
        ).fetchone()
        if legacy_pause is not None:
            self.connection.execute(
                """
                UPDATE worker_control
                SET paused = 1, reason = ?, updated_at = ?
                WHERE id = 1
                """,
                ("migrated from manual_pause_claims_20260810", _now()),
            )
            self.connection.execute(
                "DROP TRIGGER IF EXISTS manual_pause_claims_20260810"
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
            ("next_attempt_at", "TEXT"),
            ("batch_id", "TEXT"),
            ("fallback_finding", "INTEGER NOT NULL DEFAULT 0"),
            ("execution_started_at", "TEXT"),
            ("timing_status", "TEXT NOT NULL DEFAULT 'on_time'"),
            ("target_exceeded_at", "TEXT"),
            ("overdue_reason", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {name} {declaration}"
                )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS jobs_batch_id ON jobs (batch_id, id)"
        )
        self.connection.commit()

    def worker_control(self) -> WorkerControl:
        row = self.connection.execute(
            "SELECT paused, reason, updated_at FROM worker_control WHERE id = 1"
        ).fetchone()
        if row is None:  # pragma: no cover - schema initialization guarantees the row
            raise sqlite3.DatabaseError("worker control row does not exist")
        return WorkerControl(bool(row["paused"]), row["reason"], row["updated_at"])

    def set_worker_paused(self, paused: bool, reason: str | None = None) -> None:
        normalized_reason = reason.strip() if reason and reason.strip() else None
        with self.connection:
            self.connection.execute(
                """
                UPDATE worker_control
                SET paused = ?, reason = ?, updated_at = ?
                WHERE id = 1
                """,
                (int(paused), normalized_reason if paused else None, _now()),
            )

    def record_publish_checkpoint(
        self,
        job_ids: tuple[int, ...],
        *,
        batch_id: str | None,
        sequence: int,
        branch: str,
        commit: str,
        fingerprints: tuple[str, ...],
        files: tuple[str, ...],
        title: str,
    ) -> None:
        if not job_ids or not fingerprints:
            raise ValueError("publish checkpoint requires jobs and fingerprints")
        scope_id = f"batch:{batch_id}" if batch_id else f"job:{job_ids[0]}"
        group_key = "|".join(sorted(fingerprints))
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO publish_checkpoints (
                    scope_id, group_key, batch_id, sequence, branch, commit_hash,
                    fingerprints_json, files_json, title, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                ON CONFLICT (scope_id, group_key) DO UPDATE SET
                    sequence = excluded.sequence,
                    branch = excluded.branch,
                    commit_hash = excluded.commit_hash,
                    fingerprints_json = excluded.fingerprints_json,
                    files_json = excluded.files_json,
                    title = excluded.title,
                    status = 'ready',
                    updated_at = excluded.updated_at
                """,
                (
                    scope_id,
                    group_key,
                    batch_id,
                    sequence,
                    branch,
                    commit,
                    json.dumps(fingerprints, ensure_ascii=False),
                    json.dumps(files, ensure_ascii=False),
                    title,
                    now,
                    now,
                ),
            )
            for job_id in job_ids:
                self.connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'ready', fix_branch = ?, result_commit = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (branch, commit, now, job_id),
                )
                self._insert_event(
                    job_id,
                    "publish_checkpoint_recorded",
                    "validated commit was checkpointed before push",
                    {
                        "batch_id": batch_id,
                        "sequence": sequence,
                        "branch": branch,
                        "commit": commit,
                        "fingerprints": list(fingerprints),
                        "files": list(files),
                        "title": title,
                    },
                    status="ready",
                )

    def publish_checkpoints(self, scope_id: str) -> tuple[PublishCheckpoint, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM publish_checkpoints
            WHERE scope_id = ?
            ORDER BY sequence, created_at
            """,
            (scope_id,),
        ).fetchall()
        return tuple(_publish_checkpoint(row) for row in rows)

    def mark_publish_checkpoint_pushed(
        self, scope_id: str, fingerprints: tuple[str, ...]
    ) -> None:
        group_key = "|".join(sorted(fingerprints))
        with self.connection:
            self.connection.execute(
                """
                UPDATE publish_checkpoints
                SET status = 'pushed', updated_at = ?
                WHERE scope_id = ? AND group_key = ?
                """,
                (_now(), scope_id, group_key),
            )

    def accept(self, repository: RepositoryConfig, event: ReviewEvent) -> IntakeResult:
        now = datetime.now(timezone.utc).isoformat()
        batch_id = (
            uuid.uuid4().hex if repository.processing_mode == "review_batch" else None
        )
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
                        batch_id, fallback_finding, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                        batch_id,
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
            if batch_id is not None and created:
                status = "completed" if created == skipped else "queued"
                self.connection.execute(
                    """
                    INSERT INTO batch_runs (
                        id, repository_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (batch_id, repository.id, status, now, now),
                )
        return IntakeResult(
            tuple(job_ids), created, duplicate, skipped, batch_id if created else None
        )

    def jobs(self, limit: int = 100) -> tuple[Job, ...]:
        rows = self.connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(Job(**dict(row)) for row in rows)

    def job(self, job_id: int) -> Job | None:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return Job(**dict(row)) if row is not None else None

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

    def has_event(self, job_id: int, event_type: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM job_events WHERE job_id = ? AND event_type = ? LIMIT 1",
            (job_id, event_type),
        ).fetchone()
        return row is not None

    def mark_overdue(self, job_ids: tuple[int, ...], reason: str) -> None:
        if not job_ids:
            return
        now = _now()
        placeholders = ",".join("?" for _ in job_ids)
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE jobs
                SET timing_status = 'overdue',
                    target_exceeded_at = COALESCE(target_exceeded_at, ?),
                    overdue_reason = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, reason[:4_000], now, *job_ids),
            )

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
        settings = {repository.id: repository for repository in repositories}
        now = _now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.worker_control().paused:
                self.connection.commit()
                return None
            rows = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                """,
                (now,),
            ).fetchall()
            row = next(
                (
                    value
                    for value in rows
                    if value["repository_id"] in settings
                    and (
                        settings[value["repository_id"]].processing_mode == "finding"
                        or bool(value["fallback_finding"])
                    )
                    and (
                        settings[value["repository_id"]].max_attempts == 0
                        or value["attempts"]
                        < settings[value["repository_id"]].max_attempts
                    )
                ),
                None,
            )
            if row is None:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                """
                UPDATE jobs
                SET status = 'validating', attempts = attempts + 1,
                    next_attempt_at = NULL,
                    execution_started_at = COALESCE(execution_started_at, ?),
                    updated_at = ?
                WHERE id = ? AND status IN ('queued', 'failed')
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
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
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return Job(**dict(claimed)) if claimed is not None else None

    def next_claim_kind(
        self, repositories: tuple[RepositoryConfig, ...]
    ) -> str | None:
        if self.worker_control().paused:
            return None
        settings = {repository.id: repository for repository in repositories}
        rows = self.connection.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('queued', 'failed')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at, id
            """,
            (_now(),),
        ).fetchall()
        for row in rows:
            repository = settings.get(row["repository_id"])
            if repository is None:
                continue
            if repository.max_attempts and row["attempts"] >= repository.max_attempts:
                continue
            if bool(row["fallback_finding"]):
                return "finding"
            if repository.processing_mode == "finding":
                return "finding"
            if row["batch_id"] is not None:
                return "batch"
        return None

    def claim_next_batch(
        self, repositories: tuple[RepositoryConfig, ...]
    ) -> BatchClaim | None:
        settings = {repository.id: repository for repository in repositories}
        now = _now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.worker_control().paused:
                self.connection.commit()
                return None
            rows = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE batch_id IS NOT NULL
                  AND fallback_finding = 0
                  AND status IN ('queued', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                """,
                (now,),
            ).fetchall()
            first = next(
                (
                    row
                    for row in rows
                    if row["repository_id"] in settings
                    and settings[row["repository_id"]].processing_mode
                    == "review_batch"
                    and (
                        settings[row["repository_id"]].max_attempts == 0
                        or row["attempts"]
                        < settings[row["repository_id"]].max_attempts
                    )
                ),
                None,
            )
            if first is None:
                self.connection.commit()
                return None
            batch_id = first["batch_id"]
            batch_rows = [row for row in rows if row["batch_id"] == batch_id]
            claimed_ids = [row["id"] for row in batch_rows]
            placeholders = ",".join("?" for _ in claimed_ids)
            cursor = self.connection.execute(
                f"""
                UPDATE jobs
                SET status = 'validating', attempts = attempts + 1,
                    next_attempt_at = NULL,
                    execution_started_at = COALESCE(execution_started_at, ?),
                    updated_at = ?
                WHERE id IN ({placeholders})
                  AND status IN ('queued', 'failed')
                """,
                (now, now, *claimed_ids),
            )
            if cursor.rowcount != len(claimed_ids):
                self.connection.rollback()
                return None
            attempt = max(int(row["attempts"]) + 1 for row in batch_rows)
            self.connection.execute(
                """
                UPDATE batch_runs
                SET status = 'processing', attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, batch_id),
            )
            for row in batch_rows:
                self._insert_event(
                    row["id"],
                    "batch_claimed",
                    "worker claimed the review batch",
                    {
                        "batch_id": batch_id,
                        "batch_size": len(batch_rows),
                        "attempt": int(row["attempts"]) + 1,
                    },
                    status="validating",
                )
            claimed = self.connection.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY id",
                claimed_ids,
            ).fetchall()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        run = self.batch_run(str(batch_id))
        if run is None or run.started_at is None:  # pragma: no cover - schema invariant
            raise sqlite3.DatabaseError(f"batch run does not exist: {batch_id}")
        return BatchClaim(
            str(batch_id),
            tuple(Job(**dict(row)) for row in claimed),
            attempt,
            run.started_at,
        )

    def batch_run(self, batch_id: str) -> BatchRun | None:
        row = self.connection.execute(
            "SELECT * FROM batch_runs WHERE id = ?", (batch_id,)
        ).fetchone()
        return BatchRun(**dict(row)) if row is not None else None

    def record_batch_metrics(
        self,
        batch_id: str,
        *,
        codex_calls: int,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_input_tokens: int,
        output_tokens: int,
        reasoning_output_tokens: int,
        total_tokens: int,
        duration_ms: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE batch_runs
                SET codex_calls = codex_calls + ?,
                    input_tokens = input_tokens + ?,
                    cached_input_tokens = cached_input_tokens + ?,
                    cache_write_input_tokens = cache_write_input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    reasoning_output_tokens = reasoning_output_tokens + ?,
                    total_tokens = total_tokens + ?,
                    duration_ms = duration_ms + ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    codex_calls,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    output_tokens,
                    reasoning_output_tokens,
                    total_tokens,
                    duration_ms,
                    _now(),
                    batch_id,
                ),
            )

    def mark_batch_failed(self, batch_id: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE batch_runs
                SET status = 'failed', last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:20_000], _now(), batch_id),
            )

    def mark_batch_completed(self, batch_id: str) -> None:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE batch_runs
                SET status = 'completed', last_error = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, batch_id),
            )

    def mark_finding_fallback(self, job_ids: tuple[int, ...], reason: str) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        now = _now()
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE jobs
                SET fallback_finding = 1, status = 'queued', attempts = 0,
                    last_error = ?, next_attempt_at = NULL, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (reason[:20_000], now, *job_ids),
            )
            for job_id in job_ids:
                self._insert_event(
                    job_id,
                    "batch_finding_fallback",
                    "repeated batch failure isolated the finding",
                    {"reason": reason[:4_000]},
                    status="queued",
                )

    def mark_finding_fallback_pending(
        self, job_ids: tuple[int, ...], reason: str
    ) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        now = _now()
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE jobs
                SET fallback_finding = 1, status = 'fallback_pending', attempts = 0,
                    last_error = ?, next_attempt_at = NULL, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (reason[:20_000], now, *job_ids),
            )
            for job_id in job_ids:
                self._insert_event(
                    job_id,
                    "batch_finding_fallback_pending",
                    "finding fallback is waiting for batch worktree cleanup",
                    {"reason": reason[:4_000]},
                    status="fallback_pending",
                )

    def activate_finding_fallback(
        self, job_ids: tuple[int, ...], reason: str
    ) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        now = _now()
        with self.connection:
            rows = self.connection.execute(
                f"""
                SELECT id FROM jobs
                WHERE id IN ({placeholders}) AND status = 'fallback_pending'
                ORDER BY id
                """,
                job_ids,
            ).fetchall()
            activated = tuple(int(row["id"]) for row in rows)
            if not activated:
                return
            activated_placeholders = ",".join("?" for _ in activated)
            self.connection.execute(
                f"""
                UPDATE jobs
                SET status = 'queued', next_attempt_at = NULL, updated_at = ?
                WHERE id IN ({activated_placeholders})
                """,
                (now, *activated),
            )
            for job_id in activated:
                self._insert_event(
                    job_id,
                    "batch_finding_fallback",
                    "batch cleanup completed and finding fallback was queued",
                    {"reason": reason[:4_000]},
                    status="queued",
                )

    def recover_interrupted_jobs(self) -> tuple[int, ...]:
        now = _now()
        recovered: list[int] = []
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                """
                SELECT id, status, attempts, last_error
                FROM jobs
                WHERE status IN (
                    'validating', 'fixing', 'testing', 'ready', 'pushed',
                    'fallback_pending'
                )
                ORDER BY id
                """
            ).fetchall()
            for row in rows:
                previous_status = row["status"]
                if previous_status == "fallback_pending":
                    self.connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'queued', next_attempt_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, row["id"]),
                    )
                    self._insert_event(
                        row["id"],
                        "fallback_recovery_scheduled",
                        "interrupted fallback will start in a finding worktree",
                        {"previous_status": previous_status},
                        status="queued",
                    )
                    recovered.append(row["id"])
                    continue
                interruption = (
                    f"process restarted while job was {previous_status}; "
                    "resume the recorded worktree"
                )
                previous_error = row["last_error"]
                error = (
                    f"{interruption}\nPrevious error: {previous_error}"
                    if previous_error
                    else interruption
                )
                self.connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', attempts = CASE
                            WHEN attempts > 0 THEN attempts - 1
                            ELSE 0
                        END,
                        last_error = ?, next_attempt_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:20_000], now, now, row["id"]),
                )
                self._insert_event(
                    row["id"],
                    "restart_recovery_scheduled",
                    "interrupted job will resume from its recorded worktree",
                    {
                        "previous_status": previous_status,
                        "attempt_preserved": row["attempts"],
                    },
                    status="failed",
                )
                recovered.append(row["id"])
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return tuple(recovered)

    def resumable_worktree(
        self, job_id: int, *, scope: str = "finding"
    ) -> tuple[str, str] | None:
        if scope not in {"batch", "finding"}:
            raise ValueError(f"invalid worktree scope: {scope}")
        rows = self.connection.execute(
            """
            SELECT id, details_json
            FROM job_events
            WHERE job_id = ? AND event_type = 'worktree_created'
            ORDER BY id DESC
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            return None
        removed_rows = self.connection.execute(
            """
            SELECT id, details_json
            FROM job_events
            WHERE job_id = ? AND event_type = 'worktree_removed'
            ORDER BY id DESC
            """,
            (job_id,),
        ).fetchall()
        job = self.connection.execute(
            "SELECT batch_id, fallback_finding FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        fallback = self.connection.execute(
            """
            SELECT MAX(id) AS id FROM job_events
            WHERE job_id = ? AND event_type IN (
                'batch_fallback_started', 'batch_finding_fallback',
                'batch_finding_fallback_pending'
            )
            """,
            (job_id,),
        ).fetchone()
        fallback_event_id = int(fallback["id"] or 0) if fallback is not None else 0
        for row in rows:
            try:
                details = json.loads(row["details_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(details, dict):
                continue
            recorded_scope = details.get("scope")
            if isinstance(recorded_scope, str):
                if recorded_scope != scope:
                    continue
            elif (
                scope == "finding"
                and job is not None
                and job["batch_id"] is not None
                and bool(job["fallback_finding"])
                and int(row["id"]) < fallback_event_id
            ):
                continue
            path = details.get("path")
            base_commit = details.get("base_commit")
            if not isinstance(path, str) or not path:
                continue
            if not isinstance(base_commit, str) or not base_commit:
                continue
            if any(
                int(removed["id"]) > int(row["id"])
                and _event_path(removed["details_json"]) == path
                for removed in removed_rows
            ):
                continue
            if not Path(path).is_dir():
                continue
            return path, base_commit
        return None

    def resumable_batch_worktree(self, batch_id: str) -> tuple[str, str] | None:
        rows = self.connection.execute(
            """
            SELECT event.job_id
            FROM job_events AS event
            JOIN jobs AS job ON job.id = event.job_id
            WHERE job.batch_id = ? AND event.event_type = 'worktree_created'
            ORDER BY event.id DESC
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            resumable = self.resumable_worktree(
                int(row["job_id"]), scope="batch"
            )
            if resumable is not None:
                return resumable
        return None

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

    def record_fix_iteration_failure(
        self, job_id: int, error: str, results: list[dict[str, object]]
    ) -> None:
        self._update(
            job_id,
            "fixing",
            last_error=error[:20_000],
            tests_json=json.dumps(results, ensure_ascii=False),
        )

    def record_postcheck(
        self,
        job_id: int,
        valid: bool,
        reason: str,
        *,
        retry_on_failure: bool = False,
    ) -> None:
        self._update(
            job_id,
            "ready" if valid else ("fixing" if retry_on_failure else "rejected"),
            postcheck_status="resolved" if valid else "invalid",
            postcheck_reason=reason,
        )

    def mark_pushed(self, job_id: int, branch: str, commit: str) -> None:
        self._update(job_id, "pushed", fix_branch=branch, result_commit=commit)

    def mark_completed(self, job_id: int, pr_url: str | None) -> None:
        self._update(
            job_id,
            "completed",
            pr_url=pr_url,
            last_error=None,
            next_attempt_at=None,
        )

    def mark_failed(
        self, job_id: int, error: str, retry_after_seconds: int | None = None
    ) -> str | None:
        next_attempt_at = None
        if retry_after_seconds is not None:
            next_attempt_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=retry_after_seconds)
            ).isoformat()
        self._update(
            job_id,
            "failed",
            last_error=error[:20_000],
            next_attempt_at=next_attempt_at,
        )
        return next_attempt_at

    def _update(self, job_id: int, status: str, **values: object) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        parameters: list[object] = [status, _now()]
        for key, value in values.items():
            if key not in {
                "last_error",
                "next_attempt_at",
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


def _publish_checkpoint(row: sqlite3.Row) -> PublishCheckpoint:
    fingerprints = json.loads(row["fingerprints_json"])
    files = json.loads(row["files_json"])
    if not isinstance(fingerprints, list) or not all(
        isinstance(value, str) for value in fingerprints
    ):
        raise sqlite3.DatabaseError("publish checkpoint fingerprints are invalid")
    if not isinstance(files, list) or not all(isinstance(value, str) for value in files):
        raise sqlite3.DatabaseError("publish checkpoint files are invalid")
    return PublishCheckpoint(
        scope_id=row["scope_id"],
        group_key=row["group_key"],
        batch_id=row["batch_id"],
        sequence=row["sequence"],
        branch=row["branch"],
        commit=row["commit_hash"],
        fingerprints=tuple(fingerprints),
        files=tuple(files),
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_path(details_json: str) -> str | None:
    try:
        details = json.loads(details_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(details, dict):
        return None
    path = details.get("path")
    return path if isinstance(path, str) else None
