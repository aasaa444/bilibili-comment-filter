from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from service.analyzer import (
    AccountBundle,
    AnalysisBatchResult,
    AnalysisDecision,
    AnalysisResult,
    AnalyzerInvalidResponseError,
    SampleContext,
    SampleSet,
)
from service.app import create_app
from service.auth import AuthStatus, AuthVerification
from service.collector import (
    CollectionCheckpoint,
    CollectionResult,
    CollectionStats,
    CommentRecord,
)


class ValidVerifier:
    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        return AuthVerification(AuthStatus.VALID, "fixture session is valid")


def _comment(task, uid: str, index: int) -> CommentRecord:
    comment_id = f"comment-{index}"
    return CommentRecord(
        comment_id=comment_id,
        uid=uid,
        nickname=f"user-{uid}",
        content=f"fixture comment {index}",
        video_id=task.video_id,
        comment_url=f"https://example.test/{comment_id}",
        root_id=comment_id,
        parent_id=None,
        level="root",
        created_at=1700000000 + index,
        is_pinned=False,
    )


@dataclass
class CompleteCollector:
    calls: int = 0

    def collect(self, task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        self.calls += 1
        comments = tuple(
            _comment(task, uid, index)
            for index, uid in enumerate(("1001", "1002", "1003"), 1)
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


class ThreeWayAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

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
            batch_count=2,
            sample_context=SampleContext(samples.version, samples.items, "full"),
        )


class InvalidResponseAnalyzer:
    def analyze(
        self, _accounts: tuple[AccountBundle, ...], _samples: SampleSet
    ) -> AnalysisBatchResult:
        raise AnalyzerInvalidResponseError("Model response was not valid JSON")


class IncompleteCollector:
    def collect(self, task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        return CollectionResult(
            comments=(_comment(task, "1001", 1),),
            checkpoint=CollectionCheckpoint(
                root_page=2,
                complete=True,
                declared_comments=2,
                declared_total=2,
            ),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=1,
                declared_comments=2,
                coverage=0.5,
            ),
            complete=True,
        )


class EmptyIncompleteCollector:
    def collect(self, task, _checkpoint: CollectionCheckpoint) -> CollectionResult:
        return CollectionResult(
            comments=(),
            checkpoint=CollectionCheckpoint(
                root_page=2,
                complete=True,
                declared_comments=2,
                declared_total=2,
            ),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=0,
                declared_comments=2,
                coverage=0.0,
            ),
            complete=True,
        )


def _create_task(http: TestClient, suffix: str) -> str:
    http.post("/api/auth/session", json={"cookies": {"SESSDATA": "fixture"}})
    response = http.post(
        "/api/tasks",
        json={"video_url": f"https://www.bilibili.com/video/BV1observability{suffix}"},
    )
    assert response.status_code == 201
    return response.json()["task_id"]


def test_task_observability_records_successful_analysis_decisions() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=CompleteCollector(),
        analyzer=ThreeWayAnalyzer(),
    )
    http = TestClient(app)
    task_id = _create_task(http, "success")

    response = http.post(f"/api/tasks/{task_id}/run")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    events = http.get(f"/api/tasks/{task_id}/events").json()["items"]
    event_types = [event["event_type"] for event in events]
    assert "phase_started" in event_types
    assert "analysis_started" in event_types
    assert "model_response" in event_types
    assert "analysis_completed" in event_types
    assert "task_transition" in event_types
    assert any(
        event["event_type"] == "analysis_completed"
        and event["details"] == {
            "account_count": 3,
            "batch_count": 2,
            "evidence_count": 2,
                "hit_count": 1,
                "non_target_count": 1,
                "uncertain_count": 1,
                "collection_complete": True,
            }
        for event in events
    )

    analysis = http.get(f"/api/tasks/{task_id}/analysis").json()
    assert analysis["latest"]["status"] == "completed"
    assert analysis["latest"]["model"] == "fixture-model"
    assert analysis["latest"]["batch_count"] == 2
    assert analysis["latest"]["account_count"] == 3
    assert analysis["latest"]["hit_count"] == 1
    assert analysis["latest"]["uncertain_count"] == 1
    assert analysis["latest"]["non_target_count"] == 1
    assert analysis["latest"]["evidence_count"] == 2


def test_task_observability_records_invalid_model_response() -> None:
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=CompleteCollector(),
        analyzer=InvalidResponseAnalyzer(),
    )
    http = TestClient(app)
    task_id = _create_task(http, "invalid")

    response = http.post(f"/api/tasks/{task_id}/run")
    assert response.json()["error_code"] == "invalid_model_response"

    analysis = http.get(f"/api/tasks/{task_id}/analysis").json()["latest"]
    assert analysis["status"] == "failed"
    assert analysis["error_code"] == "invalid_model_response"
    assert analysis["error_message"] == "Model response was not valid JSON"
    events = http.get(f"/api/tasks/{task_id}/events").json()["items"]
    assert any(
        event["event_type"] == "analysis_failed"
        and event["status"] == "failed"
        and event["details"]["error_code"] == "invalid_model_response"
        for event in events
    )


def test_task_observability_analyzes_saved_comments_when_collection_is_incomplete() -> None:
    analyzer = ThreeWayAnalyzer()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=IncompleteCollector(),
        analyzer=analyzer,
    )
    http = TestClient(app)
    task_id = _create_task(http, "incomplete")

    response = http.post(f"/api/tasks/{task_id}/run")
    assert response.json()["status"] == "partial"
    assert response.json()["error_code"] == "collection_incomplete"

    analysis = http.get(f"/api/tasks/{task_id}/analysis").json()["latest"]
    assert analysis["status"] == "completed"
    assert analysis["account_count"] == 1
    assert analysis["batch_count"] == 2
    assert analysis["hit_count"] == 1
    assert analysis["evidence_count"] == 1
    assert analyzer.calls == 1
    events = http.get(f"/api/tasks/{task_id}/events").json()["items"]
    event_types = [event["event_type"] for event in events]
    assert "collection_incomplete" in event_types
    assert "analysis_started" in event_types
    assert "model_batch" in event_types
    assert "analysis_completed" in event_types
    assert any(
        event["event_type"] == "collection_incomplete"
        and event["details"]["saved_comments"] == 1
        for event in events
    )


def test_task_observability_skips_ai_when_incomplete_collection_has_no_comments() -> None:
    analyzer = ThreeWayAnalyzer()
    app = create_app(
        db_path=":memory:",
        auth_verifier=ValidVerifier(),
        collector=EmptyIncompleteCollector(),
        analyzer=analyzer,
    )
    http = TestClient(app)
    task_id = _create_task(http, "incompleteempty")

    response = http.post(f"/api/tasks/{task_id}/run")
    assert response.json()["status"] == "partial"
    assert response.json()["error_code"] == "collection_incomplete"

    analysis = http.get(f"/api/tasks/{task_id}/analysis").json()["latest"]
    assert analysis["status"] == "not_started"
    assert analysis["error_code"] == "collection_incomplete"
    assert analyzer.calls == 0
    events = http.get(f"/api/tasks/{task_id}/events").json()["items"]
    assert any(event["event_type"] == "collection_incomplete" for event in events)
    assert not any(event["event_type"] == "analysis_started" for event in events)
