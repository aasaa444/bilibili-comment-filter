from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .db import Database
from .models import TaskStatus


class TaskNotFoundError(LookupError):
    pass


class InvalidTaskTransitionError(ValueError):
    pass


class UnsupportedVideoError(ValueError):
    pass


@dataclass(frozen=True)
class TaskProgress:
    requested_pages: int = 0
    saved_comments: int = 0
    saved_replies: int = 0
    pinned_comments: int = 0
    declared_comments: int = 0
    declared_replies: int = 0
    coverage: float = 0.0
    failed_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoTask:
    task_id: str
    video_id: str
    video_url: str
    title: str | None
    status: TaskStatus
    submitted_at: datetime
    updated_at: datetime
    attempt: int
    error_code: str | None
    error_message: str | None
    progress: TaskProgress


class TaskStore:
    ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
        TaskStatus.QUEUED: frozenset(
            {TaskStatus.QUEUED, TaskStatus.COLLECTING, TaskStatus.PAUSED, TaskStatus.FAILED}
        ),
        TaskStatus.COLLECTING: frozenset(
            {
                TaskStatus.COLLECTING,
                TaskStatus.ANALYZING,
                TaskStatus.PARTIAL,
                TaskStatus.PAUSED,
                TaskStatus.FAILED,
            }
        ),
        TaskStatus.ANALYZING: frozenset(
            {
                TaskStatus.ANALYZING,
                TaskStatus.COMPLETED,
                TaskStatus.PARTIAL,
                TaskStatus.PAUSED,
                TaskStatus.FAILED,
            }
        ),
        TaskStatus.COMPLETED: frozenset({TaskStatus.COMPLETED}),
        TaskStatus.PARTIAL: frozenset(
            {
                TaskStatus.PARTIAL,
                TaskStatus.QUEUED,
                TaskStatus.COLLECTING,
                TaskStatus.PAUSED,
                TaskStatus.FAILED,
                TaskStatus.COMPLETED,
            }
        ),
        TaskStatus.FAILED: frozenset(
            {TaskStatus.FAILED, TaskStatus.QUEUED, TaskStatus.COLLECTING, TaskStatus.PAUSED}
        ),
        TaskStatus.PAUSED: frozenset(
            {
                TaskStatus.PAUSED,
                TaskStatus.QUEUED,
                TaskStatus.COLLECTING,
                TaskStatus.FAILED,
                TaskStatus.PARTIAL,
            }
        ),
    }

    VIDEO_ID_PATTERN = re.compile(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)(?:[/?#]|$)", re.IGNORECASE)

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, video_url: str, title: str | None = None) -> tuple[VideoTask, bool]:
        video_id = self.video_id_from_url(video_url)
        existing = self.database.execute(
            "SELECT * FROM video_tasks WHERE video_id = ?", (video_id,)
        ).fetchone()
        if existing is not None:
            return self._from_row(existing), False

        task_id = uuid4().hex
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO video_tasks
                    (task_id, video_id, video_url, title, status, submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    video_id,
                    video_url,
                    title,
                    TaskStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_checkpoints (task_id, updated_at)
                VALUES (?, ?)
                """,
                (task_id, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM video_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._from_row(row), True

    def get(self, task_id: str) -> VideoTask:
        row = self.database.execute(
            "SELECT * FROM video_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._from_row(row)

    def list(self) -> list[VideoTask]:
        rows = self.database.execute(
            "SELECT * FROM video_tasks ORDER BY submitted_at DESC"
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def transition(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> VideoTask:
        current = self.get(task_id)
        if status not in self.ALLOWED_TRANSITIONS[current.status]:
            raise InvalidTaskTransitionError(
                f"Cannot transition task {task_id} from {current.status.value} to {status.value}"
            )
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE video_tasks
                SET status = ?, updated_at = ?, error_code = ?, error_message = ?
                WHERE task_id = ?
                """,
                (status.value, timestamp, error_code, error_message, task_id),
            )
        return self.get(task_id)

    def retry(self, task_id: str) -> VideoTask:
        current = self.get(task_id)
        if current.status not in {
            TaskStatus.FAILED,
            TaskStatus.PARTIAL,
            TaskStatus.PAUSED,
        }:
            raise InvalidTaskTransitionError("Only failed, partial, or paused tasks can be retried")
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE video_tasks
                SET status = ?, attempt = attempt + 1, updated_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE task_id = ?
                """,
                (TaskStatus.QUEUED.value, timestamp, task_id),
            )
        return self.get(task_id)

    def update_progress(
        self, task_id: str, progress: TaskProgress, *, checkpoint: dict[str, object] | None = None
    ) -> VideoTask:
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE video_tasks
                SET requested_pages = ?, saved_comments = ?, saved_replies = ?,
                    pinned_comments = ?, declared_comments = ?, declared_replies = ?,
                    coverage = ?, failed_items_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    progress.requested_pages,
                    progress.saved_comments,
                    progress.saved_replies,
                    progress.pinned_comments,
                    progress.declared_comments,
                    progress.declared_replies,
                    progress.coverage,
                    json.dumps(list(progress.failed_items)),
                    timestamp,
                    task_id,
                ),
            )
            if checkpoint is not None:
                connection.execute(
                    """
                    UPDATE task_checkpoints
                    SET root_page = ?, replies_json = ?, complete = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        int(checkpoint.get("root_page", 1)),
                        json.dumps(checkpoint.get("replies", {}), sort_keys=True),
                        int(bool(checkpoint.get("complete", False))),
                        timestamp,
                        task_id,
                    ),
                )
        return self.get(task_id)

    def checkpoint(self, task_id: str) -> dict[str, object]:
        self.get(task_id)
        row = self.database.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return {"root_page": 1, "replies": {}, "complete": False}
        return {
            "root_page": int(row["root_page"]),
            "replies": json.loads(row["replies_json"]),
            "complete": bool(row["complete"]),
        }

    @classmethod
    def video_id_from_url(cls, video_url: str) -> str:
        match = cls.VIDEO_ID_PATTERN.search(video_url)
        if match is None:
            raise UnsupportedVideoError(
                "Only ordinary Bilibili video URLs containing a BV or av identifier are supported"
            )
        value = match.group(1)
        return value if value.startswith("BV") else value.lower()

    @staticmethod
    def _from_row(row: object) -> VideoTask:
        return VideoTask(
            task_id=row["task_id"],
            video_id=row["video_id"],
            video_url=row["video_url"],
            title=row["title"],
            status=TaskStatus(row["status"]),
            submitted_at=datetime.fromisoformat(row["submitted_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            attempt=int(row["attempt"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            progress=TaskProgress(
                requested_pages=int(row["requested_pages"]),
                saved_comments=int(row["saved_comments"]),
                saved_replies=int(row["saved_replies"]),
                pinned_comments=int(row["pinned_comments"]),
                declared_comments=int(row["declared_comments"]),
                declared_replies=int(row["declared_replies"]),
                coverage=float(row["coverage"]),
                failed_items=tuple(json.loads(row["failed_items_json"])),
            ),
        )
