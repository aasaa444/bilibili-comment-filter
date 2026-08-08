from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .blacklist import BlacklistQueueService
from .db import Database
from .models import UidState
from .persistence import EvidenceStore
from .registry import UidNotFoundError, UidRegistry
from .samples import SampleStore


@dataclass(frozen=True)
class ReviewRecord:
    action_id: str
    evidence_id: str
    uid: str
    action: str
    before_state: UidState | None
    after_state: UidState | None
    actor: str
    created_at: datetime


class ReviewService:
    def __init__(
        self,
        database: Database,
        evidence_store: EvidenceStore,
        uid_registry: UidRegistry,
        queue: BlacklistQueueService,
        sample_store: SampleStore,
    ) -> None:
        self.database = database
        self.evidence_store = evidence_store
        self.uid_registry = uid_registry
        self.queue = queue
        self.sample_store = sample_store

    def apply(self, *, evidence_id: str, action: str, actor: str) -> ReviewRecord:
        evidence = self.evidence_store.get(evidence_id)
        normalized_action = {
            "hide-only": "hide_only",
            "positive-sample": "highlight",
        }.get(action, action)
        try:
            current = self.uid_registry.get(evidence.uid)
            before_state = current.state
        except UidNotFoundError:
            current = None
            before_state = None

        after_state = before_state
        if normalized_action in {"revoke", "exception"}:
            after_state = UidState.EXCEPTION
            self.queue.cancel_for_uid(evidence.uid)
            self._update_or_create(evidence.uid, evidence.nickname, after_state)
        elif normalized_action == "hide_only":
            after_state = UidState.HIDDEN
            self.queue.cancel_for_uid(evidence.uid)
            self._update_or_create(evidence.uid, evidence.nickname, after_state)
        elif normalized_action in {"confirm", "keep"}:
            if normalized_action == "confirm":
                after_state = UidState.QUEUED
                record = self._update_or_create(evidence.uid, evidence.nickname, after_state)
                self.queue.enqueue(uid=record.uid, evidence_id=evidence_id)
            elif current is None:
                after_state = UidState.REVIEW
                self._update_or_create(evidence.uid, evidence.nickname, after_state)
        elif normalized_action == "highlight":
            content = _first_comment_content(evidence.comments)
            if content:
                self.sample_store.add_review_sample(content=content)
        else:
            raise ValueError(f"Unsupported review action: {action}")

        action_id = uuid4().hex
        timestamp = datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO review_actions
                    (action_id, evidence_id, uid, action, before_state,
                     after_state, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    evidence_id,
                    evidence.uid,
                    normalized_action,
                    before_state.value if before_state else None,
                    after_state.value if after_state else None,
                    actor,
                    timestamp.isoformat(),
                ),
            )
        return ReviewRecord(
            action_id=action_id,
            evidence_id=evidence_id,
            uid=evidence.uid,
            action=normalized_action,
            before_state=before_state,
            after_state=after_state,
            actor=actor,
            created_at=timestamp,
        )

    def _update_or_create(self, uid: str, nickname: str | None, state: UidState):
        try:
            current = self.uid_registry.get(uid)
        except UidNotFoundError:
            current, _ = self.uid_registry.add(uid=uid, nickname=nickname, state=state)
            return current
        if current.state is state:
            return current
        return self.uid_registry.update(uid=uid, nickname=nickname, state=state)


def _first_comment_content(comments: tuple[dict[str, object], ...]) -> str:
    for comment in comments:
        value = comment.get("content")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
