from fastapi.testclient import TestClient

from service.app import create_app
from service.auth import AuthStatus, AuthVerification


class FixedAuthVerifier:
    def __init__(self, verification: AuthVerification) -> None:
        self.verification = verification

    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        assert cookies
        return self.verification


def test_health_reports_database_worker_and_auth_state() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": {"status": "ready", "detail": "SQLite connection is available"},
        "worker": {"status": "ready", "detail": "Task worker is available"},
        "auth": {"status": "missing", "detail": "No Bilibili session has been synchronized"},
    }


def test_auth_session_reports_verified_state_without_echoing_cookies() -> None:
    verifier = FixedAuthVerifier(
        AuthVerification(status=AuthStatus.VALID, detail="Bilibili session is valid")
    )
    client = TestClient(create_app(db_path=":memory:", auth_verifier=verifier))

    response = client.post(
        "/api/auth/session",
        json={"cookies": {"SESSDATA": "test-secret"}, "source": "extension"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "valid"
    assert response.json()["detail"] == "Bilibili session is valid"
    assert "test-secret" not in response.text
    assert "SESSDATA" not in response.text


def test_auth_session_reports_missing_state_before_any_sync() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    response = client.get("/api/auth/session")

    assert response.status_code == 200
    assert response.json()["status"] == "missing"
    assert response.json()["detail"] == "No Bilibili session has been synchronized"
