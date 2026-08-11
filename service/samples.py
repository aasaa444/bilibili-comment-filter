from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .analyzer import SampleItem, SampleSet
from .db import Database


@dataclass(frozen=True)
class NewSampleItem:
    content: str
    label: str | None = None
    kind: str | None = None
    source: str = "manual"


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
    is_current: bool


class SampleStore:
    """Versioned local sample storage used by the analyzer and management API."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        kind: str,
        label: str,
        items: list[NewSampleItem | tuple[str, str | None]],
        source: str = "manual",
    ) -> SampleRecord:
        candidates: list[tuple[str, str, str, str]] = []
        has_non_empty_input = False
        default_kind = self._storage_kind(kind)
        default_source = self._storage_source(source)
        normalized_label = label.strip() or "positive"
        for item in items:
            if isinstance(item, NewSampleItem):
                content = item.content
                item_label = item.label
                item_kind = item.kind or default_kind
                item_source = item.source
            else:
                content, item_label = item
                item_kind = default_kind
                item_source = default_source
            value = content.strip()
            if not value:
                continue
            has_non_empty_input = True
            candidates.append(
                (
                    value,
                    (item_label or normalized_label).strip() or normalized_label,
                    self._storage_kind(item_kind),
                    self._storage_source(item_source),
                )
            )
        if not has_non_empty_input:
            raise ValueError("At least one non-empty sample is required")

        normalized: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        duplicate_count = 0
        timestamp = datetime.now(UTC)
        sample_id = uuid4().hex
        with self.database.transaction() as connection:
            # T16.1 snapshots are cumulative, but versions created before that
            # behavior was deployed may contain only their own import. Union
            # every published snapshot here so the next draft repairs that
            # legacy gap without mutating any historical version in place.
            inherited_rows = connection.execute(
                """
                SELECT sample_items.*, sample_sets.kind AS sample_set_kind
                FROM sample_items
                JOIN sample_sets ON sample_sets.sample_id = sample_items.sample_id
                WHERE sample_sets.status IN ('published', 'disabled')
                ORDER BY sample_sets.published_at, sample_sets.created_at,
                         sample_sets.sample_id, sample_items.rowid
                """
            ).fetchall()
            for item_row in inherited_rows:
                value = str(item_row["content"]).strip()
                if not value:
                    continue
                self._append_unique(
                    normalized,
                    seen,
                    (
                        value,
                        str(item_row["label"]).strip() or normalized_label,
                        self._storage_kind(item_row["kind"] or item_row["sample_set_kind"]),
                        self._storage_source(item_row["source"]),
                    ),
                )
            for candidate in candidates:
                value, effective_label, candidate_kind, candidate_source = candidate
                key = self._dedupe_key(value, effective_label, candidate_kind)
                if key in seen:
                    duplicate_count += 1
                    continue
                self._append_unique(
                    normalized,
                    seen,
                    (value, effective_label, candidate_kind, candidate_source),
                )
            if not normalized:
                raise ValueError("At least one non-empty sample is required")

            version = f"samples-v{self._next_version()}"
            sample_kind = self._record_kind(item[2] for item in normalized)
            connection.execute(
                """
                INSERT INTO sample_sets
                    (sample_id, kind, version, status, created_at)
                VALUES (?, ?, ?, 'draft', ?)
                """,
                (sample_id, sample_kind, version, timestamp.isoformat()),
            )
            for value, content_label, item_kind, item_source in normalized:
                connection.execute(
                    """
                    INSERT INTO sample_items
                        (item_id, sample_id, kind, label, content, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        sample_id,
                        item_kind,
                        content_label,
                        value,
                        item_source,
                    ),
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
            if row["status"] != "draft":
                raise ValueError("Only draft sample versions can be published")
            connection.execute(
                """
                UPDATE sample_sets
                SET status = 'disabled', retired_at = ?
                WHERE status = 'published'
                """,
                (timestamp,),
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
        draft = self.create(
            kind=kind,
            label="positive",
            items=[NewSampleItem(content, "positive", kind, "review")],
        )
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
                kind=self._storage_kind(item_row["kind"] or row["kind"]),
                label=item_row["label"],
                content=item_row["content"],
                source=self._storage_source(item_row["source"]),
            )
            for item_row in item_rows
        )
        return SampleRecord(
            sample_id=row["sample_id"],
            kind=self._record_kind(item.kind for item in items),
            version=str(row["version"]),
            status=row["status"],
            label=self._record_label(item.label for item in items),
            items=items,
            duplicate_count=duplicate_count,
            created_at=datetime.fromisoformat(row["created_at"]),
            published_at=(
                datetime.fromisoformat(row["published_at"]) if row["published_at"] else None
            ),
            is_current=row["status"] == "published",
        )

    @staticmethod
    def _append_unique(
        normalized: list[tuple[str, str, str, str]],
        seen: set[tuple[str, str, str]],
        item: tuple[str, str, str, str],
    ) -> None:
        value, item_label, item_kind, _ = item
        key = SampleStore._dedupe_key(value, item_label, item_kind)
        if key in seen:
            return
        seen.add(key)
        normalized.append(item)

    @staticmethod
    def _dedupe_key(value: str, label: str, item_kind: str) -> tuple[str, str, str]:
        return item_kind, label, value

    @staticmethod
    def _storage_kind(value: object) -> str:
        return "nickname" if str(value) in {"nickname", "nickname-positive"} else "comment"

    @staticmethod
    def _storage_source(value: object) -> str:
        return str(value) if str(value) in {"manual", "file", "review"} else "manual"

    @staticmethod
    def _record_kind(kinds: object) -> str:
        values = set(str(kind) for kind in kinds)
        if len(values) == 1:
            return next(iter(values))
        return "mixed" if values else "comment"

    @staticmethod
    def _record_label(labels: object) -> str:
        values = set(str(label) for label in labels)
        if len(values) == 1:
            return next(iter(values))
        return "mixed" if values else "positive"
