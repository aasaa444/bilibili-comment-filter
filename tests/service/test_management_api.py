from fastapi.testclient import TestClient
from test_orchestrator import FixedAnalyzer, FixedCollector, ValidVerifier

from service.app import create_app
from service.blacklist import RecordingBlacklistExecutor
from service.db import Database
from service.models import UidState
from service.registry import UidRegistry


class CapturingAnalyzer(FixedAnalyzer):
    def __init__(self) -> None:
        self.last_samples = ()

    def analyze(self, accounts, samples):
        self.last_samples = samples.items
        return super().analyze(accounts, samples)


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


def test_new_sample_version_inherits_current_snapshot_and_preserves_mixed_items() -> None:
    app, http, _ = seeded_app()

    base = http.post(
        "/api/samples",
        json={"kind": "comment", "label": "positive", "text": "base comment"},
    )
    assert base.status_code == 201
    assert http.post(f"/api/samples/{base.json()['sample_id']}/publish").status_code == 200

    nickname = http.post(
        "/api/samples",
        json={"kind": "nickname", "label": "positive", "text": "hostile nickname"},
    )
    assert nickname.status_code == 201
    assert nickname.json()["is_current"] is False
    assert [item["kind"] for item in nickname.json()["items"]] == [
        "comment-positive",
        "nickname-positive",
    ]
    assert {item["text"] for item in nickname.json()["items"]} == {
        "base comment",
        "hostile nickname",
    }
    assert http.post(f"/api/samples/{nickname.json()['sample_id']}/publish").status_code == 200

    next_draft = http.post(
        "/api/samples",
        json={
            "kind": "comment",
            "label": "positive",
            "text": "base comment\nnew comment\nbase comment",
        },
    )

    assert next_draft.status_code == 201
    assert next_draft.json()["duplicate_count"] == 2
    assert len(next_draft.json()["items"]) == 3
    assert {item["text"] for item in next_draft.json()["items"]} == {
        "base comment",
        "hostile nickname",
        "new comment",
    }

    published = http.post(f"/api/samples/{next_draft.json()['sample_id']}/publish")
    assert published.status_code == 200
    assert published.json()["is_current"] is True
    assert http.post(f"/api/samples/{base.json()['sample_id']}/publish").status_code == 409

    versions = http.get("/api/samples").json()["items"]
    by_version = {item["version"]: item for item in versions}
    assert by_version["samples-v1"]["status"] == "disabled"
    assert by_version["samples-v1"]["items"][0]["text"] == "base comment"
    assert by_version["samples-v2"]["status"] == "disabled"
    assert len(by_version["samples-v2"]["items"]) == 2
    assert by_version["samples-v3"]["status"] == "published"
    assert len(by_version["samples-v3"]["items"]) == 3

    current = app.state.sample_store.current()
    assert current.version == "samples-v3"
    assert {item.kind for item in current.items} == {"comment", "nickname"}
    assert {item.content for item in current.items} == {
        "base comment",
        "hostile nickname",
        "new comment",
    }


def test_sample_response_exposes_item_source_and_label() -> None:
    _, http, _ = seeded_app()

    response = http.post(
        "/api/samples",
        json={
            "kind": "comment",
            "label": "positive",
            "items": [
                {
                    "content": "same imported body",
                    "label": "positive",
                    "kind": "comment",
                    "source": "file",
                },
                {
                    "content": "same imported body",
                    "label": "negative",
                    "kind": "comment",
                    "source": "file",
                },
            ],
        },
    )

    assert response.status_code == 201
    assert response.json()["items"] == [
        {
            "text": "same imported body",
            "content": "same imported body",
            "kind": "comment-positive",
            "label": "positive",
            "source": "file",
        },
        {
            "text": "same imported body",
            "content": "same imported body",
            "kind": "comment-negative",
            "label": "negative",
            "source": "file",
        },
    ]


def test_orchestrator_receives_the_complete_current_sample_snapshot() -> None:
    analyzer = CapturingAnalyzer()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=FixedCollector(),
        analyzer=analyzer,
        blacklist_executor=RecordingBlacklistExecutor(),
    )
    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})

    first = http.post(
        "/api/samples",
        json={"kind": "comment", "label": "positive", "text": "first sample"},
    ).json()
    http.post(f"/api/samples/{first['sample_id']}/publish")
    second = http.post(
        "/api/samples",
        json={"kind": "nickname", "label": "positive", "text": "nickname sample"},
    ).json()
    http.post(f"/api/samples/{second['sample_id']}/publish")

    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1fullsnapshot"}
    ).json()
    app.state.orchestrator.run(task["task_id"])

    assert {item.content for item in analyzer.last_samples} == {
        "first sample",
        "nickname sample",
    }
    assert {item.kind for item in analyzer.last_samples} == {"comment", "nickname"}


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
    assert samples[0]["items"][0]["source"] == "review"


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
