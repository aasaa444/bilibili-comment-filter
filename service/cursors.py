from __future__ import annotations

from typing import TypeAlias

CursorValue: TypeAlias = int | str


def normalize_cursor(value: object) -> CursorValue | None:
    """Normalize a persisted Bilibili cursor without losing opaque offsets."""

    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("cursor must be an integer or a non-empty string")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    raise ValueError("cursor must be an integer or a non-empty string")
