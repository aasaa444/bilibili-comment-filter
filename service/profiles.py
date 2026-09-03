from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from .analyzer import RuleCatalog
from .db import Database

DEFAULT_FILTER_PROFILE_ID = "default-james-haters"


@dataclass(frozen=True)
class FilterProfile:
    profile_id: str
    name: str
    description: str
    catalog: RuleCatalog
    status: str
    created_at: datetime
    updated_at: datetime
    is_current: bool = False


class FilterProfileStore:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.ensure_default()

    def ensure_default(self) -> FilterProfile:
        row = self.database.execute(
            "SELECT profile_id FROM filter_profiles WHERE profile_id = ?",
            (DEFAULT_FILTER_PROFILE_ID,),
        ).fetchone()
        if row is None:
            timestamp = datetime.now(UTC).isoformat()
            catalog = RuleCatalog()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO filter_profiles
                        (profile_id, name, description, rules_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        DEFAULT_FILTER_PROFILE_ID,
                        "詹黑过滤",
                        "识别针对詹姆斯的恶意贬损、持续嘲讽和明显敌意表达",
                        json.dumps(_catalog_payload(catalog), ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
        return self.get(DEFAULT_FILTER_PROFILE_ID)

    def get(self, profile_id: str) -> FilterProfile:
        row = self.database.execute(
            "SELECT * FROM filter_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return self._from_row(row)

    def current(self) -> FilterProfile:
        row = self.database.execute(
            """
            SELECT profile_id FROM filter_profiles
            WHERE profile_id = COALESCE(
                (SELECT setting_value FROM app_settings
                 WHERE setting_key = 'active_filter_profile_id'),
                ?
            )
            LIMIT 1
            """,
            (DEFAULT_FILTER_PROFILE_ID,),
        ).fetchone()
        if row is None:
            return self.ensure_default()
        return self._from_row(
            self.database.execute(
                "SELECT * FROM filter_profiles WHERE profile_id = ?", (row["profile_id"],)
            ).fetchone(),
            is_current=True,
        )

    def list(self) -> tuple[FilterProfile, ...]:
        current_id = self.current().profile_id
        rows = self.database.execute(
            "SELECT * FROM filter_profiles WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        return tuple(
            self._from_row(row, is_current=row["profile_id"] == current_id) for row in rows
        )

    def create(
        self,
        *,
        name: str,
        description: str,
        known_terms: tuple[str, ...] = (),
        standalone_terms: tuple[str, ...] = (),
        friendly_exceptions: tuple[str, ...] = (),
        hostile_context: tuple[str, ...] = (),
        nickname_positive: tuple[str, ...] = (),
    ) -> FilterProfile:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("策略名称不能为空")
        profile_id = f"profile-{uuid4().hex}"
        timestamp = datetime.now(UTC).isoformat()
        catalog = RuleCatalog(
            version=f"rules-{profile_id}",
            known_terms=_clean_terms(known_terms),
            standalone_terms=_clean_terms(standalone_terms),
            friendly_exceptions=_clean_terms(friendly_exceptions),
            hostile_context=_clean_terms(hostile_context),
            nickname_positive=_clean_terms(nickname_positive),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO filter_profiles
                    (profile_id, name, description, rules_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    profile_id,
                    normalized_name,
                    description.strip(),
                    json.dumps(_catalog_payload(catalog), ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(profile_id)

    def activate(self, profile_id: str) -> FilterProfile:
        profile = self.get(profile_id)
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES ('active_filter_profile_id', ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (profile.profile_id, timestamp),
            )
        return replace(
            profile, updated_at=datetime.fromisoformat(timestamp), is_current=True
        )

    @staticmethod
    def _from_row(row: object, *, is_current: bool | None = None) -> FilterProfile:
        payload = json.loads(row["rules_json"])
        catalog = RuleCatalog(
            version=str(payload.get("version") or "rules-v2"),
            known_terms=tuple(payload.get("known_terms", ())),
            standalone_terms=tuple(payload.get("standalone_terms", ())),
            friendly_exceptions=tuple(payload.get("friendly_exceptions", ())),
            hostile_context=tuple(payload.get("hostile_context", ())),
            nickname_positive=tuple(payload.get("nickname_positive", ())),
        )
        return FilterProfile(
            profile_id=row["profile_id"],
            name=row["name"],
            description=row["description"],
            catalog=catalog,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            is_current=bool(is_current) if is_current is not None else False,
        )


def _catalog_payload(catalog: RuleCatalog) -> dict[str, object]:
    return {
        "version": catalog.version,
        "known_terms": list(catalog.known_terms),
        "standalone_terms": list(catalog.standalone_terms),
        "friendly_exceptions": list(catalog.friendly_exceptions),
        "hostile_context": list(catalog.hostile_context),
        "nickname_positive": list(catalog.nickname_positive),
    }


def _clean_terms(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
