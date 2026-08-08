from fastapi.testclient import TestClient

from service.app import create_app

VIDEO_URL = "https://www.bilibili.com/video/BV1task123"


def test_task_creation_is_idempotent_for_the_same_video() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    first = client.post("/api/tasks", json={"video_url": VIDEO_URL})
    duplicate = client.post("/api/tasks", json={"video_url": VIDEO_URL})

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["task_id"] == first.json()["task_id"]
    assert duplicate.json()["video_id"] == "BV1task123"
    assert duplicate.json()["status"] == "queued"
    assert len(client.get("/api/tasks").json()["items"]) == 1


def test_task_detail_exposes_lifecycle_progress_and_retry_contract() -> None:
    client = TestClient(create_app(db_path=":memory:"))
    created = client.post("/api/tasks", json={"video_url": VIDEO_URL}).json()

    detail = client.get(f"/api/tasks/{created['task_id']}")

    assert detail.status_code == 200
    assert detail.json()["progress"] == {
        "requested_pages": 0,
        "saved_comments": 0,
        "saved_replies": 0,
        "pinned_comments": 0,
        "declared_comments": 0,
        "declared_replies": 0,
        "coverage": 0.0,
        "failed_items": [],
    }

    retry = client.post(f"/api/tasks/{created['task_id']}/retry")

    assert retry.status_code == 409
    assert retry.json()["detail"] == "Only failed, partial, or paused tasks can be retried"
