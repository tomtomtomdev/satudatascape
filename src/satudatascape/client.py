"""Client for the data.go.id proxy (SPEC §1.1, §3.1)."""

from typing import Any
from urllib.parse import quote, urlencode

import httpx

PROXY_URL = "https://data.go.id/api/proxy"
ACTION_PREFIX = "/api/3/action/"


class ProxyError(Exception):
    """The proxy answered with an error: non-200, a Spring error body, or an unreadable body."""

    def __init__(self, message: str, *, status: int, path: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class SchemaDrift(ProxyError):
    """A 200 response whose shape no longer matches what the crawler relies on."""


class ProxyClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def action(self, name: str, **params: Any) -> Any:
        endpoint = ACTION_PREFIX + name
        if params:
            endpoint += "?" + urlencode(params, quote_via=quote)
        envelope = {"endpoint": endpoint, "method": "GET", "body": {}, "token": ""}
        response = await self._post(envelope)
        result = _unwrap(response, endpoint)
        if name == "package_search":
            _check_search(result)
        return result

    async def _post(self, envelope: dict[str, Any]) -> httpx.Response:
        if self._http is not None:
            return await self._http.post(PROXY_URL, json=envelope)
        async with httpx.AsyncClient() as http:
            return await http.post(PROXY_URL, json=envelope)


def _unwrap(response: httpx.Response, endpoint: str) -> Any:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if status != 200:
        raise _error(status, body, endpoint)
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
