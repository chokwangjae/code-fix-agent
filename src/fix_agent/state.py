from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .config import RepositoryConfig
from .contract import Finding, ReviewEvent


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
                fix_branch TEXT,
                result_commit TEXT,
                pr_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (repository, branch, fingerprint)
            )
            """
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
                else:
                    created += 1
                    skipped += int(status == "skipped")
                    row = self.connection.execute(
                        "SELECT id FROM jobs WHERE rowid = last_insert_rowid()"
                    ).fetchone()
                if row is not None:
                    job_ids.append(row["id"])
        return IntakeResult(tuple(job_ids), created, duplicate, skipped)

    def jobs(self, limit: int = 100) -> tuple[Job, ...]:
        rows = self.connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(Job(**dict(row)) for row in rows)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
