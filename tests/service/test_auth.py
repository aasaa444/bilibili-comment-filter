import httpx
import pytest

from service.auth import AuthService, AuthStatus, AuthVerification, BilibiliAuthVerifier
from service.db import Database

FIXTURE_COOKIES = {
    "SESSDATA": "fixture-sessdata",
    "bili_jct": "fixture-jct",
}
BASE_URL = "https://api.fixture.test"
NAV_URL = f"{BASE_URL}/x/web-interface/nav"
COMPATIBLE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FixedAuthVerifier:
    def __init__(self, verification: AuthVerification) -> None:
        self.verification = verification

    def verify(self, cookies: dict[str, str]) -> AuthVerification:
        return self.verification


def test_bilibili_auth_verifier_maps_valid_response_and_sends_request_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verification = BilibiliAuthVerifier(base_url=BASE_URL, client=client).verify(
            FIXTURE_COOKIES
        )

    assert verification.status is AuthStatus.VALID
    assert verification.detail == "Bilibili session is valid"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == NAV_URL
    assert request.headers["Cookie"] == "SESSDATA=fixture-sessdata; bili_jct=fixture-jct"
    assert request.headers["Referer"] == "https://www.bilibili.com/"
    assert request.headers["Accept"] == "application/json, text/plain, */*"
    assert request.headers["User-Agent"] == COMPATIBLE_USER_AGENT
    assert "fixture-sessdata" not in verification.detail
    assert "fixture-jct" not in verification.detail


def test_bilibili_auth_verifier_accepts_custom_user_agent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verification = BilibiliAuthVerifier(
            base_url=BASE_URL,
            client=client,
            user_agent="fixture-browser/1.0",
        ).verify(FIXTURE_COOKIES)

    assert verification.status is AuthStatus.VALID
    assert requests[0].headers["User-Agent"] == "fixture-browser/1.0"


def test_bilibili_auth_verifier_maps_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": -101, "message": "fixture session rejected", "data": {}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verification = BilibiliAuthVerifier(base_url=BASE_URL, client=client).verify(
            FIXTURE_COOKIES
        )

    assert verification.status is AuthStatus.INVALID
    assert verification.detail == "fixture session rejected"


def test_bilibili_auth_verifier_rejects_non_object_json_response() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    ) as client:
        verification = BilibiliAuthVerifier(base_url=BASE_URL, client=client).verify(
            FIXTURE_COOKIES
        )

    assert verification.status is AuthStatus.VERIFICATION_FAILED
    assert verification.detail == (
        "Bilibili session verification returned an invalid JSON object"
    )


def test_bilibili_auth_verifier_maps_missing_cookies_without_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing cookies must not call Bilibili")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verification = BilibiliAuthVerifier(base_url=BASE_URL, client=client).verify({})

    assert verification.status is AuthStatus.MISSING
    assert verification.detail == "No Bilibili session was provided"


@pytest.mark.parametrize("failure", ["http_500", "timeout"])
def test_bilibili_auth_verifier_maps_transport_failures_to_verification_failed(
    failure: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http_500":
            return httpx.Response(500, text="fixture upstream failure")
        raise httpx.ReadTimeout("fixture timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verification = BilibiliAuthVerifier(base_url=BASE_URL, client=client).verify(
            FIXTURE_COOKIES
        )

    assert verification.status is AuthStatus.VERIFICATION_FAILED
    assert verification.detail.startswith("Bilibili session verification failed:")
    assert "fixture-sessdata" not in verification.detail


def test_auth_service_reports_empty_cookie_as_missing_and_not_present() -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        service = AuthService(database, BilibiliAuthVerifier(base_url=BASE_URL))
        synchronized, _ = service.synchronize(cookies={}, source="fixture")
        current, _, cookie_present = service.current()
    finally:
        database.close()

    assert synchronized.status is AuthStatus.MISSING
    assert current.status is AuthStatus.MISSING
    assert cookie_present is False


@pytest.mark.parametrize("status", [AuthStatus.INVALID, AuthStatus.VERIFICATION_FAILED])
def test_auth_service_reports_nonempty_cookie_as_present_for_failed_verification(
    status: AuthStatus,
) -> None:
    database = Database(":memory:")
    database.initialize()
    try:
        verification = AuthVerification(status, "fixture verification result")
        service = AuthService(database, FixedAuthVerifier(verification))
        synchronized, _ = service.synchronize(
            cookies=FIXTURE_COOKIES,
            source="fixture",
        )
        current, _, cookie_present = service.current()
    finally:
        database.close()

    assert synchronized.status is status
    assert current.status is status
    assert cookie_present is True
