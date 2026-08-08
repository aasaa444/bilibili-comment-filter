from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .analyzer import SampleItem, SampleSet
from .db import Database


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    kind: str
    version: str
    status: str
    label: str
    items: tuple[SampleItem, ...]
    duplicate_count: int
    created_at: datetime
    published_at: datetime | None


class SampleStore:
    """Versioned local sample storage used by the analyzer and management API."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        kind: str,
        label: str,
        items: list[tuple[str, str | None]],
    ) -> SampleRecord:
        normalized: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        duplicate_count = 0
        for content, item_label in items:
            value = content.strip()
            effective_label = (item_label or label).strip() or label
            key = (effective_label, value)
            if not value:
                continue
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            normalized.append(key)
        if not normalized:
            raise ValueError("At least one non-empty sample is required")

        timestamp = datetime.now(UTC)
        sample_id = uuid4().hex
        version = f"samples-v{self._next_version()}"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sample_sets
                    (sample_id, kind, version, status, created_at)
                VALUES (?, ?, ?, 'draft', ?)
                """,
                (sample_id, kind, version, timestamp.isoformat()),
            )
            for content_label, value in normalized:
                connection.execute(
                    """
                    INSERT INTO sample_items (item_id, sample_id, label, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (uuid4().hex, sample_id, content_label, value),
                )
        return self.get(sample_id, duplicate_count=duplicate_count)

    def get(self, sample_id: str, *, duplicate_count: int = 0) -> SampleRecord:
        row = self.database.execute(
            "SELECT * FROM sample_sets WHERE sample_id = ?", (sample_id,)
        ).fetchone()
        if row is None:
            raise KeyError(sample_id)
        return self._from_row(row, duplicate_count=duplicate_count)

    def list(self) -> tuple[SampleRecord, ...]:
        rows = self.database.execute(
            "SELECT * FROM sample_sets ORDER BY created_at DESC, sample_id DESC"
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def publish(self, sample_id: str) -> SampleRecord:
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sample_sets WHERE sample_id = ?", (sample_id,)
            ).fetchone()
            if row is None:
                raise KeyError(sample_id)
            connection.execute(
                "UPDATE sample_sets SET status = 'disabled' WHERE status = 'published'"
            )
            connection.execute(
                """
                UPDATE sample_sets
                SET status = 'published', published_at = ?, retired_at = NULL
                WHERE sample_id = ?
                """,
                (timestamp, sample_id),
            )
        return self.get(sample_id)

    def current(self) -> SampleSet:
        row = self.database.execute(
            """
            SELECT * FROM sample_sets
            WHERE status = 'published'
            ORDER BY published_at DESC, sample_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return SampleSet("samples-empty", ())
        record = self._from_row(row)
        return SampleSet(record.version, record.items)

    def add_review_sample(self, *, content: str, kind: str = "comment") -> SampleRecord:
        draft = self.create(kind=kind, label="positive", items=[(content, "positive")])
        return self.publish(draft.sample_id)

    def _next_version(self) -> int:
        rows = self.database.execute("SELECT version FROM sample_sets").fetchall()
        versions = [
            int(str(row["version"]).rsplit("v", 1)[-1])
            for row in rows
            if str(row["version"]).rsplit("v", 1)[-1].isdigit()
        ]
        return max(versions, default=0) + 1

    def _from_row(self, row: object, *, duplicate_count: int = 0) -> SampleRecord:
        item_rows = self.database.execute(
            "SELECT * FROM sample_items WHERE sample_id = ? ORDER BY rowid",
            (row["sample_id"],),
        ).fetchall()
        items = tuple(
            SampleItem(
                sample_id=item_row["item_id"],
                kind="nickname" if row["kind"] == "nickname" else "comment",
                label=item_row["label"],
                content=item_row["content"],
            )
            for item_row in item_rows
        )
        return SampleRecord(
            sample_id=row["sample_id"],
            kind=row["kind"],
            version=str(row["version"]),
            status=row["status"],
            label=items[0].label if items else "positive",
            items=items,
            duplicate_count=duplicate_count,
            created_at=datetime.fromisoformat(row["created_at"]),
            published_at=(
                datetime.fromisoformat(row["published_at"]) if row["published_at"] else None
            ),
        )
