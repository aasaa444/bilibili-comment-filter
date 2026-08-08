from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cookie_digest TEXT NOT NULL,
    cookies_json TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    source TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uid_records (
    uid TEXT PRIMARY KEY,
    nickname TEXT,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uid_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT NOT NULL,
    action TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (uid) REFERENCES uid_records(uid)
);

CREATE TABLE IF NOT EXISTS sync_versions (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO sync_versions (id, current_version) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS video_tasks (
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

CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT PRIMARY KEY,
    root_page INTEGER NOT NULL DEFAULT 1,
    replies_json TEXT NOT NULL DEFAULT '{}',
    complete INTEGER NOT NULL DEFAULT 0,
    root_cursor INTEGER,
    requested_pages INTEGER NOT NULL DEFAULT 0,
    declared_comments INTEGER NOT NULL DEFAULT 0,
    declared_reply_counts_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES video_tasks(task_id)
);

CREATE TABLE IF NOT EXISTS comments (
    task_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    nickname TEXT,
    content TEXT NOT NULL,
    video_id TEXT NOT NULL,
    comment_url TEXT NOT NULL,
    root_id TEXT NOT NULL,
    parent_id TEXT,
    level TEXT NOT NULL,
    created_at INTEGER,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    context_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (task_id, comment_id),
    FOREIGN KEY (task_id) REFERENCES video_tasks(task_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    decision TEXT NOT NULL,
    nickname TEXT,
    video_id TEXT NOT NULL,
    comment_ids_json TEXT NOT NULL,
    comments_json TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    model_version TEXT NOT NULL,
    sample_version TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, uid),
    FOREIGN KEY (task_id) REFERENCES video_tasks(task_id)
);

CREATE TABLE IF NOT EXISTS sample_sets (
    sample_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS sample_items (
    item_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE (sample_id, label, content),
    FOREIGN KEY (sample_id) REFERENCES sample_sets(sample_id)
);

CREATE TABLE IF NOT EXISTS review_actions (
    action_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    action TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS blacklist_queue (
    item_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    evidence_id TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id)
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA)
        self._migrate_task_checkpoints()
        self._connection.commit()

    def _migrate_task_checkpoints(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        additions = {
            "root_cursor": "INTEGER",
            "requested_pages": "INTEGER NOT NULL DEFAULT 0",
            "declared_comments": "INTEGER NOT NULL DEFAULT 0",
            "declared_reply_counts_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE task_checkpoints ADD COLUMN {name} {definition}"
                )

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been initialized")
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def check(self) -> tuple[bool, str]:
        try:
            self.connection.execute("SELECT 1").fetchone()
        except (sqlite3.Error, RuntimeError) as exc:
            return False, f"SQLite connection unavailable: {exc}"
        return True, "SQLite connection is available"

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @staticmethod
    def digest_cookies(cookies: dict[str, str]) -> str:
        encoded = json.dumps(cookies, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def save_auth_session(
        self,
        *,
        cookies: dict[str, str],
        status: str,
        detail: str,
        source: str,
        checked_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions
                    (cookie_digest, cookies_json, status, detail, source, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.digest_cookies(cookies),
                    json.dumps(cookies, sort_keys=True),
                    status,
                    detail,
                    source,
                    checked_at,
                ),
            )

    def latest_auth_session(self) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM auth_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def auth_session_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM auth_sessions").fetchone()
        return int(row["count"])

    def latest_auth_cookies(self) -> dict[str, str] | None:
        row = self.latest_auth_session()
        if row is None:
            return None
        return json.loads(row["cookies_json"])

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.connection.execute(query, parameters)
