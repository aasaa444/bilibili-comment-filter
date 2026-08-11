from __future__ import annotations

from fastapi.testclient import TestClient

from service.blacklist import BlacklistExecutionError, ExecutionFailureKind


def _items_from(response):
    response.raise_for_status()
    return response.json()["items"]


def _uid_item(items, uid: str):
    return next(item for item in items if item["uid"] == uid)


def _run_task(http: TestClient, task_id: str) -> dict:
    response = http.post(f"/api/tasks/{task_id}/run")
    response.raise_for_status()
    return response.json()


def _publish_sample(http: TestClient, sample_response) -> dict:
    sample = sample_response.json()
    published = http.post(f"/api/samples/{sample['sample_id']}/publish")
    published.raise_for_status()
    assert published.json()["status"] == "published"
    return sample


def test_management_policy_closes_sample_review_local_and_official_boundaries(e2e_app) -> None:
    app, http, blacklist_executor = e2e_app
    assert isinstance(http, TestClient)

    auth = http.post(
        "/api/auth/session",
        json={"cookies": {"SESSDATA": "fixture-only"}, "source": "e2e"},
    )
    assert auth.status_code == 200
    assert auth.json()["status"] == "valid"
    assert "fixture-only" not in auth.text

    initial_settings = http.get("/api/blacklist/settings")
    initial_settings_payload = initial_settings.json()
    assert initial_settings_payload["enabled"] is False
    assert initial_settings_payload["mode"] == "local_only"
    assert initial_settings_payload["updated_at"]

    first_sample = http.post(
        "/api/samples",
        json={"kind": "comment", "label": "positive", "text": "first policy sample"},
    )
    assert first_sample.status_code == 201
    first_sample_payload = _publish_sample(http, first_sample)
    first_sample_version = first_sample_payload["version"]

    submitted = http.post(
        "/api/tasks",
        json={"video_url": "https://www.bilibili.com/video/BV1policyoff"},
    )
    assert submitted.status_code == 201
    first_task_id = submitted.json()["task_id"]
    first_run = _run_task(http, first_task_id)
    assert first_run["status"] == "completed"

    first_evidence = _items_from(http.get("/api/reviews", params={"task_id": first_task_id}))
    assert {item["sample_version"] for item in first_evidence} == {first_sample_version}
    first_events = _items_from(http.get(f"/api/tasks/{first_task_id}/events"))
    assert {
        event["event_type"] for event in first_events
    } >= {
        "task_started",
        "phase_started",
        "collection_progress",
        "collection_saved",
        "analysis_started",
        "model_batch",
        "model_response",
        "analysis_completed",
    }
    first_analysis = http.get(f"/api/tasks/{first_task_id}/analysis").json()
    assert first_analysis["latest"]["status"] == "completed"
    assert first_analysis["latest"]["sample_version"] == first_sample_version

    first_evidence_by_uid = {item["uid"]: item for item in first_evidence}
    uids = _items_from(http.get("/api/uids"))
    assert _uid_item(uids, "1001")["state"] == "hidden"
    assert _uid_item(uids, "1002")["state"] == "review"
    assert _items_from(http.get("/api/blacklist")) == []
    sync = http.get("/api/uids/sync", params={"since": 0}).json()
    sync_by_uid = {item["uid"]: item for item in sync["items"]}
    assert sync_by_uid["1001"]["state"] == "hidden"
    assert sync_by_uid["1002"]["state"] == "review"

    rejected_confirm = http.post(
        f"/api/reviews/{first_evidence_by_uid['1001']['evidence_id']}",
        json={"action": "confirm", "actor": "e2e-user"},
    )
    assert rejected_confirm.status_code == 409
    assert _items_from(http.get("/api/blacklist")) == []

    second_sample = http.post(
        "/api/samples",
        json={"kind": "comment", "label": "positive", "text": "second policy sample"},
    )
    assert second_sample.status_code == 201
    second_sample_payload = second_sample.json()
    second_sample_version = second_sample_payload["version"]
    assert {item["content"] for item in second_sample_payload["items"]} == {
        "first policy sample",
        "second policy sample",
    }
    _publish_sample(http, second_sample)

    enabled = http.patch("/api/blacklist/settings", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["mode"] == "local_and_official_queue"

    second_task = http.post(
        "/api/tasks",
        json={"video_url": "https://www.bilibili.com/video/BV1policyon"},
    )
    assert second_task.status_code == 201
    second_task_id = second_task.json()["task_id"]
    second_run = _run_task(http, second_task_id)
    assert second_run["status"] == "completed"

    second_evidence = _items_from(http.get("/api/reviews", params={"task_id": second_task_id}))
    assert {item["sample_version"] for item in second_evidence} == {second_sample_version}
    assert {
        item["sample_version"]
        for item in _items_from(http.get("/api/reviews", params={"task_id": first_task_id}))
    } == {first_sample_version}
    sample_history = {
        item["version"]: item for item in _items_from(http.get("/api/samples"))
    }
    assert sample_history[first_sample_version]["status"] == "disabled"
    assert {
        item["text"] for item in sample_history[first_sample_version]["items"]
    } == {"first policy sample"}
    assert sample_history[second_sample_version]["status"] == "published"

    queued = _items_from(http.get("/api/blacklist"))
    assert len(queued) == 1
    assert queued[0]["uid"] == "1001"
    assert queued[0]["status"] == "queued"
    assert _uid_item(_items_from(http.get("/api/uids")), "1001")["state"] == "queued"

    disabled_again = http.patch("/api/blacklist/settings", json={"enabled": False})
    assert disabled_again.status_code == 200
    assert disabled_again.json()["mode"] == "local_only"
    assert app.state.worker.run_once() is False
    assert _items_from(http.get("/api/blacklist"))[0]["status"] == "queued"
    assert blacklist_executor.uids == []

    reopened = http.patch("/api/blacklist/settings", json={"enabled": True})
    assert reopened.status_code == 200

    class InterceptedExecutor:
        def execute(self, _item):
            raise BlacklistExecutionError(
                ExecutionFailureKind.INTERCEPTED,
                "selector=.fixture-confirm",
            )

    app.state.worker.executor = InterceptedExecutor()
    assert app.state.worker.run_once() is True
    failed = _items_from(http.get("/api/blacklist"))[0]
    assert failed["status"] == "paused"
    assert failed["failure_type"] == "intercepted"
    assert failed["error_category"] == "page_structure"
    assert failed["user_message"]
    assert failed["recovery_action"]
    assert failed["last_error"] == "selector=.fixture-confirm"

    resumed = http.post(f"/api/blacklist/{failed['item_id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    app.state.worker.executor = blacklist_executor
    assert app.state.worker.run_once() is True
    assert _items_from(http.get("/api/blacklist"))[0]["status"] == "completed"
    final_uid = _uid_item(_items_from(http.get("/api/uids")), "1001")
    assert final_uid["state"] == "blocked"
    assert blacklist_executor.uids == ["1001"]
