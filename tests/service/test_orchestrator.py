from dataclasses import dataclass

from service.analyzer import (
    AccountBundle,
    AnalysisBatchResult,
    AnalysisDecision,
    AnalysisResult,
    SampleContext,
    SampleSet,
)
from service.app import create_app
from service.auth import AuthStatus, AuthVerification
from service.collector import (
    BilibiliAuthenticationError,
    CollectionCheckpoint,
    CollectionResult,
    CollectionStats,
    CommentRecord,
)


class ValidVerifier:
    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        return AuthVerification(AuthStatus.VALID, "fixture session is valid")


@dataclass
class FixedCollector:
    calls: int = 0

    def collect(self, task, checkpoint: CollectionCheckpoint) -> CollectionResult:
        self.calls += 1
        comments = (
            CommentRecord(
                "c-hit",
                "1001",
                "hit-user",
                "巴斯特 垃圾詹",
                task.video_id,
                "https://example.test/c-hit",
                "c-hit",
                None,
                "root",
                1700000000,
                False,
            ),
            CommentRecord(
                "c-uncertain",
                "1002",
                "uncertain-user",
                "boundary comment",
                task.video_id,
                "https://example.test/c-uncertain",
                "c-uncertain",
                None,
                "root",
                1700000001,
                False,
            ),
            CommentRecord(
                "c-non-target",
                "1003",
                "ordinary-user",
                "normal discussion",
                task.video_id,
                "https://example.test/c-non-target",
                "c-non-target",
                None,
                "root",
                1700000002,
                False,
            ),
        )
        return CollectionResult(
            comments=comments,
            checkpoint=CollectionCheckpoint(root_page=2, complete=True, declared_total=3),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=3,
                declared_comments=3,
                coverage=1.0,
            ),
            complete=True,
        )


class FixedAnalyzer:
    calls: int = 0

    def analyze(
        self, accounts: tuple[AccountBundle, ...], samples: SampleSet
    ) -> AnalysisBatchResult:
        self.calls += 1
        decisions = {
            "1001": AnalysisDecision.HIT,
            "1002": AnalysisDecision.UNCERTAIN,
            "1003": AnalysisDecision.NON_TARGET,
        }
        results = tuple(
            AnalysisResult(
                uid=account.uid,
                decision=decisions[account.uid],
                evidence_comment_ids=tuple(comment.comment_id for comment in account.comments),
                signals=(f"fixture:{decisions[account.uid].value}",),
                reason="fixture decision",
                confidence=0.91,
                model_version="fixture-model",
                sample_version=samples.version,
                rule_version="fixture-rules",
            )
            for account in accounts
        )
        return AnalysisBatchResult(
            results=results,
            batch_count=1,
            sample_context=SampleContext(samples.version, samples.items, "full"),
        )


