from fastapi.testclient import TestClient

from service.analyzer import OpenAICompatibleBatchAnalyzer, OpenAICompatibleTransport
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


def test_invalid_auth_pauses_task_through_public_api() -> None:
    verifier = FixedAuthVerifier(
        AuthVerification(status=AuthStatus.INVALID, detail="fixture session rejected")
    )
    client = TestClient(create_app(db_path=":memory:", auth_verifier=verifier))

    auth_response = client.post(
        "/api/auth/session",
        json={"cookies": {"SESSDATA": "fixture-sessdata"}, "source": "test"},
    )
    task_response = client.post(
        "/api/tasks",
        json={"video_url": "https://www.bilibili.com/video/BV1authpaused1"},
    )
    run_response = client.post(f"/api/tasks/{task_response.json()['task_id']}/run")

    assert auth_response.status_code == 200
    assert auth_response.json()["status"] == "invalid"
    assert "fixture-sessdata" not in auth_response.text
    assert task_response.status_code == 201
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "paused"
    assert run_response.json()["error_code"] == "auth_unavailable"
    assert client.get(f"/api/tasks/{task_response.json()['task_id']}").json()["status"] == (
        "paused"
    )


def test_empty_model_environment_values_use_local_defaults(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_BASE_URL", "   ")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MODEL", "")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_API_KEY", " ")
    monkeypatch.delenv("BILIBILI_FILTER_MODEL_URL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_MODEL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_MODEL_KEY", raising=False)

    app = create_app(db_path=":memory:")
    analyzer = app.state.orchestrator.analyzer

    assert isinstance(analyzer, OpenAICompatibleBatchAnalyzer)
    transport = analyzer.transport
    assert isinstance(transport, OpenAICompatibleTransport)
    assert str(transport.client.base_url) == "http://127.0.0.1:11434/v1/"
    assert transport.model == "local-model"
    assert transport.api_key is None
