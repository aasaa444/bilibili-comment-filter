import threading

from service.blacklist import (
    BlacklistExecutionError,
    BlacklistQueueService,
    BlacklistQueueStatus,
    ExecutionFailureKind,
)
from service.db import Database
from service.models import UidState
from service.registry import UidRegistry
from service.tasks import TaskStatus, TaskStore
from service.worker import BackgroundWorker, WorkerConfig


class EmptyQueue:
    def list(self):
        return ()

    def process_next(self, _executor):
        return None

    def retry(self, _item_id):
        raise AssertionError("empty queue should not be retried")


def test_background_worker_running_state_tracks_lifecycle() -> None:
    class EmptyTaskStore:
        def list(self):
            return ()

    worker = BackgroundWorker(
        task_store=EmptyTaskStore(),
        orchestrator=object(),
        queue=EmptyQueue(),
        executor=object(),
        config=WorkerConfig(poll_interval=0.01, queue_interval=60.0),
    )
    started = threading.Event()

    def fake_run_once(*, process_queue: bool) -> bool:
        started.set()
        worker._stop.wait(0.01)
        return False

    worker._run_once = fake_run_once

    assert worker.running is False
    assert worker.available is False
    worker.start()
    try:
        assert started.wait(1.0)
        assert worker.running is True
        assert worker.available is True
    finally:
        worker.stop(timeout=1.0)

    assert worker.running is False
    assert worker.available is False


def test_background_worker_throttles_blacklist_processing() -> None:
    worker = BackgroundWorker(
        task_store=object(),
        orchestrator=object(),
        queue=object(),
        executor=object(),
        config=WorkerConfig(poll_interval=0.0, queue_interval=60.0),
    )
    process_queue_flags: list[bool] = []

    def fake_run_once(*, process_queue: bool) -> bool:
        process_queue_flags.append(process_queue)
        if len(process_queue_flags) == 3:
            worker._stop.set()
        return False

    worker._run_once = fake_run_once
    worker.run_forever()

    assert process_queue_flags == [True, False, False]


def test_background_worker_retries_transient_partial_task_before_processing_queue() -> None:
    database = Database(":memory:")
    database.initialize()
    task_store = TaskStore(database)
    task, _ = task_store.create(video_url="https://www.bilibili.com/video/BV1workerretry")
    task_store.transition(task.task_id, TaskStatus.COLLECTING)
    task_store.transition(
        task.task_id,
        TaskStatus.PARTIAL,
        error_code="model_unavailable",
        error_message="fixture model is offline",
    )

    class CompletingOrchestrator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, task_id: str):
            self.calls.append(task_id)
            return type("Summary", (), {"status": TaskStatus.COMPLETED})()

    orchestrator = CompletingOrchestrator()
    worker = BackgroundWorker(
        task_store=task_store,
        orchestrator=orchestrator,
        queue=EmptyQueue(),
        executor=object(),
        config=WorkerConfig(poll_interval=0, queue_interval=0, task_retry_delay=0),
    )

    assert worker.run_once() is True
    assert orchestrator.calls == [task.task_id]
    assert task_store.get(task.task_id).attempt == 1


def test_background_worker_retries_temporary_blacklist_failure_without_manual_action() -> None:
    database = Database(":memory:")
    database.initialize()
    task_store = TaskStore(database)
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9001", nickname="temporary", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9001")

    class RecoveringExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _item):
            self.calls += 1
            if self.calls == 1:
                raise BlacklistExecutionError(ExecutionFailureKind.TEMPORARY, "network timeout")
            return type("Result", (), {"success": True, "detail": "done"})()

    executor = RecoveringExecutor()
    worker = BackgroundWorker(
        task_store=task_store,
        orchestrator=object(),
        queue=queue,
        executor=executor,
        config=WorkerConfig(queue_retry_delay=0, max_queue_retries=3),
    )

    assert worker.run_once() is True
    assert queue.get(item.item_id).status.value == "failed"
    assert worker.run_once() is True
    assert queue.get(item.item_id).status.value == "completed"
    assert executor.calls == 2


