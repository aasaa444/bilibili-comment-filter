from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .db import Database


@dataclass(frozen=True)
class TaskEventRecord:
    event_id: int
    task_id: str
    attempt: int
    phase: str
    event_type: str
    status: str
    message: str
    details: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class AnalysisRunRecord:
    analysis_id: str
    task_id: str
    attempt: int
    status: str
    model: str | None
    sample_version: str | None
    batch_count: int
    account_count: int
    hit_count: int
    uncertain_count: int
    non_target_count: int
    evidence_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class TaskEventStore:
    """Persist a safe, task-scoped execution timeline without request credentials."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        *,
        task_id: str,
        attempt: int,
        phase: str,
        event_type: str,
        status: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> TaskEventRecord:
        timestamp = datetime.now(UTC).isoformat()
        safe_details = details or {}
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_events
                    (task_id, attempt, phase, event_type, status, message, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt,
                    phase,
                    event_type,
                    status,
                    message,
                    json.dumps(safe_details, ensure_ascii=False, default=str),
                    timestamp,
                ),
            )
            event_id = int(cursor.lastrowid)
        row = self.database.execute(
            "SELECT * FROM task_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._from_row(row)

    def list_for_task(self, task_id: str) -> tuple[TaskEventRecord, ...]:
        rows = self.database.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY created_at ASC, event_id ASC
            """,
            (task_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: Any) -> TaskEventRecord:
        details = json.loads(row["details_json"])
        return TaskEventRecord(
            event_id=int(row["event_id"]),
            task_id=row["task_id"],
            attempt=int(row["attempt"]),
            phase=row["phase"],
            event_type=row["event_type"],
            status=row["status"],
            message=row["message"],
            details=details if isinstance(details, dict) else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class AnalysisRunStore:
    """Persist one compact AI summary per task attempt, including failed attempts."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start(
        self,
        *,
        task_id: str,
        attempt: int,
        account_count: int,
        model: str | None = None,
    ) -> AnalysisRunRecord:
        timestamp = datetime.now(UTC).isoformat()
        existing = self._row(task_id, attempt)
        analysis_id = existing["analysis_id"] if existing is not None else uuid4().hex
        with self.database.transaction() as connection:
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO task_analysis_runs
                        (analysis_id, task_id, attempt, status, model, batch_count, account_count,
                         hit_count, uncertain_count, non_target_count, evidence_count,
                         started_at)
                    VALUES (?, ?, ?, 'running', ?, 0, ?, 0, 0, 0, 0, ?)
                    """,
                    (analysis_id, task_id, attempt, model, account_count, timestamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_analysis_runs
                    SET status = 'running', model = ?, sample_version = NULL,
                        batch_count = 0, account_count = ?, hit_count = 0,
                        uncertain_count = 0, non_target_count = 0, evidence_count = 0,
                        error_code = NULL, error_message = NULL,
                        started_at = ?, completed_at = NULL
                    WHERE task_id = ? AND attempt = ?
                    """,
                    (model, account_count, timestamp, task_id, attempt),
                )
        return self.get(analysis_id)

    def set_sample_version(self, task_id: str, attempt: int, sample_version: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE task_analysis_runs
                SET sample_version = ?
                WHERE task_id = ? AND attempt = ?
                """,
                (sample_version, task_id, attempt),
            )

    def finish(
        self,
        *,
        task_id: str,
        attempt: int,
        status: str,
        account_count: int,
        batch_count: int,
        hit_count: int,
        uncertain_count: int,
        non_target_count: int,
        evidence_count: int,
        model: str | None = None,
        sample_version: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AnalysisRunRecord:
        existing = self._row(task_id, attempt)
        if existing is None:
            self.start(task_id=task_id, attempt=attempt, account_count=account_count, model=model)
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE task_analysis_runs
                SET status = ?, model = COALESCE(?, model),
                    sample_version = COALESCE(?, sample_version),
                    batch_count = ?, account_count = ?, hit_count = ?, uncertain_count = ?,
                    non_target_count = ?, evidence_count = ?, error_code = ?, error_message = ?,
                    completed_at = ?
                WHERE task_id = ? AND attempt = ?
                """,
                (
                    status,
                    model,
                    sample_version,
                    batch_count,
                    account_count,
                    hit_count,
                    uncertain_count,
                    non_target_count,
                    evidence_count,
                    error_code,
                    error_message,
                    timestamp,
                    task_id,
                    attempt,
                ),
            )
        row = self._row(task_id, attempt)
        return self._from_row(row)

    def not_started(
        self,
        *,
        task_id: str,
        attempt: int,
        error_code: str,
        error_message: str,
        account_count: int = 0,
    ) -> AnalysisRunRecord:
        existing = self._row(task_id, attempt)
        if existing is None:
            self.start(task_id=task_id, attempt=attempt, account_count=account_count)
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE task_analysis_runs
                SET status = 'not_started', account_count = ?, batch_count = 0,
                    hit_count = 0, uncertain_count = 0, non_target_count = 0,
                    evidence_count = 0, error_code = ?, error_message = ?,
                    completed_at = ?
                WHERE task_id = ? AND attempt = ?
                """,
                (account_count, error_code, error_message, timestamp, task_id, attempt),
            )
        row = self._row(task_id, attempt)
        return self._from_row(row)

    def list_for_task(self, task_id: str) -> tuple[AnalysisRunRecord, ...]:
        rows = self.database.execute(
            """
            SELECT * FROM task_analysis_runs
            WHERE task_id = ?
            ORDER BY attempt DESC, COALESCE(completed_at, started_at) DESC, analysis_id DESC
            """,
            (task_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def get(self, analysis_id: str) -> AnalysisRunRecord:
        row = self.database.execute(
            "SELECT * FROM task_analysis_runs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Analysis run {analysis_id} was not found")
        return self._from_row(row)

    def _row(self, task_id: str, attempt: int) -> Any:
        return self.database.execute(
            "SELECT * FROM task_analysis_runs WHERE task_id = ? AND attempt = ?",
            (task_id, attempt),
        ).fetchone()

    @staticmethod
    def _from_row(row: Any) -> AnalysisRunRecord:
        return AnalysisRunRecord(
            analysis_id=row["analysis_id"],
            task_id=row["task_id"],
            attempt=int(row["attempt"]),
            status=row["status"],
            model=row["model"],
            sample_version=row["sample_version"],
            batch_count=int(row["batch_count"]),
            account_count=int(row["account_count"]),
            hit_count=int(row["hit_count"]),
            uncertain_count=int(row["uncertain_count"]),
            non_target_count=int(row["non_target_count"]),
            evidence_count=int(row["evidence_count"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] is not None else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
        )
