from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
    declared_total INTEGER,
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
    declared_total INTEGER,
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
    kind TEXT NOT NULL DEFAULT 'comment',
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    UNIQUE (sample_id, kind, label, content),
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
    error_category TEXT,
    failure_type TEXT,
    user_message TEXT,
    recovery_action TEXT,
    error_at TEXT,
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
        self._transaction_lock = threading.RLock()

    def initialize(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(SCHEMA)
        self._migrate_video_tasks()
        self._migrate_task_checkpoints()
        self._migrate_sample_items()
        self._migrate_blacklist_queue()
        self._connection.commit()
        self.recover_blacklist_processing()

    def _migrate_video_tasks(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(video_tasks)")
        }
        if "declared_total" not in columns:
            self.connection.execute("ALTER TABLE video_tasks ADD COLUMN declared_total INTEGER")

    def _migrate_task_checkpoints(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(task_checkpoints)")
        }
        additions = {
            "root_cursor": "INTEGER",
            "requested_pages": "INTEGER NOT NULL DEFAULT 0",
            "declared_comments": "INTEGER NOT NULL DEFAULT 0",
            "declared_total": "INTEGER",
            "declared_reply_counts_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE task_checkpoints ADD COLUMN {name} {definition}"
                )

    def _migrate_sample_items(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(sample_items)")
        }
        if "kind" not in columns:
            self.connection.execute("ALTER TABLE sample_items ADD COLUMN kind TEXT")
        if "source" not in columns:
            self.connection.execute(
                "ALTER TABLE sample_items ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
            )
        self.connection.execute(
            """
            UPDATE sample_items
            SET kind = CASE
                WHEN (
                    SELECT kind FROM sample_sets
                    WHERE sample_sets.sample_id = sample_items.sample_id
                ) = 'nickname' THEN 'nickname'
                ELSE 'comment'
            END
            WHERE kind IS NULL OR kind = ''
            """
        )
        if self._sample_items_have_legacy_unique_constraint():
            self.connection.executescript(
                """
                CREATE TABLE sample_items_migrated (
                    item_id TEXT PRIMARY KEY,
                    sample_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'comment',
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    UNIQUE (sample_id, kind, label, content),
                    FOREIGN KEY (sample_id) REFERENCES sample_sets(sample_id)
                );
                INSERT INTO sample_items_migrated
                    (item_id, sample_id, kind, label, content, source)
                SELECT item_id, sample_id, COALESCE(kind, 'comment'), label, content,
                       COALESCE(source, 'manual')
                FROM sample_items AS legacy
                WHERE legacy.rowid IN (
                    SELECT MIN(rowid)
                    FROM sample_items
                    GROUP BY sample_id, COALESCE(kind, 'comment'), label, content
                )
                ORDER BY rowid;
                DROP TABLE sample_items;
                ALTER TABLE sample_items_migrated RENAME TO sample_items;
                """
            )

    def _migrate_blacklist_queue(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(blacklist_queue)")
        }
        additions = {
            "error_category": "TEXT",
            "failure_type": "TEXT",
            "user_message": "TEXT",
            "recovery_action": "TEXT",
            "error_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE blacklist_queue ADD COLUMN {name} {definition}"
                )

    def _sample_items_have_legacy_unique_constraint(self) -> bool:
        indexes = self.connection.execute("PRAGMA index_list(sample_items)").fetchall()
        for index in indexes:
            if not index["unique"]:
                continue
            index_name = str(index["name"]).replace('"', '""')
            columns = self.connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
            names = [str(column["name"]) for column in columns]
            if names in (
                ["sample_id", "label", "content"],
                ["sample_id", "kind", "content"],
            ):
                return True
        return False

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been initialized")
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            connection = self.connection
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def recover_blacklist_processing(self) -> int:
        """Make work interrupted by a previous service instance retryable."""

        timestamp = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE blacklist_queue
                SET status = 'failed',
                    updated_at = ?,
                    last_error = COALESCE(
                        last_error,
                        'Recovered abandoned blacklist item after service restart'
                    ),
                    error_category = COALESCE(error_category, 'browser_environment'),
                    failure_type = COALESCE(failure_type, 'environment'),
                    user_message = COALESCE(
                        user_message,
                        '服务重启时发现上次拉黑中断，队列项已保留'
                    ),
                    recovery_action = COALESCE(
                        recovery_action,
                        '请确认后台 Chromium 运行环境正常后点击“重试”'
                    ),
                    error_at = COALESCE(error_at, ?),
                    completed_at = NULL
                WHERE status = 'processing'
                """,
                (timestamp, timestamp),
            )
        return cursor.rowcount

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
