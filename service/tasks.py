from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

from .cursors import normalize_cursor
from .db import Database
from .models import TaskStatus
from .profiles import DEFAULT_FILTER_PROFILE_ID

if TYPE_CHECKING:
    from .observability import TaskEventStore


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
    declared_total: int | None = None
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
    title_source: str = "legacy"
    profile_id: str = DEFAULT_FILTER_PROFILE_ID


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

    VIDEO_ID_PATTERN = re.compile(r"^/video/(BV[0-9A-Za-z]+)(?:/)?$", re.IGNORECASE)
    SUPPORTED_VIDEO_HOSTS = frozenset({"bilibili.com", "www.bilibili.com"})

    def __init__(self, database: Database, event_store: TaskEventStore | None = None) -> None:
        self.database = database
        self.event_store = event_store

    def create(
        self,
        *,
        video_url: str,
        title: str | None = None,
        title_source: str = "legacy",
        profile_id: str = DEFAULT_FILTER_PROFILE_ID,
    ) -> tuple[VideoTask, bool]:
        video_id = self.video_id_from_url(video_url)
        task_id = uuid4().hex
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO video_tasks
                    (
                        task_id, video_id, video_url, title, title_source,
                        status, submitted_at, updated_at, profile_id
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO NOTHING
                """,
                (
                    task_id,
                    video_id,
                    video_url,
                    title,
                    title_source,
                    TaskStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                    profile_id,
                ),
            )
            created = connection.execute("SELECT changes()").fetchone()[0] == 1
            if created:
                connection.execute(
                    """
                    INSERT INTO task_checkpoints (task_id, updated_at)
                    VALUES (?, ?)
                    """,
                    (task_id, timestamp),
                )
            elif title_source == "bilibili" and title:
                connection.execute(
                    """
                    UPDATE video_tasks
                    SET title = ?, title_source = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (title, title_source, timestamp, video_id),
                )
            row = connection.execute(
                "SELECT * FROM video_tasks WHERE video_id = ?", (video_id,)
            ).fetchone()
        task = self._from_row(row)
        if created and self.event_store is not None:
            self.event_store.append(
                task_id=task.task_id,
                attempt=task.attempt,
                phase="queued",
                event_type="task_created",
                status="queued",
                message="Task was created and queued for collection",
                details={"video_id": task.video_id},
            )
        return task, created

    def update_title(self, task_id: str, title: str, *, source: str = "bilibili") -> VideoTask:
        normalized = title.strip()
        if not normalized:
            return self.get(task_id)
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE video_tasks
                SET title = ?, title_source = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (normalized, source, timestamp, task_id),
            ).rowcount
        if updated == 0:
            raise TaskNotFoundError(task_id)
        return self.get(task_id)

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
        updated = self.get(task_id)
        if self.event_store is not None:
            self.event_store.append(
                task_id=task_id,
                attempt=updated.attempt,
                phase=_task_phase(status),
                event_type="task_transition",
                status="succeeded",
                message=f"Task status changed from {current.status.value} to {status.value}",
                details={"from": current.status.value, "to": status.value},
            )
        return updated

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
        updated = self.get(task_id)
        if self.event_store is not None:
            self.event_store.append(
                task_id=task_id,
                attempt=updated.attempt,
                phase="queued",
                event_type="retry_scheduled",
                status="queued",
                message="Task retry was queued",
                details={"previous_attempt": current.attempt},
            )
        return updated

    def retry_auth_unavailable(self) -> list[VideoTask]:
        """Requeue only tasks paused because a valid Bilibili session was missing."""

        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                UPDATE video_tasks
                SET status = ?, attempt = attempt + 1, updated_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE status = ? AND error_code = ?
                RETURNING task_id
                """,
                (
                    TaskStatus.QUEUED.value,
                    timestamp,
                    TaskStatus.PAUSED.value,
                    "auth_unavailable",
                ),
            ).fetchall()
        tasks = [self.get(row["task_id"]) for row in rows]
        if self.event_store is not None:
            for task in tasks:
                self.event_store.append(
                    task_id=task.task_id,
                    attempt=task.attempt,
                    phase="queued",
                    event_type="retry_scheduled",
                    status="queued",
                    message="Task was requeued after authentication became available",
                    details={"reason": "auth_unavailable"},
                )
        return tasks

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
                    declared_total = ?,
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
                    progress.declared_total,
                    progress.coverage,
                    json.dumps(list(progress.failed_items)),
                    timestamp,
                    task_id,
                ),
            )
            if checkpoint is not None:
                reply_pages = checkpoint.get("reply_pages", checkpoint.get("replies", {}))
                root_cursor = checkpoint.get("root_cursor")
                connection.execute(
                    """
                    UPDATE task_checkpoints
                    SET root_page = ?, replies_json = ?, complete = ?, root_cursor = ?,
                        requested_pages = ?, declared_comments = ?, declared_total = ?,
                        declared_reply_counts_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        int(checkpoint.get("root_page", 1)),
                        json.dumps(reply_pages, sort_keys=True),
                        int(bool(checkpoint.get("complete", False))),
                        normalize_cursor(root_cursor),
                        int(checkpoint.get("requested_pages", 0)),
                        int(checkpoint.get("declared_comments", 0)),
                        (
                            int(checkpoint["declared_total"])
                            if checkpoint.get("declared_total") is not None
                            else None
                        ),
                        json.dumps(
                            checkpoint.get("declared_reply_counts", {}), sort_keys=True
                        ),
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
            return {
                "root_cursor": None,
                "requested_pages": 0,
                "declared_comments": 0,
                "declared_total": None,
                "declared_reply_counts": {},
                "root_page": 1,
                "replies": {},
                "complete": False,
            }
        return {
            "root_cursor": normalize_cursor(row["root_cursor"]),
            "requested_pages": int(row["requested_pages"]),
            "declared_comments": int(row["declared_comments"]),
            "declared_total": (
                int(row["declared_total"]) if row["declared_total"] is not None else None
            ),
            "declared_reply_counts": json.loads(row["declared_reply_counts_json"]),
            "root_page": int(row["root_page"]),
            "replies": json.loads(row["replies_json"]),
            "complete": bool(row["complete"]),
        }

    @classmethod
    def video_id_from_url(cls, video_url: str) -> str:
        parsed = urlparse(video_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or hostname not in cls.SUPPORTED_VIDEO_HOSTS:
            raise UnsupportedVideoError(
                "Only ordinary HTTPS Bilibili video URLs are supported"
            )
        match = cls.VIDEO_ID_PATTERN.fullmatch(parsed.path)
        if match is None:
            raise UnsupportedVideoError(
                "Only ordinary Bilibili video URLs containing a BV identifier are supported"
            )
        value = match.group(1)
        return f"BV{value[2:]}"

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
                declared_total=(
                    int(row["declared_total"]) if row["declared_total"] is not None else None
                ),
                coverage=float(row["coverage"]),
                failed_items=tuple(json.loads(row["failed_items_json"])),
            ),
            title_source=row["title_source"] or "legacy",
            profile_id=row["profile_id"] or DEFAULT_FILTER_PROFILE_ID,
        )


def _task_phase(status: TaskStatus) -> str:
    if status in {TaskStatus.COLLECTING, TaskStatus.ANALYZING}:
        return status.value
    return "task"
