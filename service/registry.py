from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Database
from .models import UidState


class UidNotFoundError(LookupError):
    pass


class InvalidUidTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class UidRecord:
    uid: str
    nickname: str | None
    state: UidState
    version: int
    created_at: datetime
    updated_at: datetime


class UidRegistry:
    FILTERABLE_STATES = frozenset(
        {
            UidState.HIDDEN,
            UidState.REVIEW,
            UidState.QUEUED,
            UidState.BLOCKED,
            UidState.FAILED,
            UidState.PAUSED,
        }
    )
    ALLOWED_TRANSITIONS: dict[UidState, frozenset[UidState]] = {
        UidState.HIDDEN: frozenset(
            {UidState.HIDDEN, UidState.REVIEW, UidState.QUEUED, UidState.EXCEPTION, UidState.FAILED}
        ),
        UidState.REVIEW: frozenset(
            {UidState.REVIEW, UidState.HIDDEN, UidState.QUEUED, UidState.EXCEPTION}
        ),
        UidState.QUEUED: frozenset(
            {
                UidState.QUEUED,
                UidState.BLOCKED,
                UidState.FAILED,
                UidState.PAUSED,
                UidState.HIDDEN,
                UidState.EXCEPTION,
            }
        ),
        UidState.BLOCKED: frozenset({UidState.BLOCKED, UidState.HIDDEN, UidState.EXCEPTION}),
        UidState.EXCEPTION: frozenset(
            {UidState.EXCEPTION, UidState.HIDDEN, UidState.REVIEW, UidState.QUEUED}
        ),
        UidState.FAILED: frozenset(
            {UidState.FAILED, UidState.QUEUED, UidState.PAUSED, UidState.HIDDEN, UidState.EXCEPTION}
        ),
        UidState.PAUSED: frozenset(
            {UidState.PAUSED, UidState.QUEUED, UidState.FAILED, UidState.HIDDEN, UidState.EXCEPTION}
        ),
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self, *, uid: str, nickname: str | None, state: UidState = UidState.HIDDEN
    ) -> tuple[UidRecord, bool]:
        now = datetime.now(UTC)
        timestamp = now.isoformat()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM uid_records WHERE uid = ?", (uid,)).fetchone()
            if row is not None:
                current = self._from_row(row)
                if nickname is None or nickname == current.nickname:
                    return current, False
                version = self._next_version(connection)
                connection.execute(
                    """
                    UPDATE uid_records
                    SET nickname = ?, version = ?, updated_at = ?
                    WHERE uid = ?
                    """,
                    (nickname, version, timestamp, uid),
                )
                self._event(
                    connection,
                    uid=uid,
                    before_state=current.state,
                    after_state=current.state,
                    action="nickname_snapshot",
                    version=version,
                    timestamp=timestamp,
                )
                return self.get(uid), False

            version = self._next_version(connection)
            connection.execute(
                """
                INSERT INTO uid_records (uid, nickname, state, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uid, nickname, state.value, version, timestamp, timestamp),
            )
            self._event(
                connection,
                uid=uid,
                before_state=None,
                after_state=state,
                action="create",
                version=version,
                timestamp=timestamp,
            )
            return self.get(uid), True

    def update(
        self, *, uid: str, state: UidState | None = None, nickname: str | None = None
    ) -> UidRecord:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM uid_records WHERE uid = ?", (uid,)).fetchone()
            if row is None:
                raise UidNotFoundError(uid)
            current = self._from_row(row)
            next_state = state or current.state
            if next_state not in self.ALLOWED_TRANSITIONS[current.state]:
                raise InvalidUidTransitionError(
                    f"Cannot transition UID {uid} from {current.state.value} to {next_state.value}"
                )
            next_nickname = current.nickname if nickname is None else nickname
            if next_state is current.state and next_nickname == current.nickname:
                return current
            timestamp = datetime.now(UTC).isoformat()
            version = self._next_version(connection)
            connection.execute(
                """
                UPDATE uid_records
                SET nickname = ?, state = ?, version = ?, updated_at = ?
                WHERE uid = ?
                """,
                (next_nickname, next_state.value, version, timestamp, uid),
            )
            self._event(
                connection,
                uid=uid,
                before_state=current.state,
                after_state=next_state,
                action="update",
                version=version,
                timestamp=timestamp,
            )
            return self.get(uid)

    def get(self, uid: str) -> UidRecord:
        row = self.database.execute("SELECT * FROM uid_records WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            raise UidNotFoundError(uid)
        return self._from_row(row)

    def list(self, *, state: UidState | None = None) -> list[UidRecord]:
        if state is None:
            rows = self.database.execute("SELECT * FROM uid_records ORDER BY uid").fetchall()
        else:
            rows = self.database.execute(
                "SELECT * FROM uid_records WHERE state = ? ORDER BY uid", (state.value,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def version(self) -> int:
        row = self.database.execute(
            "SELECT current_version FROM sync_versions WHERE id = 1"
        ).fetchone()
        return int(row["current_version"])

    def sync(self, since: int) -> tuple[str, int, list[UidRecord]]:
        current_version = self.version()
        if since == 0:
            return "full", current_version, self.list()
        rows = self.database.execute(
            """
            SELECT uid, MAX(version) AS latest_version
            FROM uid_events
            WHERE version > ?
            GROUP BY uid
            ORDER BY latest_version, uid
            """,
            (since,),
        ).fetchall()
        records = [self.get(row["uid"]) for row in rows]
        return "delta", current_version, records

    def is_filterable(self, uid: str) -> bool:
        try:
            return self.get(uid).state in self.FILTERABLE_STATES
        except UidNotFoundError:
            return False

    @staticmethod
    def _from_row(row: object) -> UidRecord:
        return UidRecord(
            uid=row["uid"],
            nickname=row["nickname"],
            state=UidState(row["state"]),
            version=int(row["version"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _next_version(connection: object) -> int:
        row = connection.execute(
            "SELECT current_version FROM sync_versions WHERE id = 1"
        ).fetchone()
        version = int(row["current_version"]) + 1
        connection.execute("UPDATE sync_versions SET current_version = ? WHERE id = 1", (version,))
        return version

    @staticmethod
    def _event(
        connection: object,
        *,
        uid: str,
        before_state: UidState | None,
        after_state: UidState,
        action: str,
        version: int,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO uid_events
                (uid, before_state, after_state, action, version, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                before_state.value if before_state else None,
                after_state.value,
                action,
                version,
                timestamp,
            ),
        )
