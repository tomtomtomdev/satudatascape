"""Client for the data.go.id proxy (SPEC §1.1, §3.1)."""

import asyncio
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from satudatascape import __version__
from satudatascape.ratelimit import Clock, Sleep, TokenBucket

PROXY_URL = "https://data.go.id/api/proxy"
ACTION_PREFIX = "/api/3/action/"
USER_AGENT = f"satudatascape/{__version__} (+https://github.com/tomtomtomdev/satudatascape)"
TIMEOUT = httpx.Timeout(90.0, connect=10.0)
MAX_ATTEMPTS = 5
_backoff = wait_exponential_jitter(initial=1, max=60, jitter=1)


class ProxyError(Exception):
    """The proxy answered with an error: non-200, a Spring error body, or an unreadable body."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        path: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.retry_after = retry_after


class SchemaDrift(ProxyError):
    """A 200 response whose shape no longer matches what the crawler relies on."""


class ProxyClient:
    """Polite proxy client: ≤`concurrency` in flight, ≤`rps` requests/s, retries with backoff."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        rps: float = 2.0,
        concurrency: int = 4,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._bucket = TokenBucket(rps, clock=clock, sleep=sleep)
        self._slots = asyncio.Semaphore(concurrency)
        self._sleep = sleep

    async def action(self, name: str, **params: Any) -> Any:
        endpoint = ACTION_PREFIX + name
        if params:
            endpoint += "?" + urlencode(params, quote_via=quote)
        envelope = {"endpoint": endpoint, "method": "GET", "body": {}, "token": ""}
        retrying = AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_wait,
            retry=retry_if_exception(_retryable),
            sleep=self._sleep,
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await self._send(envelope)
                result = _unwrap(response, endpoint)
        if name == "package_search":
            _check_search(result)
        return result

    async def _send(self, envelope: dict[str, Any]) -> httpx.Response:
        async with self._slots:
            await self._bucket.acquire()
            return await self._post(envelope)

    async def _post(self, envelope: dict[str, Any]) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "json": envelope,
            "headers": {"User-Agent": USER_AGENT},
            "timeout": TIMEOUT,
        }
        if self._http is not None:
            return await self._http.post(PROXY_URL, **kwargs)
        async with httpx.AsyncClient() as http:
            return await http.post(PROXY_URL, **kwargs)


def _retryable(exc: BaseException) -> bool:
    """5xx, 429 and timeouts retry; a 500 wrapping CKAN's `404 NOT FOUND` (S1 finding) does not."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, SchemaDrift) or not isinstance(exc, ProxyError):
        return False
    if "404 NOT FOUND" in str(exc):
        return False
    return exc.status == 429 or exc.status >= 500


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, ProxyError) and exc.retry_after is not None:
        return exc.retry_after
    return _backoff(state)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _unwrap(response: httpx.Response, endpoint: str) -> Any:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if status != 200:
        error = _error(status, body, endpoint)
        error.retry_after = _retry_after(response)
        raise error
    if not isinstance(body, dict) or "result" not in body:
        raise SchemaDrift(f"{endpoint}: 200 response without 'result'", status=status)
    return body["result"]


def _error(status: int, body: Any, endpoint: str) -> ProxyError:
    if isinstance(body, dict) and "path" in body:
        path = str(body["path"])
        return ProxyError(
            f"HTTP {status} {body.get('error', '')} for {path}".strip(), status=status, path=path
        )
    if isinstance(body, dict) and "message" in body:
        return ProxyError(f"HTTP {status} for {endpoint}: {body['message']}", status=status)
    return ProxyError(f"HTTP {status} for {endpoint}: unreadable body", status=status)


def _check_search(result: Any) -> None:
    if not isinstance(result, dict):
        raise SchemaDrift("package_search: result is not an object", status=200)
    count = result.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise SchemaDrift("package_search: result.count missing or not an int", status=200)
    if not isinstance(result.get("results"), list):
        raise SchemaDrift("package_search: result.results missing or not a list", status=200)
