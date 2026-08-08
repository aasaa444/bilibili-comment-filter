from service.blacklist import (
    BlacklistExecutionError,
    BlacklistQueueService,
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
