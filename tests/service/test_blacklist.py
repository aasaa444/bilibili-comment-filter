import sys
import threading
import types
from datetime import UTC, datetime

import pytest

from service.blacklist import (
    BlacklistExecutionError,
    BlacklistItem,
    BlacklistQueueService,
    BlacklistQueueStatus,
    ExecutionFailureKind,
    ExecutionResult,
    PlaywrightBlacklistExecutor,
)
from service.db import Database
from service.models import UidState
from service.registry import UidRegistry


class FakeLocator:
    def __init__(self, page: "FakePage", text: str, count: int) -> None:
        self._page = page
        self._text = text
        self._count = count

    def count(self) -> int:
        return self._count

    def click(self) -> None:
        self._page.clicked.append(self._text)
        if self._text == ".more-actions__trigger":
            self._page.menu_open = True
        elif self._text == "加入黑名单":
            self._page.dialog_open = True
        elif self._text == "确定":
            self._page.confirmed = True
            if self._page.success_after_confirmation:
                self._page.blacklisted = True

    def wait_for(self, *, state: str, timeout: int) -> None:
        self._page.waited_for.append((self._text, state, timeout))
        if self.count() == 0:
            raise TimeoutError(f"Fake locator {self._text!r} did not become visible")


class FakePage:
    def __init__(
        self,
        *,
        logged_out: bool = False,
        captcha: bool = False,
        platform_intercepted: str | None = None,
        blacklist_control: bool = True,
        blacklist_menu_control: bool = True,
        already_blacklisted: bool = False,
        success_after_confirmation: bool = True,
    ) -> None:
        self.logged_out = logged_out
        self.captcha = captcha
        self.platform_intercepted = platform_intercepted
        self.blacklist_control = blacklist_control
        self.blacklist_menu_control = blacklist_menu_control
        self.blacklisted = already_blacklisted
        self.success_after_confirmation = success_after_confirmation
        self.menu_open = False
        self.dialog_open = False
        self.confirmed = False
        self.clicked: list[str] = []
        self.waited_for: list[tuple[str, str, int]] = []
        self.load_states: list[str] = []

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        visible = (
            selector == ".more-actions__trigger"
            and self.blacklist_control
            and not self.blacklisted
        )
        return FakeLocator(self, selector, int(visible))

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        visible = False
        if text == "登录" and exact:
            visible = self.logged_out
        elif text == "验证码" and not exact:
            visible = self.captcha
        elif text == self.platform_intercepted and not exact:
            visible = True
        elif text == "加入黑名单" and exact:
            visible = (
                self.menu_open
                and self.blacklist_menu_control
                and not self.blacklisted
            )
        elif text == "已拉黑" and exact:
            visible = self.blacklisted
        elif text == "确定" and exact:
            visible = self.dialog_open
        return FakeLocator(self, text, int(visible))

    def wait_for_load_state(self, state: str, *_args, **_kwargs) -> None:
        self.load_states.append(state)
        if state == "networkidle":
            raise AssertionError("blacklist detection must not wait for networkidle")
        return None


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.cookies: list[dict[str, object]] = []

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies.extend(cookies)

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.context = FakeContext(page)
        self.closed = False
        self.launch_kwargs: dict[str, object] = {}

    def new_context(self, **_kwargs) -> FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.chromium = self

    def launch(self, **_kwargs) -> FakeBrowser:
        self.browser.launch_kwargs = _kwargs
        return self.browser


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, *_args) -> None:
        return None


def install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch, browser: FakeBrowser
) -> None:
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywrightManager(FakePlaywright(browser))
    playwright_package = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", playwright_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def make_item() -> BlacklistItem:
    now = datetime.now(UTC)
    return BlacklistItem(
        item_id="item-1",
        uid="1001",
        evidence_id=None,
        status="processing",
        attempts=1,
        last_error=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def assert_resources_closed(browser: FakeBrowser) -> None:
    assert browser.context.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("configured_headless", "expected_headless"),
    [("true", True), ("false", False)],
)
def test_native_executor_confirms_blacklist_and_uses_configured_headless_mode(
    monkeypatch: pytest.MonkeyPatch,
    configured_headless: str,
    expected_headless: bool,
) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_BROWSER_HEADLESS", configured_headless)
    page = FakePage()
    browser = FakeBrowser(page)
    install_fake_playwright(monkeypatch, browser)

    result = PlaywrightBlacklistExecutor().execute(make_item())

    assert result.success is True
    assert page.clicked == [".more-actions__trigger", "加入黑名单", "确定"]
    assert len(page.waited_for) == 4
    assert all(
        state == "visible" and timeout == 10_000
        for _, state, timeout in page.waited_for
    )
    assert page.load_states == []
    assert browser.launch_kwargs["headless"] is expected_headless
    assert_resources_closed(browser)


def test_native_executor_injects_the_latest_cookie_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage())
    install_fake_playwright(monkeypatch, browser)

    result = PlaywrightBlacklistExecutor(
        cookies_provider=lambda: {"SESSDATA": "fixture-session", "bili_jct": "fixture-csrf"}
    ).execute(make_item())

    assert result.success is True
    assert browser.context.cookies == [
        {
            "name": "SESSDATA",
            "value": "fixture-session",
            "domain": ".bilibili.com",
            "path": "/",
        },
        {
            "name": "bili_jct",
            "value": "fixture-csrf",
            "domain": ".bilibili.com",
            "path": "/",
        },
    ]
    assert_resources_closed(browser)


