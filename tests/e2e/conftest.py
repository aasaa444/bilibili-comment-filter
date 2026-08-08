from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
from service.blacklist import RecordingBlacklistExecutor
from service.collector import (
    CollectionCheckpoint,
    CollectionResult,
    CollectionStats,
    CommentRecord,
)
from service.tasks import VideoTask


class ValidVerifier:
    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        return AuthVerification(AuthStatus.VALID, "e2e fixture session is valid")


@dataclass
class FixedCollector:
    calls: int = 0

    def collect(self, task: VideoTask, checkpoint: CollectionCheckpoint) -> CollectionResult:
        self.calls += 1
        comments = (
            CommentRecord(
                comment_id="c-hit",
                uid="1001",
                nickname="hit-user",
                content="fixture hostile comment",
                video_id=task.video_id,
                comment_url="https://fixture.test/comments/c-hit",
                root_id="c-hit",
                parent_id=None,
                level="root",
                created_at=1700000000,
                is_pinned=False,
                context=("fixture thread context",),
            ),
            CommentRecord(
                comment_id="c-uncertain",
                uid="1002",
                nickname="uncertain-user",
                content="fixture boundary comment",
                video_id=task.video_id,
                comment_url="https://fixture.test/comments/c-uncertain",
                root_id="c-uncertain",
                parent_id=None,
                level="root",
                created_at=1700000001,
                is_pinned=False,
                context=("fixture review context",),
            ),
            CommentRecord(
                comment_id="c-non-target",
                uid="1003",
                nickname="ordinary-user",
                content="fixture ordinary discussion",
                video_id=task.video_id,
                comment_url="https://fixture.test/comments/c-non-target",
                root_id="c-non-target",
                parent_id=None,
                level="root",
                created_at=1700000002,
                is_pinned=False,
                context=(),
            ),
        )
        return CollectionResult(
            comments=comments,
            checkpoint=CollectionCheckpoint(
                root_page=2,
                complete=True,
                requested_pages=1,
                declared_comments=3,
            ),
            stats=CollectionStats(
                requested_pages=1,
                saved_comments=3,
                declared_comments=3,
                coverage=1.0,
            ),
            complete=True,
        )


@dataclass
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


@pytest.fixture
def e2e_app(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient, RecordingBlacklistExecutor]]:
    blacklist_executor = RecordingBlacklistExecutor()
    app = create_app(
        db_path=tmp_path / "bilibili-filter-e2e.sqlite3",
        auth_verifier=ValidVerifier(),
        collector=FixedCollector(),
        analyzer=FixedAnalyzer(),
        blacklist_executor=blacklist_executor,
        start_background_worker=False,
    )
    with TestClient(app) as client:
        yield app, client, blacklist_executor
