from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .db import Database
from .models import UidState
from .registry import UidRegistry


class BlacklistQueueStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class ExecutionFailureKind(StrEnum):
    TEMPORARY = "temporary"
    AUTH = "auth"
    CAPTCHA = "captcha"
    INTERCEPTED = "intercepted"
    BLOCKED = "blocked"


class BlacklistExecutionError(RuntimeError):
    def __init__(self, kind: ExecutionFailureKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class BlacklistItem:
    item_id: str
    uid: str
    evidence_id: str | None
    status: BlacklistQueueStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class ExecutionResult:
    success: bool = True
    detail: str = "Blacklist action completed"


class BlacklistExecutor(Protocol):
    def execute(self, item: BlacklistItem) -> ExecutionResult:
        """Perform the visible native Bilibili action at the browser boundary."""


class RecordingBlacklistExecutor:
    """Test-only executor that records UIDs and never opens a browser."""

    def __init__(self) -> None:
        self.uids: list[str] = []

    def execute(self, item: BlacklistItem) -> ExecutionResult:
        self.uids.append(item.uid)
        return ExecutionResult()


class PlaywrightBlacklistExecutor:
    """Production browser boundary; selectors are deliberately configuration-driven."""

    _PLATFORM_INTERCEPTION_MARKERS = (
        "安全验证",
        "请求过频",
        "请求过于频繁",
        "操作频繁",
        "访问受限",
        "风控",
        "平台拦截",
    )

    def __init__(
        self,
        *,
        base_url: str = "https://space.bilibili.com",
        cookies_provider: Callable[[], dict[str, str] | None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies_provider = cookies_provider

    def execute(self, item: BlacklistItem) -> ExecutionResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BlacklistExecutionError(
                ExecutionFailureKind.TEMPORARY,
                "Playwright is not installed for the native blacklist executor",
            ) from exc
        try:
            with sync_playwright() as playwright:
                browser = None
                context = None
                try:
                    browser = playwright.chromium.launch(headless=_headless_mode())
                    context = browser.new_context(
                        locale="zh-CN",
                        viewport={"width": 1280, "height": 900},
                    )
                    cookies = self._browser_cookies()
                    if cookies:
                        context.add_cookies(cookies)
                    page = context.new_page()
                    page.goto(f"{self.base_url}/{item.uid}", wait_until="domcontentloaded")
                    if page.get_by_text("登录", exact=True).count() > 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.AUTH,
                            "Bilibili session is no longer logged in",
                        )
                    page.wait_for_load_state("networkidle", timeout=10_000)
                    if page.get_by_text("验证码", exact=False).count() > 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.CAPTCHA,
                            "Bilibili requested a captcha; queue paused",
                        )
                    if self._platform_interception_detected(page):
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.BLOCKED,
                            "Bilibili blocked the native blacklist action; queue blocked",
                        )
                    if page.get_by_text("已拉黑", exact=True).count() > 0:
                        return ExecutionResult(detail="UID is already blacklisted")
                    button = page.get_by_text("拉黑", exact=True)
                    if button.count() == 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.INTERCEPTED,
                            "Native blacklist control was not found",
                        )
                    button.click()
                    confirm = page.get_by_text("确定", exact=True)
                    if confirm.count() == 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.INTERCEPTED,
                            "Native blacklist confirmation control was not found",
                        )
                    confirm.click()
                    page.wait_for_load_state("networkidle", timeout=10_000)
                    if self._platform_interception_detected(page):
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.BLOCKED,
                            "Bilibili blocked the native blacklist action; queue blocked",
                        )
                    success = page.get_by_text("已拉黑", exact=True)
                    try:
                        success.wait_for(state="visible", timeout=10_000)
                    except Exception as exc:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.TEMPORARY,
                            "Native blacklist success state was not confirmed",
                        ) from exc
                    if success.count() == 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.TEMPORARY,
                            "Native blacklist success state was not confirmed",
                        )
                    return ExecutionResult(detail="UID is blacklisted")
                finally:
                    if context is not None:
                        try:
                            context.close()
                        finally:
                            if browser is not None:
                                browser.close()
                    elif browser is not None:
                        browser.close()
        except BlacklistExecutionError:
            raise
        except Exception as exc:
            raise BlacklistExecutionError(
                ExecutionFailureKind.TEMPORARY,
                f"Native blacklist action failed: {exc}",
            ) from exc

    def _platform_interception_detected(self, page: object) -> bool:
        return any(
            page.get_by_text(marker, exact=False).count() > 0
            for marker in self._PLATFORM_INTERCEPTION_MARKERS
        )

    def _browser_cookies(self) -> list[dict[str, object]]:
        if self.cookies_provider is None:
            return []
        cookies = self.cookies_provider() or {}
        return [
            {
                "name": name,
                "value": value,
                "domain": ".bilibili.com",
                "path": "/",
            }
            for name, value in cookies.items()
            if name and isinstance(value, str)
        ]