def test_native_executor_does_not_report_success_without_blacklist_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(success_after_confirmation=False)
    browser = FakeBrowser(page)
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.INTERCEPTED
    assert_resources_closed(browser)


def test_native_executor_closes_browser_when_auth_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage(logged_out=True))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.AUTH
    assert_resources_closed(browser)


def test_native_executor_pauses_for_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = FakeBrowser(FakePage(captcha=True))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.CAPTCHA
    assert_resources_closed(browser)


@pytest.mark.parametrize("marker", ["风控"])
def test_native_executor_classifies_risk_verification_as_captcha(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    browser = FakeBrowser(FakePage(platform_intercepted=marker))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.CAPTCHA
    assert_resources_closed(browser)


def test_native_executor_classifies_missing_blacklist_control_as_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage(blacklist_control=False))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.INTERCEPTED
    assert_resources_closed(browser)


def test_native_executor_classifies_missing_blacklist_menu_action_as_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage(blacklist_menu_control=False))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.INTERCEPTED
    assert_resources_closed(browser)


@pytest.mark.parametrize("marker", ["安全验证", "请求过于频繁", "平台拦截"])
def test_native_executor_classifies_platform_interception_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    browser = FakeBrowser(FakePage(platform_intercepted=marker))
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.BLOCKED
    assert_resources_closed(browser)


def test_blacklist_queue_pauses_blocked_action_and_can_resume() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        registry = UidRegistry(database)
        registry.add(uid="1001", nickname="blocked", state=UidState.QUEUED)
        queue = BlacklistQueueService(database, registry)
        item, _ = queue.enqueue(uid="1001")

        class BlockedExecutor:
            def execute(self, _item: BlacklistItem) -> ExecutionResult:
                raise BlacklistExecutionError(
                    ExecutionFailureKind.BLOCKED, "fixture platform interception"
                )

        result = queue.process_next(BlockedExecutor())

        assert result is not None
        assert result.status is BlacklistQueueStatus.PAUSED
        assert queue.resume(item.item_id).status is BlacklistQueueStatus.QUEUED
    finally:
        database.close()


def test_cancelled_blacklist_item_is_not_requeued_by_later_detection() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        item, created = queue.enqueue(uid="1001")
        assert created is True
        assert queue.cancel_for_uid("1001") is not None

        retried, recreated = queue.enqueue(uid="1001", evidence_id="evidence-2")

        assert recreated is False
        assert retried.item_id == item.item_id
        assert retried.status is BlacklistQueueStatus.CANCELLED
        assert retried.evidence_id is None
    finally:
        database.close()


def test_blacklist_queue_finishes_unexpected_executor_exception_as_failed() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        item, _ = queue.enqueue(uid="1002")

        class ExplodingExecutor:
            def execute(self, _item: BlacklistItem) -> ExecutionResult:
                raise RuntimeError("fixture executor failure")

        result = queue.process_next(ExplodingExecutor())

        assert result is not None
        assert result.status is BlacklistQueueStatus.FAILED
        assert result.last_error == "Blacklist executor failed: fixture executor failure"
        assert queue.get(item.item_id).status is BlacklistQueueStatus.FAILED
    finally:
        database.close()


def test_blacklist_queue_claim_is_atomic_across_concurrent_processors() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        item, _ = queue.enqueue(uid="1003")
        started = threading.Event()
        release = threading.Event()
        first_result: list[object] = []

        class BlockingExecutor:
            def execute(self, _item: BlacklistItem) -> ExecutionResult:
                started.set()
                assert release.wait(timeout=2)
                return ExecutionResult(detail="fixture complete")

        def process_first() -> None:
            first_result.append(queue.process_next(BlockingExecutor()))

        first = threading.Thread(target=process_first)
        first.start()
        assert started.wait(timeout=2)

        second = queue.process_next(BlockingExecutor())
        release.set()
        first.join(timeout=2)

        assert second is None
        assert len(first_result) == 1
        assert first_result[0].item_id == item.item_id
        assert queue.get(item.item_id).status is BlacklistQueueStatus.COMPLETED
    finally:
        database.close()


def test_database_recovers_processing_blacklist_items_after_restart(tmp_path) -> None:
    db_path = tmp_path / "queue-recovery.sqlite3"
    database = Database(db_path)
    database.initialize()
    queue = BlacklistQueueService(database)
    item, _ = queue.enqueue(uid="1004")
    claimed = queue._claim_next()
    assert claimed is not None
    assert claimed.status is BlacklistQueueStatus.PROCESSING
    database.close()

    restarted = Database(db_path)
    restarted.initialize()
    try:
        recovered = BlacklistQueueService(restarted).get(item.item_id)
        assert recovered.status is BlacklistQueueStatus.FAILED
        assert recovered.last_error == (
            "Recovered abandoned blacklist item after service restart"
        )
    finally:
        restarted.close()
