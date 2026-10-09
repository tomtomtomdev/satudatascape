import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from satudatascape import __version__
from satudatascape.client import PROXY_URL, ProxyClient, ProxyError, SchemaDrift
from satudatascape.ratelimit import TokenBucket


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "200 OK", "message": "Sucess", "result": result})


def bad_gateway() -> httpx.Response:
    return httpx.Response(502, text="<html>Bad Gateway</html>")


def client(clock: FakeClock, **kwargs: Any) -> ProxyClient:
    return ProxyClient(clock=clock, sleep=clock.sleep, **kwargs)


@respx.mock
async def test_retries_on_502_then_succeeds() -> None:
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(side_effect=[bad_gateway(), ok(["a"])])

    assert await client(clock).action("package_list") == ["a"]

    assert route.call_count == 2
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] >= 1


@respx.mock
async def test_retries_on_429_and_timeouts() -> None:
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(
        side_effect=[httpx.Response(429), httpx.ReadTimeout("slow"), ok(["a"])]
    )

    assert await client(clock).action("package_list") == ["a"]

    assert route.call_count == 3


@respx.mock
async def test_gives_up_after_five_attempts_in_total() -> None:
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(side_effect=lambda request: bad_gateway())

    with pytest.raises(ProxyError) as info:
        await client(clock).action("package_list")

    assert info.value.status == 502
    assert route.call_count == 5
    assert len(clock.sleeps) == 4
    assert clock.sleeps == sorted(clock.sleeps)  # backoff grows


@respx.mock
async def test_does_not_retry_spring_404() -> None:
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(
        return_value=httpx.Response(404, json={"status": 404, "path": "/api/3/action/x"})
    )

    with pytest.raises(ProxyError):
        await client(clock).action("x")

    assert route.call_count == 1
    assert clock.sleeps == []


@respx.mock
async def test_does_not_retry_500_that_wraps_404_not_found() -> None:
    # Live finding (S1): package_show for an unknown id answers HTTP 500 with this envelope.
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(
        return_value=httpx.Response(
            500,
            json={
                "status": "500 INTERNAL_SERVER_ERROR",
                "message": '404 NOT FOUND: {"success": false, "error": {"message": "Not found"}}',
            },
        )
    )

    with pytest.raises(ProxyError, match="404 NOT FOUND") as info:
        await client(clock).action("package_show", id="missing")

    assert info.value.status == 500
    assert route.call_count == 1
    assert clock.sleeps == []


@respx.mock
async def test_does_not_retry_schema_drift() -> None:
    clock = FakeClock()
    route = respx.post(PROXY_URL).mock(return_value=httpx.Response(200, json={"status": "200 OK"}))

    with pytest.raises(SchemaDrift):
        await client(clock).action("package_list")

    assert route.call_count == 1


@respx.mock
async def test_honours_retry_after() -> None:
    clock = FakeClock()
    respx.post(PROXY_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), ok(["a"])]
    )

    assert await client(clock).action("package_list") == ["a"]

    assert clock.sleeps == [7.0]


async def test_never_more_than_four_in_flight() -> None:
    clock = FakeClock()
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        for _ in range(5):
            await asyncio.sleep(0)
        in_flight -= 1
        return ok([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        proxy = client(clock, http=http, rps=1000)
        await asyncio.gather(*(proxy.action("package_list") for _ in range(20)))

    assert peak == 4


@respx.mock
async def test_ten_calls_at_two_rps_take_at_least_four_and_a_half_seconds() -> None:
    clock = FakeClock()
    respx.post(PROXY_URL).mock(return_value=ok([]))

    proxy = client(clock, rps=2)
    await asyncio.gather(*(proxy.action("package_list") for _ in range(10)))

    assert clock.now >= 4.5


async def test_token_bucket_spaces_acquires() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rps=2, clock=clock, sleep=clock.sleep)

    for _ in range(3):
        await bucket.acquire()

    assert clock.sleeps == [0.5, 0.5]


@respx.mock
async def test_sends_identifying_user_agent() -> None:
    route = respx.post(PROXY_URL).mock(return_value=ok([]))

    await ProxyClient().action("package_list")

    assert route.calls.last.request.headers["user-agent"] == (
        f"satudatascape/{__version__} (+https://github.com/tomtomtomdev/satudatascape)"
    )


@respx.mock
async def test_timeouts_connect_10s_read_90s() -> None:
    route = respx.post(PROXY_URL).mock(return_value=ok([]))

    await ProxyClient().action("package_list")

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["connect"] == 10
    assert timeout["read"] == 90


async def test_user_agent_and_timeouts_apply_to_injected_client() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ProxyClient(http=http).action("package_list")

    assert seen[0].headers["user-agent"].startswith("satudatascape/")
    assert seen[0].extensions["timeout"]["read"] == 90
    assert json.loads(seen[0].content)["method"] == "GET"
