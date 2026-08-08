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
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    def goto(self, *_args, **_kwargs) -> None:
        return None

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        return FakeLocator(1 if text == "登录" and exact else 0)


class FakeContext:
    def new_page(self) -> FakePage:
        return FakePage()


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def new_context(self, **_kwargs) -> FakeContext:
        return FakeContext()

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.chromium = self

    def launch(self, **_kwargs) -> FakeBrowser:
        return self.browser


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, *_args) -> None:
        return None


def test_native_executor_closes_browser_when_auth_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywrightManager(FakePlaywright(browser))
    playwright_package = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", playwright_package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    item = BlacklistItem(
        item_id="item-1",
        uid="1001",
        evidence_id=None,
        status="processing",
        attempts=1,
        last_error=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        completed_at=None,
    )

    with pytest.raises(BlacklistExecutionError) as raised:
        PlaywrightBlacklistExecutor().execute(item)

    assert raised.value.kind is ExecutionFailureKind.AUTH
    assert browser.closed is True
