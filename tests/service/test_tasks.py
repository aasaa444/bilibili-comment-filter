import sqlite3

from fastapi.testclient import TestClient

from service.app import create_app
from service.db import Database
from service.tasks import TaskProgress, TaskStore

VIDEO_URL = "https://www.bilibili.com/video/BV1task123"


def test_task_creation_is_idempotent_for_the_same_video() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    first = client.post("/api/tasks", json={"video_url": VIDEO_URL})
    duplicate = client.post("/api/tasks", json={"video_url": VIDEO_URL})

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["task_id"] == first.json()["task_id"]
    assert duplicate.json()["video_id"] == "BV1task123"
    assert duplicate.json()["status"] == "queued"
    assert len(client.get("/api/tasks").json()["items"]) == 1


def test_task_detail_exposes_lifecycle_progress_and_retry_contract() -> None:
    client = TestClient(create_app(db_path=":memory:"))
    created = client.post("/api/tasks", json={"video_url": VIDEO_URL}).json()

    detail = client.get(f"/api/tasks/{created['task_id']}")

    assert detail.status_code == 200
    assert detail.json()["progress"] == {
        "requested_pages": 0,
        "saved_comments": 0,
        "saved_replies": 0,
        "pinned_comments": 0,
        "declared_comments": 0,
        "declared_replies": 0,
        "coverage": 0.0,
        "failed_items": [],
    }

    retry = client.post(f"/api/tasks/{created['task_id']}/retry")

    assert retry.status_code == 409
    assert retry.json()["detail"] == "Only failed, partial, or paused tasks can be retried"


def test_task_creation_rejects_non_bilibili_or_non_video_paths() -> None:
    client = TestClient(create_app(db_path=":memory:"))

    urls = [
        "https://example.test/video/BV1valid1234",
        "https://www.bilibili.com/live/video/BV1valid1234",
        "http://www.bilibili.com/video/BV1valid1234",
        "https://m.bilibili.com/video/BV1valid1234",
    ]

    for url in urls:
        response = client.post("/api/tasks", json={"video_url": url})
        assert response.status_code == 422, url


def test_checkpoint_round_trip_survives_task_store_restart(tmp_path) -> None:
    db_path = tmp_path / "tasks.sqlite3"
    database = Database(db_path)
    database.initialize()
    task, _ = TaskStore(database).create(video_url=VIDEO_URL)

    expected = {
        "root_cursor": 7,
        "requested_pages": 5,
        "declared_comments": 42,
        "declared_reply_counts": {"root-1": 3, "root-2": 1},
        "root_page": 4,
        "replies": {"root-2": 2},
        "complete": True,
    }
    TaskStore(database).update_progress(
        task.task_id,
        TaskProgress(
            requested_pages=5,
            saved_comments=4,
            saved_replies=3,
            declared_comments=42,
            declared_replies=4,
            coverage=1.0,
        ),
        checkpoint=expected,
    )
    database.close()

    restarted_database = Database(db_path)
    restarted_database.initialize()

    assert TaskStore(restarted_database).checkpoint(task.task_id) == expected


def test_checkpoint_migrates_legacy_table_and_preserves_legacy_values(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    timestamp = "2026-08-09T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE video_tasks (
                task_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL UNIQUE,
                video_url TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                requested_pages INTEGER NOT NULL DEFAULT 0,
                saved_comments INTEGER NOT NULL DEFAULT 0,
                saved_replies INTEGER NOT NULL DEFAULT 0,
                pinned_comments INTEGER NOT NULL DEFAULT 0,
                declared_comments INTEGER NOT NULL DEFAULT 0,
                declared_replies INTEGER NOT NULL DEFAULT 0,
                coverage REAL NOT NULL DEFAULT 0,
                failed_items_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE task_checkpoints (
                task_id TEXT PRIMARY KEY,
                root_page INTEGER NOT NULL DEFAULT 1,
                replies_json TEXT NOT NULL DEFAULT '{}',
                complete INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES video_tasks(task_id)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO video_tasks
                (task_id, video_id, video_url, status, submitted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-task", "BV1legacy", VIDEO_URL, "queued", timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO task_checkpoints
                (task_id, root_page, replies_json, complete, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-task", 2, '{"legacy-root": 3}', 0, timestamp),
        )

    database = Database(db_path)
    database.initialize()
    store = TaskStore(database)

    assert store.checkpoint("legacy-task") == {
        "root_cursor": None,
        "requested_pages": 0,
        "declared_comments": 0,
        "declared_reply_counts": {},
        "root_page": 2,
        "replies": {"legacy-root": 3},
        "complete": False,
    }

    expected = {
        "root_cursor": 9,
        "requested_pages": 6,
        "declared_comments": 50,
        "declared_reply_counts": {"legacy-root": 3},
        "root_page": 3,
        "replies": {"legacy-root": 4},
        "complete": False,
    }
    store.update_progress(
        "legacy-task",
        TaskProgress(requested_pages=6, declared_comments=50, declared_replies=3),
        checkpoint=expected,
    )

    assert store.checkpoint("legacy-task") == expected
