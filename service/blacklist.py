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
    NETWORK = "network"
    AUTH = "auth"
    CAPTCHA = "captcha"
    INTERCEPTED = "intercepted"
    BLOCKED = "blocked"
    ENVIRONMENT = "environment"
    UNKNOWN = "unknown"


class BlacklistErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    CAPTCHA_OR_RISK = "captcha_or_risk"
    PAGE_STRUCTURE = "page_structure"
    PLATFORM_INTERCEPTION = "platform_interception"
    NETWORK = "network"
    BROWSER_ENVIRONMENT = "browser_environment"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BlacklistFailureDiagnostic:
    error_category: BlacklistErrorCategory
    failure_type: ExecutionFailureKind
    user_message: str
    recovery_action: str


_FAILURE_DIAGNOSTICS: dict[ExecutionFailureKind, BlacklistFailureDiagnostic] = {
    ExecutionFailureKind.TEMPORARY: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.NETWORK,
        failure_type=ExecutionFailureKind.TEMPORARY,
        user_message="临时网络错误，拉黑操作失败",
        recovery_action="网络恢复后请点击“重试”",
    ),
    ExecutionFailureKind.NETWORK: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.NETWORK,
        failure_type=ExecutionFailureKind.NETWORK,
        user_message="临时网络错误，拉黑操作失败",
        recovery_action="网络恢复后请点击“重试”",
    ),
    ExecutionFailureKind.AUTH: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.AUTHENTICATION,
        failure_type=ExecutionFailureKind.AUTH,
        user_message="B 站登录状态失效，队列已暂停",
        recovery_action="请重新同步 B 站登录状态后点击“恢复”",
    ),
    ExecutionFailureKind.CAPTCHA: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.CAPTCHA_OR_RISK,
        failure_type=ExecutionFailureKind.CAPTCHA,
        user_message="检测到验证码或风控验证，队列已暂停",
        recovery_action="请完成验证码或风控验证后点击“恢复”",
    ),
    ExecutionFailureKind.INTERCEPTED: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.PAGE_STRUCTURE,
        failure_type=ExecutionFailureKind.INTERCEPTED,
        user_message="确认窗口结构未识别，队列已暂停",
        recovery_action="请检查 B 站页面结构后点击“恢复”",
    ),
    ExecutionFailureKind.BLOCKED: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.PLATFORM_INTERCEPTION,
        failure_type=ExecutionFailureKind.BLOCKED,
        user_message="检测到 B 站平台拦截，队列已暂停",
        recovery_action="请等待平台限制解除后点击“恢复”",
    ),
    ExecutionFailureKind.ENVIRONMENT: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.BROWSER_ENVIRONMENT,
        failure_type=ExecutionFailureKind.ENVIRONMENT,
        user_message="浏览器执行环境故障，拉黑操作失败",
        recovery_action="请检查后台 Chromium 运行环境后点击“重试”",
    ),
    ExecutionFailureKind.UNKNOWN: BlacklistFailureDiagnostic(
        error_category=BlacklistErrorCategory.UNKNOWN,
        failure_type=ExecutionFailureKind.UNKNOWN,
        user_message="拉黑操作遇到未识别错误，队列项已保留",
        recovery_action="请查看技术详情后点击“重试”",
    ),
}


def failure_diagnostic(kind: ExecutionFailureKind) -> BlacklistFailureDiagnostic:
    return _FAILURE_DIAGNOSTICS.get(kind, _FAILURE_DIAGNOSTICS[ExecutionFailureKind.UNKNOWN])


def failure_status(kind: ExecutionFailureKind) -> BlacklistQueueStatus:
    if kind in {
        ExecutionFailureKind.AUTH,
        ExecutionFailureKind.CAPTCHA,
        ExecutionFailureKind.INTERCEPTED,
        ExecutionFailureKind.BLOCKED,
    }:
        return BlacklistQueueStatus.PAUSED
    return BlacklistQueueStatus.FAILED


