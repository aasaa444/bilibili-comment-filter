from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .blacklist import (
    BlacklistExecutor,
    BlacklistQueueService,
)
from .orchestrator import TaskOrchestrator
from .tasks import TaskStatus, TaskStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval: float = 2.0
    # Native blacklist actions are intentionally slow and non-periodic by default.
    queue_interval: float = 60.0
    queue_jitter: float = 30.0
    task_retry_delay: float = 5.0
    max_task_retries: int = 3


class BackgroundWorker:
    """Single-process worker for queued collection, analysis and native actions."""

    RETRYABLE_TASK_ERRORS = frozenset(
        {
            "collection_incomplete",
            "collection_failed",
            "model_unavailable",
            "analysis_failed",
        }
    )

    def __init__(
        self,
        *,
        task_store: TaskStore,
        orchestrator: TaskOrchestrator,
        queue: BlacklistQueueService,
        executor: BlacklistExecutor,
        config: WorkerConfig | None = None,
        auto_blacklist_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.task_store = task_store
        self.orchestrator = orchestrator
        self.queue = queue
        self.executor = executor
        self.config = config or WorkerConfig()
        self.auto_blacklist_enabled = auto_blacklist_enabled or (lambda: False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_retry_after: dict[str, float] = {}

    @property
    def running(self) -> bool:
        """Whether the background thread is currently alive."""

        return self._thread is not None and self._thread.is_alive()

    @property
    def available(self) -> bool:
        """Whether this worker is available to execute work right now."""

        return self.running

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="bilibili-filter-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._thread = None

    def run_once(self) -> bool:
        return self._run_once(process_queue=True)

    def _run_once(self, *, process_queue: bool) -> bool:
        did_work = self._retry_ready_task()
        queued = next(
            (task for task in self.task_store.list() if task.status is TaskStatus.QUEUED), None
        )
        if queued is not None:
            did_work = True
            try:
                summary = self.orchestrator.run(queued.task_id)
                if (
                    summary.status in {TaskStatus.PARTIAL, TaskStatus.FAILED}
                    and summary.error_code in self.RETRYABLE_TASK_ERRORS
                ):
                    self._task_retry_after[queued.task_id] = time.monotonic() + max(
                        0.0, self.config.task_retry_delay
                    )
                elif summary.status not in {TaskStatus.PARTIAL, TaskStatus.FAILED}:
                    self._task_retry_after.pop(queued.task_id, None)
            except Exception:
                logger.exception("Queued task %s failed unexpectedly", queued.task_id)

        if process_queue and self.auto_blacklist_enabled():
            item = self.queue.process_next(self.executor)
            if item is not None:
                did_work = True
        return did_work

    def _retry_ready_task(self) -> bool:
        now = time.monotonic()
        max_retries = max(0, self.config.max_task_retries)
        for task in self.task_store.list():
            if task.status not in {TaskStatus.PARTIAL, TaskStatus.FAILED}:
                continue
            if task.error_code not in self.RETRYABLE_TASK_ERRORS:
                continue
            if task.attempt >= max_retries:
                continue
            if now < self._task_retry_after.get(task.task_id, now):
                continue
            try:
                self.task_store.retry(task.task_id)
            except Exception:
                logger.exception("Unable to retry task %s", task.task_id)
                continue
            self._task_retry_after.pop(task.task_id, None)
            return True
        return False

    def run_forever(self) -> None:
        next_queue_run = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            process_queue = now >= next_queue_run
            did_work = self._run_once(process_queue=process_queue)
            if process_queue:
                next_queue_run = self._next_queue_deadline(time.monotonic())
            wait_for = 0.05 if did_work else self.config.poll_interval
            self._stop.wait(wait_for)

    def _next_queue_deadline(self, now: float) -> float:
        interval = max(0.0, self.config.queue_interval)
        jitter = max(0.0, self.config.queue_jitter) if interval else 0.0
        return now + interval + random.uniform(0.0, jitter)
