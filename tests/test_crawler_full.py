import asyncio
import gzip
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from satudatascape.client import PROXY_URL, ProxyClient, ProxyError, SchemaDrift
from satudatascape.crawler import full_crawl
from satudatascape.store import Store

T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def pkg(i: int) -> dict[str, Any]:
    return {"id": f"id-{i:05d}", "name": f"pkg-{i:05d}", "title": f"Dataset {i}", "resources": []}


class FakeIndex:
    """A `package_search` index served through respx; records each request's params."""

    def __init__(self, n: int, fail: Callable[[int], httpx.Response | None] | None = None):
        self.packages = [pkg(i) for i in range(n)]
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = json.loads(request.content)["endpoint"]
        params = {k: v[0] for k, v in parse_qs(urlsplit(endpoint).query).items()}
        self.calls.append(params)
        rows, start = int(params.get("rows", 10)), int(params.get("start", 0))
        if rows > 0 and self.fail and (failure := self.fail(start)) is not None:
            return failure
        results = self.packages[start : start + rows] if rows else []
        return httpx.Response(
            200,
            json={"status": "200 OK", "result": {"count": len(self.packages), "results": results}},
        )

    def starts(self) -> list[int]:
        return sorted(int(c["start"]) for c in self.calls if c.get("rows") != "0")


def spring_400() -> httpx.Response:
    return httpx.Response(400, json={"status": 400, "error": "Bad Request", "path": "/x"})


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "sds.db", clock=lambda: T0)
    yield s
    s.close()


def client() -> ProxyClient:
    clock = FakeClock()
    return ProxyClient(clock=clock, sleep=clock.sleep)


def stored(store: Store) -> int:
    return int(store.conn.execute("SELECT count(*) FROM packages").fetchone()[0])


def run_row(store: Store, run_id: int) -> dict[str, Any]:
    r = store.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert r is not None
    return dict(r)


def checkpoint(store: Store, run_id: int) -> int:
    return int(json.loads(run_row(store, run_id)["checkpoint"])["next_start"])


def only_run_id(store: Store) -> int:
    return int(store.conn.execute("SELECT id FROM runs").fetchone()[0])


@respx.mock
async def test_full_crawl_pages_by_id_and_stores_every_package(store: Store) -> None:
    index = FakeIndex(2500)
    respx.post(PROXY_URL).mock(side_effect=index)

    result = await full_crawl(client(), store)

    assert index.calls[0] == {"rows": "0"}
    pages = [c for c in index.calls if c.get("rows") != "0"]
    assert sorted(int(c["start"]) for c in pages) == [0, 1000, 2000]
    assert all(c["sort"] == "id asc" and c["rows"] == "1000" for c in pages)
    assert stored(store) == 2500
    assert (result.count_search, result.pages, result.inserted) == (2500, 3, 2500)
    run = run_row(store, result.run_id)
    assert run["kind"] == "full"
    assert run["started_at"] == T0.isoformat()
    assert (run["count_search"], run["count_stored"], run["upserts"]) == (2500, 2500, 2500)
    assert checkpoint(store, result.run_id) == 2500


@respx.mock
async def test_crash_after_page_two_resumes_from_the_runs_checkpoint(store: Store) -> None:
    crashing = FakeIndex(2500, fail=lambda start: spring_400() if start == 2000 else None)
    respx.post(PROXY_URL).mock(side_effect=crashing)

    with pytest.raises(ProxyError):
        await full_crawl(client(), store)

    run_id = only_run_id(store)
    assert checkpoint(store, run_id) == 2000
    assert stored(store) == 2000

    healthy = FakeIndex(2500)
    respx.post(PROXY_URL).mock(side_effect=healthy)
    result = await full_crawl(client(), store, run_id)

    assert result.run_id == run_id
    assert healthy.starts() == [2000]
    assert stored(store) == 2500
    assert checkpoint(store, run_id) == 2500
    assert run_row(store, run_id)["upserts"] == 2500


@respx.mock
async def test_checkpoint_only_advances_over_contiguous_pages(store: Store) -> None:
    index = FakeIndex(2500, fail=lambda start: spring_400() if start == 0 else None)
    respx.post(PROXY_URL).mock(side_effect=index)

    with pytest.raises(ProxyError):
        await full_crawl(client(), store)

    assert checkpoint(store, only_run_id(store)) == 0


@respx.mock
async def test_empty_page_before_the_end_is_schema_drift(store: Store) -> None:
    def empty_page(start: int) -> httpx.Response | None:
        if start != 1000:
            return None
        return httpx.Response(200, json={"result": {"count": 2500, "results": []}})

    respx.post(PROXY_URL).mock(side_effect=FakeIndex(2500, fail=empty_page))

    with pytest.raises(SchemaDrift, match="start=1000"):
        await full_crawl(client(), store)

    assert checkpoint(store, only_run_id(store)) <= 1000


@respx.mock
async def test_raw_jsonl_gz_written_per_page_when_enabled(store: Store, tmp_path: Path) -> None:
    respx.post(PROXY_URL).mock(side_effect=FakeIndex(1500))
    raw = tmp_path / "raw"

    await full_crawl(client(), store, raw_dir=raw)

    day = raw / "2026-10-09"
    assert sorted(p.name for p in day.iterdir()) == ["page-0.jsonl.gz", "page-1000.jsonl.gz"]
    with gzip.open(day / "page-1000.jsonl.gz", "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    assert [p["id"] for p in lines] == [f"id-{i:05d}" for i in range(1000, 1500)]


@respx.mock
async def test_no_raw_files_when_disabled(store: Store, tmp_path: Path) -> None:
    respx.post(PROXY_URL).mock(side_effect=FakeIndex(10))

    await full_crawl(client(), store)

    assert not (tmp_path / "raw").exists()


@respx.mock
async def test_raw_rotation_keeps_only_the_last_n_run_directories(
    store: Store, tmp_path: Path
) -> None:
    respx.post(PROXY_URL).mock(side_effect=FakeIndex(10))
    raw = tmp_path / "raw"
    for day in range(1, 9):
        (raw / f"2026-10-0{day}").mkdir(parents=True)
    (raw / "keep-me").mkdir()

    await full_crawl(client(), store, raw_dir=raw, raw_keep=3)

    assert sorted(p.name for p in raw.iterdir()) == [
        "2026-10-07",
        "2026-10-08",
        "2026-10-09",
        "keep-me",
    ]