class ExplodingCollector:
    def collect(self, _task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        raise TimeoutError("fixture collector timeout")


class CollectionPausedCollector:
    def collect(self, task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        return CollectionResult(
            comments=(),
            checkpoint=CollectionCheckpoint(root_page=1, requested_pages=1),
            stats=CollectionStats(requested_pages=1),
            complete=False,
            failed_items=("root_page:1:http_rate_limit:429:Bilibili HTTP 429",),
            pause_reason="Bilibili collection paused after http_rate_limit (429)",
        )


class IncompleteButMarkedCompleteCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        self.calls += 1
        comment = CommentRecord(
            "c-incomplete",
            "1004",
            "incomplete-user",
            "partial collection",
            task.video_id,
            "https://example.test/c-incomplete",
            "c-incomplete",
            None,
            "root",
            1700000003,
            False,
        )
        return CollectionResult(
            comments=(comment,),
            checkpoint=CollectionCheckpoint(
                root_page=2,
                complete=True,
                requested_pages=1,
                declared_comments=2,
            ),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=1,
                declared_comments=2,
                coverage=0.5,
            ),
            complete=True,
        )


class RestartCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, task, checkpoint: CollectionCheckpoint) -> CollectionResult:
        self.calls += 1
        if self.calls == 1:
            comment = CommentRecord(
                "restart-root-1",
                "1001",
                "first-user",
                "first page",
                task.video_id,
                "https://example.test/restart-root-1",
                "restart-root-1",
                None,
                "root",
                1700000010,
                False,
            )
            return CollectionResult(
                comments=(comment,),
                checkpoint=CollectionCheckpoint(
                    root_page=2,
                    complete=False,
                    requested_pages=1,
                    declared_comments=2,
                ),
                stats=CollectionStats(
                    requested_pages=1,
                    saved_comments=1,
                    declared_comments=2,
                    coverage=0.5,
                ),
                complete=False,
            )

        assert checkpoint.root_page == 2
        comment = CommentRecord(
            "restart-root-2",
            "1002",
            "second-user",
            "second page",
            task.video_id,
            "https://example.test/restart-root-2",
            "restart-root-2",
            None,
            "root",
            1700000011,
            False,
        )
        return CollectionResult(
            comments=(comment,),
            checkpoint=CollectionCheckpoint(
                root_page=3,
                complete=True,
                requested_pages=2,
                declared_comments=2,
            ),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=1,
                declared_comments=2,
                coverage=0.5,
            ),
            complete=True,
        )


class ExpiredSessionCollector:
    def collect(self, _task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        raise BilibiliAuthenticationError("fixture session expired")


class ExplodingAnalyzer:
    def analyze(self, _accounts, _samples):
        raise TimeoutError("fixture model timeout")


class RecoveringAnalyzer:
    def __init__(self) -> None:
        self.calls = 0
        self._fallback = FixedAnalyzer()

    def analyze(self, accounts, samples):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("fixture model timeout")
        return self._fallback.analyze(accounts, samples)


def test_orchestrator_applies_three_states_and_repeats_idempotently() -> None:
    collector = FixedCollector()
    analyzer = FixedAnalyzer()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=collector,
        analyzer=analyzer,
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1orchestrator1"}
    ).json()

    first = app.state.orchestrator.run(task["task_id"])
    second = app.state.orchestrator.run(task["task_id"])

    assert first.analyzed_count == 3
    assert first.evidence_count == 2
    assert first.queue_count == 1
    assert second.evidence_count == 2
    assert collector.calls == 1
    assert analyzer.calls == 1
    assert http.get("/api/tasks").json()["items"][0]["status"] == "completed"
    assert http.get("/api/tasks").json()["items"][0]["progress"]["declared_total"] == 3
    uid_items = {item["uid"]: item for item in http.get("/api/uids").json()["items"]}
    assert uid_items["1001"]["state"] == "queued"
    assert uid_items["1002"]["state"] == "review"
    assert "1003" not in uid_items


def test_orchestrator_turns_unexpected_collection_errors_into_partial_tasks() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=ExplodingCollector(),
        analyzer=FixedAnalyzer(),
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1collectionerror"}
    ).json()

    summary = app.state.orchestrator.run(task["task_id"])

    assert summary.status.value == "partial"
    detail = http.get(f"/api/tasks/{task['task_id']}").json()
    assert detail["status"] == "partial"
    assert detail["error_code"] == "collection_failed"
    assert "fixture collector timeout" in detail["error_message"]


def test_orchestrator_does_not_persist_complete_checkpoint_below_coverage() -> None:
    collector = IncompleteButMarkedCompleteCollector()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=collector,
        analyzer=FixedAnalyzer(),
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1checkpointguard"}
    ).json()

    summary = app.state.orchestrator.run(task["task_id"])

    assert summary.status.value == "partial"
    assert app.state.task_store.checkpoint(task["task_id"])["complete"] is False
    app.state.task_store.retry(task["task_id"])
    app.state.orchestrator.run(task["task_id"])
    assert collector.calls == 2


