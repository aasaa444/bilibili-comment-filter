from fastapi.testclient import TestClient

from service.app import create_app


def test_uid_registry_deduplicates_by_uid_and_keeps_nickname_as_snapshot() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    first = client.post("/api/uids", json={"uid": "1001", "nickname": "first-name"})
    second = client.post("/api/uids", json={"uid": "1001", "nickname": "renamed"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["uid"] == "1001"
    assert second.json()["nickname"] == "renamed"
    assert len(client.get("/api/uids").json()["items"]) == 1


def test_uid_registry_exposes_full_then_incremental_versioned_sync() -> None:
    client = TestClient(create_app(db_path=":memory:"))
    client.post("/api/uids", json={"uid": "1001", "nickname": "alpha"})

    full = client.get("/api/uids/sync", params={"since": 0})
    full_payload = full.json()
    assert full.status_code == 200
    assert full_payload["mode"] == "full"
    assert full_payload["version"] == 1
    assert [item["uid"] for item in full_payload["items"]] == ["1001"]

    client.patch("/api/uids/1001", json={"state": "exception"})
    client.post("/api/uids", json={"uid": "1002", "nickname": "beta"})

    delta = client.get("/api/uids/sync", params={"since": full_payload["version"]})
    delta_payload = delta.json()
    assert delta_payload["mode"] == "delta"
    assert delta_payload["version"] == 3
    assert {item["uid"] for item in delta_payload["items"]} == {"1001", "1002"}
    assert delta_payload["items"][0]["state"] == "exception"
