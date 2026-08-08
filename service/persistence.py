from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .analyzer import AnalysisDecision, AnalysisResult
from .collector import CommentRecord
from .db import Database


class EvidenceNotFoundError(LookupError):
    pass


class CommentStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_many(self, task_id: str, comments: tuple[CommentRecord, ...]) -> int:
        with self.database.transaction() as connection:
            for comment in comments:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO comments
                        (task_id, comment_id, uid, nickname, content, video_id, comment_url,
                         root_id, parent_id, level, created_at, is_pinned, context_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        comment.comment_id,
                        comment.uid,
                        comment.nickname,
                        comment.content,
                        comment.video_id,
                        comment.comment_url,
                        comment.root_id,
                        comment.parent_id,
                        comment.level,
                        comment.created_at,
                        int(comment.is_pinned),
                        json.dumps(list(comment.context), ensure_ascii=False),
                    ),
                )
        row = self.database.execute(
            "SELECT COUNT(*) AS count FROM comments WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["count"])

    def list_for_task(self, task_id: str) -> tuple[CommentRecord, ...]:
        rows = self.database.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY rowid", (task_id,)
        ).fetchall()
        return tuple(
            CommentRecord(
                comment_id=row["comment_id"],
                uid=row["uid"],
                nickname=row["nickname"],
                content=row["content"],
                video_id=row["video_id"],
                comment_url=row["comment_url"],
                root_id=row["root_id"],
                parent_id=row["parent_id"],
                level=row["level"],
                created_at=row["created_at"],
                is_pinned=bool(row["is_pinned"]),
                context=tuple(json.loads(row["context_json"])),
            )
            for row in rows
        )


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    task_id: str
    uid: str
    decision: AnalysisDecision
    nickname: str | None
    video_id: str
    comment_ids: tuple[str, ...]
    comments: tuple[dict[str, object], ...]
    signals: tuple[str, ...]
    reason: str
    confidence: float
    model_version: str
    sample_version: str
    rule_version: str
    created_at: datetime


class EvidenceStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_if_absent(
        self,
        *,
        task_id: str,
        video_id: str,
        account_comments: tuple[CommentRecord, ...],
        result: AnalysisResult,
    ) -> tuple[EvidenceRecord, bool]:
        existing = self.database.execute(
            "SELECT * FROM evidence WHERE task_id = ? AND uid = ?", (task_id, result.uid)
        ).fetchone()
        if existing is not None:
            return self._from_row(existing), False
        evidence_id = uuid4().hex
        timestamp = datetime.now(UTC).isoformat()
        snapshots = tuple(_comment_snapshot(comment) for comment in account_comments)
        nickname = account_comments[0].nickname if account_comments else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO evidence
                    (evidence_id, task_id, uid, decision, nickname, video_id, comment_ids_json,
                     comments_json, signals_json, reason, confidence, model_version,
                     sample_version, rule_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    task_id,
                    result.uid,
                    result.decision.value,
                    nickname,
                    video_id,
                    json.dumps(list(result.evidence_comment_ids)),
                    json.dumps(list(snapshots), ensure_ascii=False),
                    json.dumps(list(result.signals), ensure_ascii=False),
                    result.reason,
                    result.confidence,
                    result.model_version,
                    result.sample_version,
                    result.rule_version,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        return self._from_row(row), True

    def get(self, evidence_id: str) -> EvidenceRecord:
        row = self.database.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(evidence_id)
        return self._from_row(row)

    def list(
        self,
        *,
        task_id: str | None = None,
        uid: str | None = None,
        decision: AnalysisDecision | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if uid is not None:
            clauses.append("uid = ?")
            parameters.append(uid)
        if decision is not None:
            clauses.append("decision = ?")
            parameters.append(decision.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.execute(
            f"SELECT * FROM evidence{where} ORDER BY created_at DESC", tuple(parameters)
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def count_for_task(self, task_id: str) -> int:
        row = self.database.execute(
            "SELECT COUNT(*) AS count FROM evidence WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _from_row(row: object) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            task_id=row["task_id"],
            uid=row["uid"],
            decision=AnalysisDecision(row["decision"]),
            nickname=row["nickname"],
            video_id=row["video_id"],
            comment_ids=tuple(json.loads(row["comment_ids_json"])),
            comments=tuple(json.loads(row["comments_json"])),
            signals=tuple(json.loads(row["signals_json"])),
            reason=row["reason"],
            confidence=float(row["confidence"]),
            model_version=row["model_version"],
            sample_version=row["sample_version"],
            rule_version=row["rule_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def _comment_snapshot(comment: CommentRecord) -> dict[str, object]:
    return {
        "comment_id": comment.comment_id,
        "uid": comment.uid,
        "nickname": comment.nickname,
        "content": comment.content,
        "video_id": comment.video_id,
        "comment_url": comment.comment_url,
        "root_id": comment.root_id,
        "parent_id": comment.parent_id,
        "level": comment.level,
        "created_at": comment.created_at,
        "is_pinned": comment.is_pinned,
        "context": list(comment.context),
    }
