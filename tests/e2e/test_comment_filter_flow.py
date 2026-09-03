from __future__ import annotations

from fastapi.testclient import TestClient


def _items(response):
    response.raise_for_status()
    return response.json()["items"]


def test_video_submission_to_review_and_blacklist_queue(e2e_app) -> None:
    app, http, blacklist_executor = e2e_app
    assert isinstance(http, TestClient)

    auth = http.post(
        "/api/auth/session",
        json={"cookies": {"SESSDATA": "fixture-only"}, "source": "e2e"},
    )
    assert auth.status_code == 200
    assert auth.json()["status"] == "valid"
    assert auth.json()["cookie_present"] is True
    assert "fixture-only" not in auth.text

    enabled = http.patch("/api/blacklist/settings", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    submitted = http.post(
        "/api/tasks",
        json={
            "video_url": "https://www.bilibili.com/video/BV1e2eFixture01",
            "title": "fixture video",
        },
    )
    assert submitted.status_code == 201
    task_id = submitted.json()["task_id"]
    assert submitted.json()["status"] == "queued"
    assert submitted.json()["video_id"] == "BV1e2eFixture01"

    run = app.state.orchestrator.run(task_id)
    assert run.status.value == "completed"

    task = http.get(f"/api/tasks/{task_id}")
    assert task.status_code == 200
    task_payload = task.json()
    assert task_payload["status"] == "completed"
    assert task_payload["progress"]["saved_comments"] == 3
    assert task_payload["progress"]["declared_comments"] == 3
    assert task_payload["progress"]["coverage"] == 1.0

    comments = _items(http.get(f"/api/tasks/{task_id}/comments"))
    assert len(comments) == 3
    assert {item["uid"] for item in comments} == {"1001", "1002", "1003"}
    assert next(item for item in comments if item["comment_id"] == "c-hit")["content"] == (
        "fixture hostile comment"
    )

    evidence = _items(http.get("/api/reviews", params={"task_id": task_id}))
    evidence_by_uid = {item["uid"]: item for item in evidence}
    assert set(evidence_by_uid) == {"1001", "1002"}
    assert evidence_by_uid["1001"]["decision"] == "hit"
    assert evidence_by_uid["1002"]["decision"] == "uncertain"
    assert evidence_by_uid["1002"]["comments"][0]["content"] == "fixture boundary comment"
    assert evidence_by_uid["1002"]["model_version"] == "fixture-model"

    uid_items = {item["uid"]: item for item in _items(http.get("/api/uids"))}
    assert {uid: item["state"] for uid, item in uid_items.items()} == {
        "1001": "queued",
        "1002": "review",
    }
    assert _items(http.get("/api/uids", params={"state": "queued"}))[0]["uid"] == "1001"
    assert _items(http.get("/api/uids", params={"state": "review"}))[0]["uid"] == "1002"
    assert "1003" not in uid_items

    initial_queue = _items(http.get("/api/blacklist"))
    assert len(initial_queue) == 1
    assert initial_queue[0]["uid"] == "1001"
    assert initial_queue[0]["status"] == "queued"
    assert initial_queue[0]["evidence_id"] == evidence_by_uid["1001"]["evidence_id"]

    highlighted = http.post(
        f"/api/reviews/{evidence_by_uid['1002']['evidence_id']}",
        json={"action": "highlight", "actor": "e2e-user"},
    )
    assert highlighted.status_code == 200
    assert highlighted.json()["action"] == "highlight"
    assert highlighted.json()["before_state"] == "review"
    assert highlighted.json()["after_state"] == "review"
    samples = _items(http.get("/api/samples"))
    assert any(
        item["items"][0]["content"] == "fixture boundary comment" for item in samples
    )

    confirmed = http.post(
        f"/api/reviews/{evidence_by_uid['1002']['evidence_id']}",
        json={"action": "confirm", "actor": "e2e-user"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["action"] == "confirm"
    assert confirmed.json()["before_state"] == "review"
    assert confirmed.json()["after_state"] == "queued"

    revoked = http.post(
        f"/api/reviews/{evidence_by_uid['1001']['evidence_id']}",
        json={"action": "revoke", "actor": "e2e-user"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["action"] == "revoke"
    assert revoked.json()["before_state"] == "queued"
    assert revoked.json()["after_state"] is None

    after_review_uids = {item["uid"]: item for item in _items(http.get("/api/uids"))}
    assert "1001" not in after_review_uids
    assert after_review_uids["1002"]["state"] == "queued"
    after_review_queue = {item["uid"]: item for item in _items(http.get("/api/blacklist"))}
    assert after_review_queue["1001"]["status"] == "cancelled"
    assert after_review_queue["1002"]["status"] == "queued"

    processed = http.post("/api/blacklist/process")
    assert processed.status_code == 200
    assert processed.json()["status"] == "completed"
    assert processed.json()["uid"] == "1002"
    assert blacklist_executor.uids == ["1002"]

    final_uids = {item["uid"]: item for item in _items(http.get("/api/uids"))}
    assert final_uids["1002"]["state"] == "blocked"
