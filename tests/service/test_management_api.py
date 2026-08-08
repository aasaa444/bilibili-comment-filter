from fastapi.testclient import TestClient
from test_orchestrator import FixedAnalyzer, FixedCollector, ValidVerifier

from service.app import create_app
from service.blacklist import RecordingBlacklistExecutor
from service.db import Database
from service.models import UidState
from service.registry import UidRegistry


def seeded_app():
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=FixedCollector(),
        analyzer=FixedAnalyzer(),
        blacklist_executor=RecordingBlacklistExecutor(),
    )
    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1management1"}
    ).json()
    app.state.orchestrator.run(task["task_id"])
    return app, http, task["task_id"]


def test_comments_and_evidence_api_expose_reviewable_context() -> None:
    _, http, task_id = seeded_app()

    comments = http.get(f"/api/tasks/{task_id}/comments")
    reviews = http.get("/api/reviews", params={"task_id": task_id})

    assert comments.status_code == 200
    assert len(comments.json()["items"]) == 3
    assert reviews.status_code == 200
    assert len(reviews.json()["items"]) == 2
    assert reviews.json()["items"][0]["comments"][0]["content"]


def test_review_exception_restores_display_and_cancels_pending_blacklist() -> None:
    _, http, task_id = seeded_app()
    evidence = next(
        item
        for item in http.get("/api/reviews", params={"task_id": task_id}).json()["items"]
        if item["uid"] == "1001"
    )

    action = http.post(
        f"/api/reviews/{evidence['evidence_id']}",
        json={"action": "exception", "actor": "test-user"},
    )

    assert action.status_code == 200
    assert action.json()["after_state"] == "exception"
    history = http.get("/api/review-actions", params={"uid": "1001"})
    assert history.status_code == 200
    assert history.json()[0]["action"] == "exception"
    assert history.json()[0]["actor"] == "test-user"
    uid = next(item for item in http.get("/api/uids").json()["items"] if item["uid"] == "1001")
    assert uid["state"] == "exception"
    queue = next(
        item for item in http.get("/api/blacklist").json()["items"] if item["uid"] == "1001"
    )
    assert queue["status"] == "cancelled"


def test_missing_review_evidence_returns_not_found() -> None:
    http = TestClient(create_app(db_path=":memory:"))

    response = http.post(
        "/api/reviews/missing-evidence",
        json={"action": "exception", "actor": "test-user"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Evidence missing-evidence was not found"


def test_samples_can_be_previewed_deduplicated_and_published() -> None:
    _, http, _ = seeded_app()

    draft = http.post(
        "/api/samples",
        json={
            "kind": "comment",
            "label": "positive",
            "text": "example one\nexample one\nexample two",
        },
    )
    sample_id = draft.json()["sample_id"]
    published = http.post(f"/api/samples/{sample_id}/publish")

    assert draft.status_code == 201
    assert draft.json()["duplicate_count"] == 1
    assert len(draft.json()["items"]) == 2
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["version"] == "samples-v1"


def test_negative_sample_response_keeps_negative_kind() -> None:
    _, http, _ = seeded_app()

    response = http.post(
        "/api/samples",
        json={"kind": "comment", "label": "negative", "text": "普通批评"},
    )

    assert response.status_code == 201
    assert response.json()["items"][0]["kind"] == "comment-negative"


def test_highlighted_review_sample_is_published_for_follow_up_analysis() -> None:
    _, http, task_id = seeded_app()
    evidence_id = http.get("/api/reviews", params={"task_id": task_id}).json()["items"][0][
        "evidence_id"
    ]

    response = http.post(
        f"/api/reviews/{evidence_id}",
        json={"action": "highlight", "actor": "reviewer"},
    )

    assert response.status_code == 200
    samples = http.get("/api/samples").json()["items"]
    assert len(samples) == 1
    assert samples[0]["status"] == "published"


def test_blacklist_queue_pause_resume_and_test_executor_are_observable() -> None:
    database = Database(":memory:")
    database.initialize()
    registry = UidRegistry(database)
    registry.add(uid="9001", nickname="queue-user", state=UidState.QUEUED)
    app = create_app(db_path=":memory:", blacklist_executor=RecordingBlacklistExecutor())
    app.state.uid_registry.add(uid="9001", nickname="queue-user", state=UidState.QUEUED)
    item, _ = app.state.blacklist_queue.enqueue(uid="9001")
    http = TestClient(app)

    assert http.post(f"/api/blacklist/{item.item_id}/pause").json()["status"] == "paused"
    assert http.post(f"/api/blacklist/{item.item_id}/resume").json()["status"] == "queued"
    processed = http.post("/api/blacklist/process")

    assert processed.status_code == 200
    assert processed.json()["status"] == "completed"
    assert app.state.blacklist_executor.uids == ["9001"]