def test_orchestrator_pauses_when_collection_session_expires() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=ExpiredSessionCollector(),
        analyzer=FixedAnalyzer(),
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1sessionexpired"}
    ).json()

    summary = app.state.orchestrator.run(task["task_id"])

    assert summary.status.value == "paused"
    detail = http.get(f"/api/tasks/{task['task_id']}").json()
    assert detail["error_code"] == "auth_unavailable"
    assert "fixture session expired" in detail["error_message"]
    auth = http.get("/api/auth/session").json()
    assert auth["status"] == "invalid"
    assert auth["detail"] == "fixture session expired"


def test_orchestrator_pauses_risk_limited_collection_and_keeps_checkpoint() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=CollectionPausedCollector(),
        analyzer=FixedAnalyzer(),
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BVcollectionpaused"}
    ).json()

    summary = app.state.orchestrator.run(task["task_id"])

    assert summary.status.value == "paused"
    detail = http.get(f"/api/tasks/{task['task_id']}").json()
    assert detail["status"] == "paused"
    assert detail["error_code"] == "collection_paused"
    assert "http_rate_limit (429)" in detail["error_message"]
    assert detail["progress"]["failed_items"] == [
        "root_page:1:http_rate_limit:429:Bilibili HTTP 429"
    ]
    checkpoint = app.state.task_store.checkpoint(task["task_id"])
    assert checkpoint["root_page"] == 1
    assert checkpoint["requested_pages"] == 1
    assert checkpoint["complete"] is False


def test_orchestrator_resumes_checkpoint_after_rebuilding_app(tmp_path) -> None:
    from fastapi.testclient import TestClient

    db_path = tmp_path / "restart.sqlite3"
    collector = RestartCollector()
    first_app = create_app(
        db_path=db_path,
        auth_verifier=ValidVerifier(),
        collector=collector,
        analyzer=FixedAnalyzer(),
    )
    with TestClient(
        first_app
    ) as first_client:
        first_client.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
        task = first_client.post(
            "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1restart"}
        ).json()
        first_run = first_client.post(f"/api/tasks/{task['task_id']}/run")
        assert first_run.json()["status"] == "partial"
        assert first_app.state.task_store.checkpoint(task["task_id"])["root_page"] == 2

    app = create_app(
        db_path=db_path,
        auth_verifier=ValidVerifier(),
        collector=collector,
        analyzer=FixedAnalyzer(),
    )
    with TestClient(app) as second_client:
        app.state.task_store.retry(task["task_id"])
        resumed = second_client.post(f"/api/tasks/{task['task_id']}/run")
        assert resumed.json()["status"] == "completed"
        comments = second_client.get(f"/api/tasks/{task['task_id']}/comments").json()["items"]
        assert [item["comment_id"] for item in comments] == [
            "restart-root-1",
            "restart-root-2",
        ]
        assert app.state.task_store.checkpoint(task["task_id"])["complete"] is True
    assert collector.calls == 2


def test_orchestrator_turns_unexpected_analysis_errors_into_failed_tasks() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=FixedCollector(),
        analyzer=ExplodingAnalyzer(),
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1analysiserror"}
    ).json()

    summary = app.state.orchestrator.run(task["task_id"])

    assert summary.status.value == "failed"
    detail = http.get(f"/api/tasks/{task['task_id']}").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "analysis_failed"
    assert "fixture model timeout" in detail["error_message"]


def test_orchestrator_reuses_completed_collection_when_analysis_is_retried() -> None:
    collector = FixedCollector()
    analyzer = RecoveringAnalyzer()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=collector,
        analyzer=analyzer,
    )
    from fastapi.testclient import TestClient

    http = TestClient(app)
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    task = http.post(
        "/api/tasks", json={"video_url": "https://www.bilibili.com/video/BV1analysisretry"}
    ).json()

    first = app.state.orchestrator.run(task["task_id"])
    assert first.status.value == "failed"
    app.state.task_store.retry(task["task_id"])

    second = app.state.orchestrator.run(task["task_id"])

    assert second.status.value == "completed"
    assert collector.calls == 1
    assert analyzer.calls == 2
