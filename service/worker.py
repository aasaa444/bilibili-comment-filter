from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .blacklist import BlacklistExecutor, BlacklistQueueService
from .orchestrator import TaskOrchestrator
from .tasks import TaskStatus, TaskStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval: float = 2.0
    queue_interval: float = 5.0


class BackgroundWorker:
    """Single-process worker for queued collection, analysis and native actions."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        orchestrator: TaskOrchestrator,
        queue: BlacklistQueueService,
        executor: BlacklistExecutor,
        config: WorkerConfig | None = None,
    ) -> None:
        self.task_store = task_store
        self.orchestrator = orchestrator
        self.queue = queue
        self.executor = executor
        self.config = config or WorkerConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._thread is None or self._thread.is_alive()

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
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        self._thread = None

    def run_once(self) -> bool:
        return self._run_once(process_queue=True)

    def _run_once(self, *, process_queue: bool) -> bool:
        did_work = False
        queued = next(
            (task for task in self.task_store.list() if task.status is TaskStatus.QUEUED), None
        )
        if queued is not None:
            did_work = True
            try:
                self.orchestrator.run(queued.task_id)
            except Exception:
                logger.exception("Queued task %s failed unexpectedly", queued.task_id)

        if process_queue:
            item = self.queue.process_next(self.executor)
            if item is not None:
                did_work = True
        return did_work

    def run_forever(self) -> None:
        last_queue_run = time.monotonic() - max(0.0, self.config.queue_interval)
        while not self._stop.is_set():
            now = time.monotonic()
            process_queue = now - last_queue_run >= max(0.0, self.config.queue_interval)
            did_work = self._run_once(process_queue=process_queue)
            if process_queue:
                last_queue_run = time.monotonic()
            wait_for = 0.05 if did_work else self.config.poll_interval
            self._stop.wait(wait_for)