def test_background_worker_stops_retrying_failed_blacklist_item_at_limit() -> None:
    database = Database(":memory:")
    database.initialize()
    task_store = TaskStore(database)
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9002", nickname="permanent", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9002")

    class FailingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _item):
            self.calls += 1
            raise BlacklistExecutionError(ExecutionFailureKind.TEMPORARY, "still offline")

    executor = FailingExecutor()
    worker = BackgroundWorker(
        task_store=task_store,
        orchestrator=object(),
        queue=queue,
        executor=executor,
        config=WorkerConfig(queue_retry_delay=0, max_queue_retries=1),
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert queue.get(item.item_id).status.value == "failed"
    assert executor.calls == 2


def test_blacklist_queue_claim_is_atomic_for_concurrent_processors() -> None:
    database = Database(":memory:")
    database.initialize()
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9003", nickname="concurrent", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9003")

    executor_started = threading.Event()
    release_executor = threading.Event()
    calls: list[str] = []

    class BlockingExecutor:
        def execute(self, claimed_item):
            calls.append(claimed_item.item_id)
            executor_started.set()
            release_executor.wait(2.0)
            return type("Result", (), {"success": True, "detail": "done"})()

    executor = BlockingExecutor()
    results: dict[str, object] = {}

    def process(name: str) -> None:
        results[name] = queue.process_next(executor)

    first = threading.Thread(target=process, args=("first",))
    second = threading.Thread(target=process, args=("second",))
    first.start()
    assert executor_started.wait(1.0)
    second.start()
    second.join(timeout=1.0)

    try:
        assert second.is_alive() is False
        assert results["second"] is None
        assert calls == [item.item_id]
        assert queue.get(item.item_id).status is BlacklistQueueStatus.PROCESSING
    finally:
        release_executor.set()
        first.join(timeout=1.0)

    assert first.is_alive() is False
    assert results["first"].status is BlacklistQueueStatus.COMPLETED


def test_blacklist_queue_marks_unexpected_executor_exception_failed() -> None:
    database = Database(":memory:")
    database.initialize()
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9004", nickname="crashed", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9004")

    class CrashingExecutor:
        def execute(self, _item):
            raise RuntimeError("browser process crashed")

    processed = queue.process_next(CrashingExecutor())

    assert processed is not None
    assert processed.status is BlacklistQueueStatus.FAILED
    assert processed.last_error == "Blacklist executor failed: browser process crashed"


def test_blacklist_queue_pauses_on_platform_interception() -> None:
    database = Database(":memory:")
    database.initialize()
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9005", nickname="intercepted", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9005")

    class InterceptedExecutor:
        def execute(self, _item):
            raise BlacklistExecutionError(
                ExecutionFailureKind.BLOCKED,
                "Bilibili platform interception detected",
            )

    processed = queue.process_next(InterceptedExecutor())

    assert processed is not None
    assert processed.status is BlacklistQueueStatus.PAUSED
    assert processed.last_error == "Bilibili platform interception detected"


def test_database_recovers_processing_blacklist_items_after_reinitialize(tmp_path) -> None:
    path = tmp_path / "blacklist.sqlite3"
    database = Database(path)
    database.initialize()
    registry = UidRegistry(database)
    queue = BlacklistQueueService(database, registry)
    registry.add(uid="9006", nickname="recovered", state=UidState.QUEUED)
    item, _ = queue.enqueue(uid="9006")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE blacklist_queue SET status = ?, attempts = 1 WHERE item_id = ?",
            (BlacklistQueueStatus.PROCESSING.value, item.item_id),
        )
    database.close()

    restarted = Database(path)
    restarted.initialize()
    recovered = BlacklistQueueService(restarted).get(item.item_id)

    try:
        assert recovered.status is BlacklistQueueStatus.FAILED
        assert recovered.last_error == "Recovered abandoned blacklist item after service restart"
        assert recovered.completed_at is None
    finally:
        restarted.close()
