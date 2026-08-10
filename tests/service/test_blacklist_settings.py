from fastapi.testclient import TestClient
from test_orchestrator import FixedAnalyzer, FixedCollector, ValidVerifier

from service.app import create_app
from service.blacklist import RecordingBlacklistExecutor
from service.models import UidState


def build_app(db_path: str, executor: RecordingBlacklistExecutor | None = None):
    return create_app(
        db_path=db_path,
        auth_verifier=ValidVerifier(),
        collector=FixedCollector(),
        analyzer=FixedAnalyzer(),
        blacklist_executor=executor or RecordingBlacklistExecutor(),
    )


def test_blacklist_switch_defaults_off_and_survives_service_restart(tmp_path) -> None:
    database = tmp_path / "settings.sqlite3"

    with TestClient(build_app(database)) as client:
        initial = client.get("/api/blacklist/settings")
        assert initial.status_code == 200
        assert initial.json()["enabled"] is False
        assert initial.json()["mode"] == "local_only"

        enabled = client.patch("/api/blacklist/settings", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["mode"] == "local_and_official_queue"

    with TestClient(build_app(database)) as client:
        restarted = client.get("/api/blacklist/settings")
        assert restarted.json()["enabled"] is True
        assert restarted.json()["mode"] == "local_and_official_queue"


def test_disabled_switch_hides_hits_without_creating_official_queue_items() -> None:
    app = build_app(":memory:")
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
        task = client.post(
            "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1switchoff"}
        ).json()

        summary = app.state.orchestrator.run(task["task_id"])

        assert summary.evidence_count == 2
        assert client.get("/api/blacklist").json()["items"] == []
        uid_items = {item["uid"]: item for item in client.get("/api/uids").json()["items"]}
        assert uid_items["1001"]["state"] == UidState.HIDDEN
        assert uid_items["1002"]["state"] == UidState.REVIEW


def test_disabled_switch_does_not_consume_queued_items_until_enabled() -> None:
    executor = RecordingBlacklistExecutor()
    app = build_app(":memory:", executor)
    app.state.uid_registry.add(uid="9001", nickname="queued", state=UidState.QUEUED)
    item, created = app.state.blacklist_queue.enqueue(uid="9001")
    assert created is True

    assert app.state.worker.run_once() is False
    assert app.state.blacklist_queue.get(item.item_id).status.value == "queued"
    assert executor.uids == []

    app.state.settings_store.set_blacklist_automation(True)
    assert app.state.worker.run_once() is True
    assert app.state.blacklist_queue.get(item.item_id).status.value == "completed"
    assert executor.uids == ["9001"]


def test_manual_process_endpoint_is_closed_when_automatic_blacklist_is_disabled() -> None:
    app = build_app(":memory:")
    with TestClient(app) as client:
        response = client.post("/api/blacklist/process")

    assert response.status_code == 409
    assert "仅本地隐藏" in response.json()["detail"]


def test_manual_confirm_requires_the_official_blacklist_switch() -> None:
    app = build_app(":memory:")
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
        task = client.post(
            "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1manualconfirm"}
        ).json()
        app.state.orchestrator.run(task["task_id"])
        evidence = client.get(
            "/api/reviews", params={"task_id": task["task_id"]}
        ).json()["items"][0]

        response = client.post(
            f"/api/reviews/{evidence['evidence_id']}",
            json={"action": "confirm", "actor": "reviewer"},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "自动执行官方拉黑已关闭；请先开启总开关，或选择“仅本地隐藏”"
        )
        assert client.get("/api/blacklist").json()["items"] == []
        assert any(
            item["evidence_id"] == evidence["evidence_id"]
            for item in client.get("/api/reviews").json()["items"]
        )


def test_legacy_keep_history_remains_readable_without_becoming_official_confirm() -> None:
    app = build_app(":memory:")
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
        task = client.post(
            "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1legacykeep"}
        ).json()
        app.state.orchestrator.run(task["task_id"])
        evidence = client.get("/api/reviews", params={"task_id": task["task_id"]}).json()["items"][0]

        with app.state.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO review_actions
                    (action_id, evidence_id, uid, action, before_state, after_state, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-keep",
                    evidence["evidence_id"],
                    evidence["uid"],
                    "keep",
                    "hidden",
                    "review",
                    "legacy-user",
                    "2026-08-11T00:00:00+00:00",
                ),
            )

        history = client.get("/api/review-actions", params={"evidence_id": evidence["evidence_id"]})
        assert history.status_code == 200
        assert history.json()[0]["action"] == "keep"

        rejected = client.post(
            f"/api/reviews/{evidence['evidence_id']}",
            json={"action": "keep", "actor": "legacy-user"},
        )
        assert rejected.status_code == 422
        assert client.get("/api/blacklist").json()["items"] == []