def classify_unexpected_failure(error: BaseException) -> ExecutionFailureKind:
    text = f"{error.__class__.__name__}: {error}".lower()
    if any(marker in text for marker in ("timeout", "timed out", "connection", "network", "dns")):
        return ExecutionFailureKind.NETWORK
    if any(
        marker in text
        for marker in (
            "playwright",
            "browser",
            "chromium",
            "executable",
            "target closed",
            "context closed",
        )
    ):
        return ExecutionFailureKind.ENVIRONMENT
    return ExecutionFailureKind.UNKNOWN


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
    error_category: BlacklistErrorCategory | None = None
    failure_type: ExecutionFailureKind | None = None
    user_message: str | None = None
    recovery_action: str | None = None
    error_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionResult:
    success: bool = True
    detail: str = "Blacklist action completed"
    failure_kind: ExecutionFailureKind | None = None


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

    _LOCATOR_TIMEOUT_MS = 10_000

    _RISK_VERIFICATION_MARKERS = (
        "验证码",
        "人机验证",
        "风险验证",
        "风控",
    )

    _PLATFORM_INTERCEPTION_MARKERS = (
        "安全验证",
        "请求过频",
        "请求过于频繁",
        "操作频繁",
        "访问受限",
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
                ExecutionFailureKind.ENVIRONMENT,
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
                    self._raise_if_page_failure(page)
                    if page.get_by_text("已拉黑", exact=True).count() > 0:
                        return ExecutionResult(detail="UID is already blacklisted")
                    more_actions = page.locator(".more-actions__trigger")
                    self._wait_for_visible(
                        page,
                        more_actions,
                        kind=ExecutionFailureKind.INTERCEPTED,
                        detail="Native blacklist control was not found",
                    )
                    more_actions.click()
                    blacklist_action = page.get_by_text("加入黑名单", exact=True)
                    self._wait_for_visible(
                        page,
                        blacklist_action,
                        kind=ExecutionFailureKind.INTERCEPTED,
                        detail="Native blacklist menu action was not found",
                    )
                    blacklist_action.click()
                    confirm = page.get_by_text("确定", exact=True)
                    self._wait_for_visible(
                        page,
                        confirm,
                        kind=ExecutionFailureKind.INTERCEPTED,
                        detail="Native blacklist confirmation control was not found",
                    )
                    confirm.click()
                    success = page.get_by_text("已拉黑", exact=True)
                    self._wait_for_visible(
                        page,
                        success,
                        kind=ExecutionFailureKind.INTERCEPTED,
                        detail="Native blacklist success state was not confirmed",
                    )
                    if success.count() == 0:
                        raise BlacklistExecutionError(
                            ExecutionFailureKind.INTERCEPTED,
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
            kind = classify_unexpected_failure(exc)
            raise BlacklistExecutionError(
                kind,
                f"Native blacklist action failed: {exc}",
            ) from exc

    def _raise_if_page_failure(self, page: object) -> None:
        if page.get_by_text("登录", exact=True).count() > 0:
            raise BlacklistExecutionError(
                ExecutionFailureKind.AUTH,
                "Bilibili session is no longer logged in",
            )
        if self._risk_verification_detected(page):
            raise BlacklistExecutionError(
                ExecutionFailureKind.CAPTCHA,
                "Bilibili requested a captcha; queue paused",
            )
        if self._platform_interception_detected(page):
            raise BlacklistExecutionError(
                ExecutionFailureKind.BLOCKED,
                "Bilibili blocked the native blacklist action; queue paused",
            )

    def _wait_for_visible(
        self,
        page: object,
        locator: object,
        *,
        kind: ExecutionFailureKind,
        detail: str,
    ) -> None:
        try:
            locator.wait_for(state="visible", timeout=self._LOCATOR_TIMEOUT_MS)
        except Exception as exc:
            self._raise_if_page_failure(page)
            raise BlacklistExecutionError(kind, detail) from exc
        self._raise_if_page_failure(page)

    def _risk_verification_detected(self, page: object) -> bool:
        return any(
            page.get_by_text(marker, exact=False).count() > 0
            for marker in self._RISK_VERIFICATION_MARKERS
        )

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
        item = self._claim_next()
        if item is None:
            return None
        try:
            outcome = executor.execute(item)
            if not outcome.success:
                kind = outcome.failure_kind or ExecutionFailureKind.UNKNOWN
                return self._finish_failure(item.item_id, kind, outcome.detail)
        except BlacklistExecutionError as exc:
            return self._finish_failure(item.item_id, exc.kind, exc.detail)
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            return self._finish_failure(
                item.item_id,
                classify_unexpected_failure(exc),
                f"Blacklist executor failed: {detail}",
            )
        completed = self._finish(item.item_id, BlacklistQueueStatus.COMPLETED, None)
        if self.registry is not None:
            try:
                current = self.registry.get(completed.uid)
                if current.state is UidState.QUEUED:
                    self.registry.update(uid=completed.uid, state=UidState.BLOCKED)
            except (LookupError, ValueError):
                pass
        return completed

    def _claim_next(self) -> BlacklistItem | None:
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                UPDATE blacklist_queue
                SET status = ?, attempts = attempts + 1, updated_at = ?, last_error = NULL,
                    error_category = NULL, failure_type = NULL,
                    user_message = NULL, recovery_action = NULL, error_at = NULL
                WHERE item_id = (
                    SELECT item_id
                    FROM blacklist_queue
                    WHERE status = ?
                    ORDER BY created_at, item_id
                    LIMIT 1
                )
                  AND status = ?
                RETURNING *
                """,
                (
                    BlacklistQueueStatus.PROCESSING.value,
                    timestamp,
                    BlacklistQueueStatus.QUEUED.value,
                    BlacklistQueueStatus.QUEUED.value,
                ),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _finish(
        self,
        item_id: str,
        status: BlacklistQueueStatus,
        error: str | None,
        diagnostic: BlacklistFailureDiagnostic | None = None,
    ) -> BlacklistItem:
        timestamp = datetime.now(UTC).isoformat()
        completed_at = timestamp if status is BlacklistQueueStatus.COMPLETED else None
        error_at = timestamp if diagnostic is not None else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE blacklist_queue
                SET status = ?, updated_at = ?, last_error = ?,
                    error_category = ?, failure_type = ?, user_message = ?,
                    recovery_action = ?, error_at = ?, completed_at = ?
                WHERE item_id = ?
                """,
                (
                    status.value,
                    timestamp,
                    error,
                    diagnostic.error_category.value if diagnostic else None,
                    diagnostic.failure_type.value if diagnostic else None,
                    diagnostic.user_message if diagnostic else None,
                    diagnostic.recovery_action if diagnostic else None,
                    error_at,
                    completed_at,
                    item_id,
                ),
            )
        return self.get(item_id)

    def _finish_failure(
        self, item_id: str, kind: ExecutionFailureKind, error: str
    ) -> BlacklistItem:
        return self._finish(
            item_id,
            failure_status(kind),
            error,
            failure_diagnostic(kind),
        )

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
            if target is BlacklistQueueStatus.QUEUED:
                connection.execute(
                    """
                    UPDATE blacklist_queue
                    SET status = ?, updated_at = ?, last_error = NULL,
                        error_category = NULL, failure_type = NULL,
                        user_message = NULL, recovery_action = NULL, error_at = NULL
                    WHERE item_id = ?
                    """,
                    (target.value, timestamp, item_id),
                )
            else:
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
            error_category=(
                BlacklistErrorCategory(row["error_category"])
                if row["error_category"]
                else None
            ),
            failure_type=(
                ExecutionFailureKind(row["failure_type"])
                if row["failure_type"]
                else None
            ),
            user_message=row["user_message"],
            recovery_action=row["recovery_action"],
            error_at=(datetime.fromisoformat(row["error_at"]) if row["error_at"] else None),
        )
