import sys
import types
from datetime import UTC, datetime

import pytest

from service.blacklist import (
    BlacklistExecutionError,
    BlacklistItem,
    ExecutionFailureKind,
    PlaywrightBlacklistExecutor,
)


class FakeLocator:
    def __init__(self, page: "FakePage", text: str, count: int) -> None:
        self._page = page
        self._text = text
        self._count = count

    def count(self) -> int:
        return self._count

    def click(self) -> None:
        self._page.clicked.append(self._text)
        if self._text == "拉黑":
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
        already_blacklisted: bool = False,
        success_after_confirmation: bool = True,
    ) -> None:
        self.logged_out = logged_out
        self.captcha = captcha
        self.platform_intercepted = platform_intercepted
        self.blacklist_control = blacklist_control
        self.blacklisted = already_blacklisted
        self.success_after_confirmation = success_after_confirmation
        self.dialog_open = False
        self.confirmed = False
        self.clicked: list[str] = []
        self.waited_for: list[tuple[str, str, int]] = []

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        visible = False
        if text == "登录" and exact:
            visible = self.logged_out
        elif text == "验证码" and not exact:
            visible = self.captcha
        elif text == self.platform_intercepted and not exact:
            visible = True
        elif text == "拉黑" and exact:
            visible = self.blacklist_control and not self.blacklisted
        elif text == "已拉黑" and exact:
            visible = self.blacklisted
        elif text == "确定" and exact:
            visible = self.dialog_open
        return FakeLocator(self, text, int(visible))

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
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
    assert page.clicked == ["拉黑", "确定"]
    assert page.waited_for == [("已拉黑", "visible", 10_000)]
    assert browser.launch_kwargs["headless"] is expected_headless
    assert_resources_closed(browser)


def test_native_executor_does_not_report_success_without_blacklist_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(success_after_confirmation=False)
    browser = FakeBrowser(page)
    install_fake_playwright(monkeypatch, browser)

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(make_item())

    assert raised.value.kind is ExecutionFailureKind.TEMPORARY
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


def test_native_executor_classifies_missing_blacklist_control_as_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser(FakePage(blacklist_control=False))
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
