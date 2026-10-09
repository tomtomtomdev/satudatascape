import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from satudatascape.client import PROXY_URL, ProxyClient, ProxyError, SchemaDrift

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "200 OK", "message": "Sucess", "result": result})


@respx.mock
async def test_posts_envelope_with_encoded_endpoint() -> None:
    route = respx.post(PROXY_URL).mock(
        return_value=httpx.Response(200, json=fixture("package_search_rows2.json"))
    )

    await ProxyClient().action("package_search", rows=1000, start=0, sort="id asc", q="a&b")

    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "endpoint": "/api/3/action/package_search?rows=1000&start=0&sort=id%20asc&q=a%26b",
        "method": "GET",
        "body": {},
        "token": "",
    }
    assert route.calls.last.request.headers["content-type"] == "application/json"


@respx.mock
async def test_no_params_means_no_query_string() -> None:
    route = respx.post(PROXY_URL).mock(return_value=ok([]))

    await ProxyClient().action("package_list")

    assert json.loads(route.calls.last.request.content)["endpoint"] == "/api/3/action/package_list"


@respx.mock
async def test_unwraps_package_search() -> None:
    body = fixture("package_search_rows2.json")
    respx.post(PROXY_URL).mock(return_value=httpx.Response(200, json=body))

    result = await ProxyClient().action("package_search", rows=2)

    assert result == body["result"]
    assert result["count"] > 0
    assert len(result["results"]) == 2


@respx.mock
async def test_unwraps_package_show() -> None:
    body = fixture("package_show.json")
    respx.post(PROXY_URL).mock(return_value=httpx.Response(200, json=body))

    result = await ProxyClient().action("package_show", id=body["result"]["id"])

    assert result == body["result"]
    assert "resources" in result


@respx.mock
async def test_unwraps_package_list() -> None:
    body = fixture("package_list_5.json")
    respx.post(PROXY_URL).mock(return_value=httpx.Response(200, json=body))

    result = await ProxyClient().action("package_list", limit=5)

    assert result == body["result"]
    assert len(result) == 5


@respx.mock
async def test_spring_404_raises_proxy_error_with_path() -> None:
    respx.post(PROXY_URL).mock(return_value=httpx.Response(404, json=fixture("spring_404.json")))

    with pytest.raises(ProxyError, match="/api/3/action/group_list") as info:
        await ProxyClient().action("group_list")

    assert info.value.status == 404
    assert info.value.path == "/api/3/action/group_list"


@respx.mock
async def test_non_200_envelope_raises_proxy_error_with_message() -> None:
    respx.post(PROXY_URL).mock(
        return_value=httpx.Response(
            500, json={"status": "500 INTERNAL_SERVER_ERROR", "message": "404 NOT FOUND: x"}
        )
    )

    with pytest.raises(ProxyError, match="404 NOT FOUND") as info:
        await ProxyClient().action("package_show", id="missing")

    assert info.value.status == 500


@respx.mock
async def test_non_json_body_raises_proxy_error() -> None:
    respx.post(PROXY_URL).mock(return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(ProxyError, match="unreadable body") as info:
        await ProxyClient(sleep=no_sleep).action("package_list")  # 502 retries (S2)

    assert info.value.status == 502


@respx.mock
async def test_200_without_result_raises_schema_drift() -> None:
    respx.post(PROXY_URL).mock(
        return_value=httpx.Response(200, json={"status": "200 OK", "message": "Sucess"})
    )

    with pytest.raises(SchemaDrift, match="result"):
        await ProxyClient().action("package_list")


@respx.mock
async def test_200_non_object_body_raises_schema_drift() -> None:
    respx.post(PROXY_URL).mock(return_value=httpx.Response(200, json=["not", "an", "envelope"]))

    with pytest.raises(SchemaDrift):
        await ProxyClient().action("package_list")


@pytest.mark.parametrize(
    "result",
    [
        {"results": []},
        {"count": 3},
        {"count": "3", "results": []},
        {"count": 3, "results": {}},
        [],
    ],
    ids=["no-count", "no-results", "count-not-int", "results-not-list", "not-a-dict"],
)
@respx.mock
async def test_package_search_without_count_or_results_raises_schema_drift(result: Any) -> None:
    respx.post(PROXY_URL).mock(return_value=ok(result))

    with pytest.raises(SchemaDrift, match="package_search"):
        await ProxyClient().action("package_search", rows=0)


@respx.mock
async def test_uses_injected_http_client() -> None:
    respx.post(PROXY_URL).mock(return_value=ok(["a"]))

    async with httpx.AsyncClient() as http:
        assert await ProxyClient(http=http).action("package_list") == ["a"]
        assert not http.is_closed


@pytest.mark.live
async def test_live_package_search_count() -> None:
    result = await ProxyClient().action("package_search", rows=1)

    assert result["count"] > 500_000
    assert len(result["results"]) == 1
