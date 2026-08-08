import httpx
import pytest

from service.auth import AuthStatus, BilibiliAuthVerifier

FIXTURE_COOKIES = {
    "SESSDATA": "fixture-sessdata",
    "bili_jct": "fixture-jct",
}
BASE_URL = "https://api.fixture.test"
NAV_URL = f"{BASE_URL}/x/web-interface/nav"


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
    assert "fixture-sessdata" not in verification.detail
    assert "fixture-jct" not in verification.detail


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
