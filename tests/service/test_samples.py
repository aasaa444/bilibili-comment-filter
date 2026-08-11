import sqlite3

from service.db import Database
from service.samples import SampleStore


def test_legacy_sample_items_are_migrated_without_losing_historical_text(tmp_path) -> None:
    database_path = tmp_path / "legacy-samples.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE sample_sets (
            sample_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            published_at TEXT,
            retired_at TEXT
        );
        CREATE TABLE sample_items (
            item_id TEXT PRIMARY KEY,
            sample_id TEXT NOT NULL,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            UNIQUE (sample_id, label, content),
            FOREIGN KEY (sample_id) REFERENCES sample_sets(sample_id)
        );
        """
    )
    connection.execute(
        """
        INSERT INTO sample_sets
            (sample_id, kind, version, status, created_at, published_at)
        VALUES ('legacy-1', 'comment', 'samples-v1', 'published', ?, ?)
        """,
        ("2026-08-09T00:00:00+00:00", "2026-08-09T00:01:00+00:00"),
    )
    connection.executemany(
        """
        INSERT INTO sample_items (item_id, sample_id, label, content)
        VALUES (?, 'legacy-1', ?, 'same body')
        """,
        [("item-1", "positive"), ("item-2", "negative")],
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    database.initialize()
    record = SampleStore(database).get("legacy-1")

    assert len(record.items) == 2
    assert {(item.label, item.kind, item.content, item.source) for item in record.items} == {
        ("positive", "comment", "same body", "manual"),
        ("negative", "comment", "same body", "manual"),
    }


def test_new_snapshot_recovers_items_from_legacy_incremental_versions() -> None:
    database = Database(":memory:")
    database.initialize()
    with database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO sample_sets
                (sample_id, kind, version, status, created_at, published_at)
            VALUES (?, 'comment', ?, ?, ?, ?)
            """,
            [
                (
                    "legacy-v1",
                    "samples-v1",
                    "disabled",
                    "2026-08-10T00:00:00+00:00",
                    "2026-08-10T00:01:00+00:00",
                ),
                (
                    "legacy-v2",
                    "samples-v2",
                    "published",
                    "2026-08-10T00:02:00+00:00",
                    "2026-08-10T00:03:00+00:00",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO sample_items
                (item_id, sample_id, kind, label, content, source)
            VALUES (?, ?, 'comment', 'positive', ?, 'manual')
            """,
            [
                ("legacy-item-v1", "legacy-v1", "first historical sample"),
                ("legacy-item-v2", "legacy-v2", "second historical sample"),
            ],
        )

    draft = SampleStore(database).create(
        kind="comment",
        label="positive",
        items=[("new sample", None)],
    )

    assert {item.content for item in draft.items} == {
        "first historical sample",
        "second historical sample",
        "new sample",
    }
