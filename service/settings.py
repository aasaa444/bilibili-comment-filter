from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Database

AUTO_BLACKLIST_SETTING_KEY = "auto_blacklist_enabled"


@dataclass(frozen=True)
class BlacklistAutomationSettings:
    enabled: bool
    mode: str
    updated_at: datetime


class SettingsStore:
    """Persist service-owned switches in the authoritative local SQLite database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get_blacklist_automation(self) -> BlacklistAutomationSettings:
        row = self.database.execute(
            "SELECT setting_value, updated_at FROM app_settings WHERE setting_key = ?",
            (AUTO_BLACKLIST_SETTING_KEY,),
        ).fetchone()
        if row is None:
            return self.set_blacklist_automation(False)
        enabled = _parse_bool(row["setting_value"])
        return _settings(enabled, datetime.fromisoformat(row["updated_at"]))

    def is_blacklist_automation_enabled(self) -> bool:
        return self.get_blacklist_automation().enabled

    def set_blacklist_automation(self, enabled: bool) -> BlacklistAutomationSettings:
        timestamp = datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (
                    AUTO_BLACKLIST_SETTING_KEY,
                    "true" if enabled else "false",
                    timestamp.isoformat(),
                ),
            )
        return _settings(enabled, timestamp)


def _settings(enabled: bool, updated_at: datetime) -> BlacklistAutomationSettings:
    return BlacklistAutomationSettings(
        enabled=enabled,
        mode="local_and_official_queue" if enabled else "local_only",
        updated_at=updated_at,
    )


def _parse_bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
