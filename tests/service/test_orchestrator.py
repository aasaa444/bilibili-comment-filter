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
            checkpoint=CollectionCheckpoint(root_page=2, complete=True),
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
    uid_items = {item["uid"]: item for item in http.get("/api/uids").json()["items"]}
    assert uid_items["1001"]["state"] == "queued"
    assert uid_items["1002"]["state"] == "review"
    assert "1003" not in uid_items
