from service.worker import BackgroundWorker, WorkerConfig


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
