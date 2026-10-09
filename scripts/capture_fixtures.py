"""Capture trimmed proxy responses into tests/fixtures/ (one-off, makes 4 live requests).

Usage: uv run python scripts/capture_fixtures.py
Each fixture is the raw proxy response body (envelope included), pretty-printed.
"""

import json
from pathlib import Path
from typing import Any

import httpx

PROXY_URL = "https://data.go.id/api/proxy"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
HEADERS = {"User-Agent": "satudatascape-fixtures (+https://github.com/tomtomtomdev/satudatascape)"}


def post(client: httpx.Client, endpoint: str, expect: int) -> Any:
    envelope = {"endpoint": endpoint, "method": "GET", "body": {}, "token": ""}
    response = client.post(PROXY_URL, json=envelope, headers=HEADERS)
    if response.status_code != expect:
        raise SystemExit(f"{endpoint}: HTTP {response.status_code}, expected {expect}")
    return response.json()


def save(name: str, body: Any) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(FIXTURES.parent.parent)} ({path.stat().st_size} bytes)")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(90, connect=10)) as client:
        search = post(client, "/api/3/action/package_search?rows=2&start=0&sort=id%20asc", 200)
        save("package_search_rows2.json", search)
        first_id = search["result"]["results"][0]["id"]
        save("package_show.json", post(client, f"/api/3/action/package_show?id={first_id}", 200))
        save("package_list_5.json", post(client, "/api/3/action/package_list?limit=5", 200))
        save("spring_404.json", post(client, "/api/3/action/group_list", 404))


if __name__ == "__main__":
    main()