def _headless_mode() -> bool:
    value = os.getenv("BILIBILI_FILTER_BROWSER_HEADLESS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


class BlacklistQueueError(ValueError):
    pass


class BlacklistQueueService:
    def __init__(self, database: Database, registry: UidRegistry | None = None) -> None:
        self.database = database
        self.registry = registry

    def enqueue(self, *, uid: str, evidence_id: str | None = None) -> tuple[BlacklistItem, bool]:
        existing = self.database.execute(
            "SELECT * FROM blacklist_queue WHERE uid = ?", (uid,)
        ).fetchone()
        if existing is not None:
            return self._from_row(existing), False
        timestamp = datetime.now(UTC).isoformat()
        item_id = uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO blacklist_queue
                    (item_id, uid, evidence_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    uid,
                    evidence_id,
                    BlacklistQueueStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM blacklist_queue WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._from_row(row), True

    def get(self, item_id: str) -> BlacklistItem:
        row = self.database.execute(
            "SELECT * FROM blacklist_queue WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise BlacklistQueueError(f"Blacklist item {item_id} was not found")
        return self._from_row(row)

    def list(self) -> tuple[BlacklistItem, ...]:
        rows = self.database.execute(
            "SELECT * FROM blacklist_queue ORDER BY created_at, item_id"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def pause(self, item_id: str) -> BlacklistItem:
        return self._transition(
            item_id,
            BlacklistQueueStatus.PAUSED,
            {
                BlacklistQueueStatus.QUEUED,
                BlacklistQueueStatus.FAILED,
                BlacklistQueueStatus.BLOCKED,
            },
        )

    def resume(self, item_id: str) -> BlacklistItem:
        return self._transition(item_id, BlacklistQueueStatus.QUEUED, {BlacklistQueueStatus.PAUSED})

    def retry(self, item_id: str) -> BlacklistItem:
        return self._transition(
            item_id,
            BlacklistQueueStatus.QUEUED,
            {
                BlacklistQueueStatus.FAILED,
                BlacklistQueueStatus.BLOCKED,
                BlacklistQueueStatus.PAUSED,
            },
        )

    def cancel_for_uid(self, uid: str) -> BlacklistItem | None:
        row = self.database.execute(
            "SELECT * FROM blacklist_queue WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None:
            return None
        item = self._from_row(row)
        if item.status in {
            BlacklistQueueStatus.QUEUED,
            BlacklistQueueStatus.PAUSED,
            BlacklistQueueStatus.FAILED,
            BlacklistQueueStatus.BLOCKED,
        }:
            return self._transition(
                item.item_id,
                BlacklistQueueStatus.CANCELLED,
                {
                    BlacklistQueueStatus.QUEUED,
                    BlacklistQueueStatus.PAUSED,
                    BlacklistQueueStatus.FAILED,
                    BlacklistQueueStatus.BLOCKED,
                },
            )
        return item

    def process_next(self, executor: BlacklistExecutor) -> BlacklistItem | None:
        row = self.database.execute(
            "SELECT * FROM blacklist_queue WHERE status = ? ORDER BY created_at, item_id LIMIT 1",
            (BlacklistQueueStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None
        item = self._from_row(row)
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE blacklist_queue
                SET status = ?, attempts = attempts + 1, updated_at = ?, last_error = NULL
                WHERE item_id = ?
                """,
                (BlacklistQueueStatus.PROCESSING.value, timestamp, item.item_id),
            )
        item = self.get(item.item_id)
        try:
            outcome = executor.execute(item)
        except BlacklistExecutionError as exc:
            status = (
                BlacklistQueueStatus.FAILED
                if exc.kind is ExecutionFailureKind.TEMPORARY
                else BlacklistQueueStatus.PAUSED
                if exc.kind
                in {
                    ExecutionFailureKind.AUTH,
                    ExecutionFailureKind.CAPTCHA,
                    ExecutionFailureKind.INTERCEPTED,
                }
                else BlacklistQueueStatus.BLOCKED
            )
            return self._finish(item.item_id, status, exc.detail)
        if not outcome.success:
            return self._finish(item.item_id, BlacklistQueueStatus.FAILED, outcome.detail)
        completed = self._finish(item.item_id, BlacklistQueueStatus.COMPLETED, None)
        if self.registry is not None:
            try:
                current = self.registry.get(completed.uid)
                if current.state is UidState.QUEUED:
                    self.registry.update(uid=completed.uid, state=UidState.BLOCKED)
            except (LookupError, ValueError):
                pass
        return completed

    def _finish(
        self, item_id: str, status: BlacklistQueueStatus, error: str | None
    ) -> BlacklistItem:
        timestamp = datetime.now(UTC).isoformat()
        completed_at = timestamp if status is BlacklistQueueStatus.COMPLETED else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE blacklist_queue
                SET status = ?, updated_at = ?, last_error = ?, completed_at = ?
                WHERE item_id = ?
                """,
                (status.value, timestamp, error, completed_at, item_id),
            )
        return self.get(item_id)

    def _transition(
        self,
        item_id: str,
        target: BlacklistQueueStatus,
        allowed: set[BlacklistQueueStatus],
    ) -> BlacklistItem:
        current = self.get(item_id)
        if current.status not in allowed:
            raise BlacklistQueueError(
                f"Cannot transition blacklist item from {current.status.value} to {target.value}"
            )
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE blacklist_queue SET status = ?, updated_at = ? WHERE item_id = ?",
                (target.value, timestamp, item_id),
            )
        return self.get(item_id)

    @staticmethod
    def _from_row(row: object) -> BlacklistItem:
        return BlacklistItem(
            item_id=row["item_id"],
            uid=row["uid"],
            evidence_id=row["evidence_id"],
            status=BlacklistQueueStatus(row["status"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
        )
