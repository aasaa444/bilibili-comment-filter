import pytest
from fastapi.testclient import TestClient

from service.analyzer import (
    AccountBundle,
    AnalysisDecision,
    AnalyzerUnavailableError,
    CommentForAnalysis,
    OpenAICompatibleBatchAnalyzer,
    OpenAICompatibleTransport,
    SampleItem,
    SampleSet,
)
from service.app import UnconfiguredBatchAnalyzer, create_app
from service.auth import AuthStatus, AuthVerification
from service.models import TaskStatus


class FixedAuthVerifier:
    def __init__(self, verification: AuthVerification) -> None:
        self.verification = verification

    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        assert cookies
        return self.verification


def test_health_reports_database_worker_auth_and_model_state(monkeypatch) -> None:
    monkeypatch.delenv("BILIBILI_FILTER_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_OPENAI_MODEL", raising=False)
    client = TestClient(create_app(db_path=":memory:"))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": {"status": "ready", "detail": "SQLite connection is available"},
        "worker": {"status": "ready", "detail": "Task worker is available"},
        "auth": {"status": "missing", "detail": "No Bilibili session has been synchronized"},
        "model": {
            "status": "unconfigured",
            "detail": (
                "远程 OpenAI-compatible 模型未配置；缺少 "
                "BILIBILI_FILTER_OPENAI_BASE_URL、BILIBILI_FILTER_OPENAI_MODEL。"
                "本地 UID 隐藏和任务提交仍可用。"
            ),
            "base_url_configured": False,
            "model_configured": False,
            "api_key_configured": False,
        },
    }


def test_health_exposes_only_remote_model_configuration_flags(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_API_KEY", "secret-model-key")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MODEL", "private-model-name")

    response = TestClient(create_app(db_path=":memory:")).get("/api/health")

    assert response.status_code == 200
    model = response.json()["model"]
    assert model["status"] == "ready"
    assert model["base_url_configured"] is True
    assert model["model_configured"] is True
    assert model["api_key_configured"] is True
    assert "secret-model-key" not in response.text
    assert "model.example" not in response.text
    assert "private-model-name" not in response.text


def test_create_app_reads_blacklist_pacing_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_BLACKLIST_INTERVAL_SECONDS", "90")
    monkeypatch.setenv("BILIBILI_FILTER_BLACKLIST_JITTER_SECONDS", "45.5")

    app = create_app(db_path=":memory:")
    try:
        assert app.state.worker.config.queue_interval == 90.0
        assert app.state.worker.config.queue_jitter == 45.5
    finally:
        app.state.database.close()


def test_health_requires_the_background_worker_when_configured_to_start() -> None:
    with TestClient(create_app(db_path=":memory:", start_background_worker=True)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["worker"]["status"] == "ready"


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


def test_empty_auth_session_does_not_claim_cookie_presence() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    response = client.post(
        "/api/auth/session",
        json={"cookies": {}, "source": "test"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "missing"
    assert response.json()["cookie_present"] is False
    assert client.get("/api/auth/session").json()["cookie_present"] is False


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


def test_valid_auth_session_requeues_tasks_paused_for_auth_only() -> None:
    verifier = FixedAuthVerifier(
        AuthVerification(status=AuthStatus.INVALID, detail="fixture session rejected")
    )
    app = create_app(db_path=":memory:", auth_verifier=verifier)
    client = TestClient(app)

    task_response = client.post(
        "/api/tasks",
        json={"video_url": "https://www.bilibili.com/video/BV1authresume1"},
    )
    task_id = task_response.json()["task_id"]
    run_response = client.post(f"/api/tasks/{task_id}/run")
    assert run_response.json()["status"] == "paused"

    other_task, _ = app.state.task_store.create(
        video_url="https://www.bilibili.com/video/BV1manualpause1"
    )
    app.state.task_store.transition(
        other_task.task_id,
        TaskStatus.PAUSED,
        error_code="collection_paused",
        error_message="fixture pause",
    )

    verifier.verification = AuthVerification(
        status=AuthStatus.VALID, detail="fixture session accepted"
    )
    auth_response = client.post(
        "/api/auth/session",
        json={"cookies": {"SESSDATA": "fixture-sessdata"}, "source": "test"},
    )

    assert auth_response.json()["status"] == "valid"
    resumed = client.get(f"/api/tasks/{task_id}").json()
    assert resumed["status"] == "queued"
    assert resumed["attempt"] == 1
    assert resumed["error_code"] is None
    assert resumed["error_message"] is None
    untouched = client.get(f"/api/tasks/{other_task.task_id}").json()
    assert untouched["status"] == "paused"
    assert untouched["error_code"] == "collection_paused"

    app.state.database.close()


def test_empty_model_environment_values_leave_remote_model_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_BASE_URL", "   ")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MODEL", "")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_API_KEY", " ")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MAX_OUTPUT_TOKENS", "321")
    monkeypatch.delenv("BILIBILI_FILTER_MODEL_URL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_MODEL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_MODEL_KEY", raising=False)

    app = create_app(db_path=":memory:")
    analyzer = app.state.orchestrator.analyzer

    assert isinstance(analyzer, UnconfiguredBatchAnalyzer)
    assert not isinstance(analyzer, OpenAICompatibleBatchAnalyzer)
    assert not hasattr(analyzer, "transport")
    with pytest.raises(AnalyzerUnavailableError, match="Remote OpenAI-compatible model"):
        analyzer.analyze((), SampleSet("samples-empty", ()))


def test_unconfigured_model_still_applies_nickname_hard_rules(monkeypatch) -> None:
    monkeypatch.delenv("BILIBILI_FILTER_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("BILIBILI_FILTER_OPENAI_MODEL", raising=False)

    app = create_app(db_path=":memory:")
    account = AccountBundle(
        uid="350213094",
        nickname="配置的詹黑型昵称",
        comments=(
            CommentForAnalysis(
                comment_id="c-nickname",
                content="普通内容",
                root_id="c-nickname",
                parent_id=None,
                context=(),
                comment_url="https://example.test/c-nickname",
            ),
        ),
    )
    samples = SampleSet(
        "samples-v3",
        (SampleItem("n1", "nickname", "positive", "配置的詹黑型昵称"),),
    )

    with pytest.raises(AnalyzerUnavailableError) as error:
        app.state.orchestrator.analyzer.analyze((account,), samples)

    assert len(error.value.partial_results) == 1
    assert error.value.partial_results[0].decision is AnalysisDecision.HIT
    assert error.value.partial_results[0].uid == "350213094"
    assert error.value.partial_results[0].sample_version == "samples-v3"
    app.state.database.close()


def test_explicit_remote_model_configuration_constructs_openai_transport(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_API_KEY", "remote-test-key")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MODEL", "remote-test-model")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MAX_OUTPUT_TOKENS", "321")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_MAX_BATCH_ACCOUNTS", "17")
    monkeypatch.setenv("BILIBILI_FILTER_OPENAI_TIMEOUT_SECONDS", "222.5")

    app = create_app(db_path=":memory:")
    analyzer = app.state.orchestrator.analyzer

    assert isinstance(analyzer, OpenAICompatibleBatchAnalyzer)
    transport = analyzer.transport
    assert isinstance(transport, OpenAICompatibleTransport)
    assert str(transport.client.base_url) == "https://model.example/v1/"
    assert transport.model == "remote-test-model"
    assert transport.api_key == "remote-test-key"
    assert transport.max_output_tokens == 321
    assert analyzer.max_batch_accounts == 17
    assert transport.client.timeout.read == 222.5
