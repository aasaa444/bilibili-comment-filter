from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from service.blacklist import (
    BlacklistExecutionError,
    BlacklistQueueService,
    BlacklistQueueStatus,
    ExecutionFailureKind,
)
from service.db import Database
from service.worker import BackgroundWorker, WorkerConfig


@pytest.mark.parametrize(
    ("kind", "status", "category", "message", "recovery"),
    [
        (
            ExecutionFailureKind.INTERCEPTED,
            BlacklistQueueStatus.PAUSED,
            "page_structure",
            "确认窗口结构未识别，队列已暂停",
            "请检查 B 站页面结构后点击“恢复”",
        ),
        (
            ExecutionFailureKind.AUTH,
            BlacklistQueueStatus.PAUSED,
            "authentication",
            "B 站登录状态失效，队列已暂停",
            "请重新同步 B 站登录状态后点击“恢复”",
        ),
        (
            ExecutionFailureKind.CAPTCHA,
            BlacklistQueueStatus.PAUSED,
            "captcha_or_risk",
            "检测到验证码或风控验证，队列已暂停",
            "请完成验证码或风控验证后点击“恢复”",
        ),
        (
            ExecutionFailureKind.BLOCKED,
            BlacklistQueueStatus.PAUSED,
            "platform_interception",
            "检测到 B 站平台拦截，队列已暂停",
            "请等待平台限制解除后点击“恢复”",
        ),
        (
            ExecutionFailureKind.TEMPORARY,
            BlacklistQueueStatus.FAILED,
            "network",
            "临时网络错误，拉黑操作失败",
            "稍后自动重试，或点击“重试”",
        ),
        (
            ExecutionFailureKind.ENVIRONMENT,
            BlacklistQueueStatus.FAILED,
            "browser_environment",
            "浏览器执行环境故障，拉黑操作失败",
            "请检查后台 Chromium 运行环境后点击“重试”",
        ),
        (
            ExecutionFailureKind.UNKNOWN,
            BlacklistQueueStatus.FAILED,
            "unknown",
            "拉黑操作遇到未识别错误，队列项已保留",
            "请查看技术详情后点击“重试”",
        ),
    ],
)
def test_queue_persists_user_diagnostic_and_raw_error(
    kind: ExecutionFailureKind,
    status: BlacklistQueueStatus,
    category: str,
    message: str,
    recovery: str,
) -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        item, _ = queue.enqueue(uid="9001")
        raw_error = "selector=.confirm; fixture failure"

        class FixedExecutor:
            def execute(self, _item):
                raise BlacklistExecutionError(kind, raw_error)

        processed = queue.process_next(FixedExecutor())

        assert processed is not None
        assert processed.status is status
        assert processed.last_error == raw_error
        assert processed.error_category is not None
        assert processed.error_category.value == category
        assert processed.failure_type is kind
        assert processed.user_message == message
        assert processed.recovery_action == recovery
        assert processed.error_at is not None
        assert queue.get(item.item_id).error_at == processed.error_at
    finally:
        database.close()


def test_unknown_executor_exception_gets_readable_fallback() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        queue.enqueue(uid="9002")

        class UnknownExecutor:
            def execute(self, _item):
                raise RuntimeError("fixture executor exploded")

        processed = queue.process_next(UnknownExecutor())

        assert processed is not None
        assert processed.status is BlacklistQueueStatus.FAILED
        assert processed.error_category.value == "unknown"
        assert processed.failure_type is ExecutionFailureKind.UNKNOWN
        assert processed.user_message == "拉黑操作遇到未识别错误，队列项已保留"
        assert processed.last_error == "Blacklist executor failed: fixture executor exploded"
    finally:
        database.close()


def test_legacy_failed_queue_item_is_not_retried_automatically() -> None:
    retry_calls: list[str] = []

    class LegacyQueue:
        def list(self):
            return (
                SimpleNamespace(
                    item_id="legacy-item",
                    status=BlacklistQueueStatus.FAILED,
                    error_category=None,
                    attempts=0,
                ),
            )

        def retry(self, item_id: str):
            retry_calls.append(item_id)

    worker = BackgroundWorker(
        task_store=object(),
        orchestrator=object(),
        queue=LegacyQueue(),
        executor=object(),
        config=WorkerConfig(queue_retry_delay=0),
    )

    assert worker._retry_ready_queue() is False
    assert retry_calls == []


def test_recovered_processing_item_gets_environment_diagnostic() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        queue = BlacklistQueueService(database)
        item, _ = queue.enqueue(uid="9003")
        with database.transaction() as connection:
            connection.execute(
                "UPDATE blacklist_queue SET status = 'processing' WHERE item_id = ?",
                (item.item_id,),
            )

        database.recover_blacklist_processing()
        recovered = queue.get(item.item_id)

        assert recovered.status is BlacklistQueueStatus.FAILED
        assert recovered.error_category is not None
        assert recovered.error_category.value == "browser_environment"
        assert recovered.failure_type is ExecutionFailureKind.ENVIRONMENT
        assert recovered.user_message == "服务重启时发现上次拉黑中断，队列项已保留"
    finally:
        database.close()


def test_blacklist_api_keeps_raw_error_inside_diagnostic_fields() -> None:
    app = create_app(db_path=":memory:", start_background_worker=False)

    class FixedExecutor:
        def execute(self, _item):
            raise BlacklistExecutionError(
                ExecutionFailureKind.INTERCEPTED,
                "selector=.more-actions__trigger",
            )

    app.state.blacklist_executor = FixedExecutor()
    with TestClient(app) as client:
        item, _ = app.state.blacklist_queue.enqueue(uid="9003")
        response = client.post("/api/blacklist/process")

        assert response.status_code == 200
        payload = response.json()
        assert payload["item_id"] == item.item_id
        assert payload["status"] == "paused"
        assert payload["user_message"] == "确认窗口结构未识别，队列已暂停"
        assert payload["recovery_action"] == "请检查 B 站页面结构后点击“恢复”"
        assert payload["error_category"] == "page_structure"
        assert payload["failure_type"] == "intercepted"
        assert payload["last_error"] == "selector=.more-actions__trigger"
        assert payload["error_at"]


def test_existing_queue_schema_receives_diagnostic_columns(tmp_path) -> None:
    path = tmp_path / "legacy-queue.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE blacklist_queue (
            item_id TEXT PRIMARY KEY,
            uid TEXT NOT NULL UNIQUE,
            evidence_id TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    try:
        columns = {
            row["name"] for row in database.execute("PRAGMA table_info(blacklist_queue)")
        }
        assert {
            "error_category",
            "failure_type",
            "user_message",
            "recovery_action",
            "error_at",
        }.issubset(columns)
    finally:
        database.close()
