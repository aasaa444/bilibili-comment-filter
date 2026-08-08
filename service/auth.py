from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from .db import Database
from .models import AuthStatus

DEFAULT_BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class AuthVerification:
    status: AuthStatus
    detail: str


class AuthVerifier(Protocol):
    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        """Verify a browser session at the external Bilibili protocol boundary."""


class BilibiliAuthVerifier:
    def __init__(
        self,
        base_url: str = "https://api.bilibili.com",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
        user_agent: str = DEFAULT_BILIBILI_USER_AGENT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = client
        self.user_agent = user_agent

    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        if not cookies:
            return AuthVerification(AuthStatus.MISSING, "No Bilibili session was provided")
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        try:
            request = self.client.get if self.client is not None else httpx.get
            response = request(
                f"{self.base_url}/x/web-interface/nav",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Cookie": cookie_header,
                    "Referer": "https://www.bilibili.com/",
                    "User-Agent": self.user_agent,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return AuthVerification(
                AuthStatus.VERIFICATION_FAILED,
                f"Bilibili session verification failed: {exc}",
            )
        if not isinstance(payload, dict):
            return AuthVerification(
                AuthStatus.VERIFICATION_FAILED,
                "Bilibili session verification returned an invalid JSON object",
            )
        if payload.get("code") != 0:
            return AuthVerification(
                AuthStatus.INVALID,
                str(payload.get("message") or "Bilibili rejected the session"),
            )
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("isLogin", False):
            return AuthVerification(AuthStatus.INVALID, "Bilibili session is not logged in")
        return AuthVerification(AuthStatus.VALID, "Bilibili session is valid")


class AuthService:
    def __init__(self, database: Database, verifier: AuthVerifier) -> None:
        self.database = database
        self.verifier = verifier

    def current(self) -> tuple[AuthVerification, datetime | None, bool]:
        row = self.database.latest_auth_session()
        if row is None:
            return (
                AuthVerification(AuthStatus.MISSING, "No Bilibili session has been synchronized"),
                None,
                False,
            )
        checked_at = datetime.fromisoformat(row["checked_at"])
        cookie_present = bool(self.database.latest_auth_cookies())
        status = AuthStatus(row["status"]) if cookie_present else AuthStatus.MISSING
        return AuthVerification(status, row["detail"]), checked_at, cookie_present

    def synchronize(
        self, *, cookies: dict[str, str], source: str
    ) -> tuple[AuthVerification, datetime]:
        verification = (
            AuthVerification(AuthStatus.MISSING, "No Bilibili session was provided")
            if not cookies
            else self.verifier.verify(cookies)
        )
        checked_at = datetime.now(UTC)
        self.database.save_auth_session(
            cookies=cookies,
            status=verification.status.value,
            detail=verification.detail,
            source=source,
            checked_at=checked_at.isoformat(),
        )
        return verification, checked_at

    def mark_invalid(self, detail: str, *, source: str = "runtime") -> AuthVerification:
        """Persist an invalidation observed after a session had already been accepted."""

        cookies = self.database.latest_auth_cookies() or {}
        verification = AuthVerification(AuthStatus.INVALID, detail)
        self.database.save_auth_session(
            cookies=cookies,
            status=verification.status.value,
            detail=verification.detail,
            source=source,
            checked_at=datetime.now(UTC).isoformat(),
        )
        return verification

    def is_valid(self) -> bool:
        verification, _, _ = self.current()
        return verification.status is AuthStatus.VALID
