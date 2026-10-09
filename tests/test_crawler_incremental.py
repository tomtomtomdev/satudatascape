import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from satudatascape.client import PROXY_URL, ProxyClient, SchemaDrift
from satudatascape.crawler import NeedsFullCrawl, incremental, incremental_q, watermark
from satudatascape.normalize import parse_ckan_ts
from satudatascape.store import Store
from tests.test_crawler_full import FakeClock

T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 10, 1, 12, 0, 0)  # naive, as CKAN returns it
RANGE = re.compile(r"metadata_modified:\[(\S+)Z TO \*\]")


def pkg(i: int, modified: datetime, title: str | None = None) -> dict[str, Any]:
    return {
        "id": f"id-{i:05d}",
        "name": f"pkg-{i:05d}",
        "title": title or f"Dataset {i}",
        "metadata_modified": modified.isoformat(timespec="microseconds"),
        "resources": [],
    }


class FakeSolr:
    """`package_search` honouring the `metadata_modified:[<ts>Z TO *]` range and the sort."""

    def __init__(self, packages: list[dict[str, Any]]) -> None:
        self.packages = packages
        self.calls: list[dict[str, str]] = []
        self.drop_page_at: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = json.loads(request.content)["endpoint"]
        parts = urlsplit(endpoint)
        assert parts.path.endswith("/package_search")
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append(params)
        match = RANGE.fullmatch(params.get("q", ""))
        assert match, params
        since = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
        hits = sorted(
            (p for p in self.packages if parse_ckan_ts(p["metadata_modified"]) >= since),  # type: ignore[operator]
            key=lambda p: (p["metadata_modified"], p["id"]),
        )
        rows, start = int(params.get("rows", 10)), int(params.get("start", 0))
        results = hits[start : start + rows] if rows else []
        if start == self.drop_page_at:
            results = []
        return httpx.Response(
            200, json={"status": "200 OK", "result": {"count": len(hits), "results": results}}
        )

    def pages(self) -> list[dict[str, str]]:
        return [c for c in self.calls if c.get("rows") != "0"]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "sds.db", clock=lambda: T0)
    yield s
    s.close()


def client() -> ProxyClient:
    clock = FakeClock()
    return ProxyClient(clock=clock, sleep=clock.sleep)


def seed(store: Store) -> list[dict[str, Any]]:
    """10 packages, one minute apart: max metadata_modified = BASE + 9 min."""
    stored = [pkg(i, BASE + timedelta(minutes=i)) for i in range(10)]
    store.upsert_packages(stored)
    return stored


def title(store: Store, i: int) -> str:
    row = store.conn.execute("SELECT title FROM packages WHERE id = ?", (f"id-{i:05d}",)).fetchone()
    return str(row[0])


def test_watermark_is_max_stored_modified_and_q_subtracts_one_hour(store: Store) -> None:
    assert watermark(store) is None
    seed(store)

    mark = watermark(store)

    assert mark == (BASE + timedelta(minutes=9)).replace(tzinfo=UTC)
    assert incremental_q(mark) == "metadata_modified:[2026-10-01T11:09:00Z TO *]"


async def test_empty_store_raises_needs_full_crawl(store: Store) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(PROXY_URL)
        with pytest.raises(NeedsFullCrawl):
            await incremental(client(), store)
        assert not route.called


@respx.mock
async def test_request_carries_range_q_and_modified_sort(store: Store) -> None:
    stored = seed(store)
    solr = FakeSolr(stored)
    respx.post(PROXY_URL).mock(side_effect=solr)

    await incremental(client(), store)

    assert solr.pages()
    for call in solr.calls:
        assert call["q"] == "metadata_modified:[2026-10-01T11:09:00Z TO *]"
    for call in solr.pages():
        assert call["sort"] == "metadata_modified asc, id asc"


@respx.mock
async def test_only_packages_modified_since_the_watermark_are_upserted(store: Store) -> None:
    stored = seed(store)
    # Upstream: an old package edited with a stale timestamp (outside the window, so not
    # fetched), one edited after the watermark, and two brand-new packages.
    upstream = [dict(p) for p in stored]
    upstream[0] = pkg(0, BASE - timedelta(days=30), title="stale edit")
    upstream[5] = pkg(5, BASE + timedelta(hours=2), title="fresh edit")
    upstream += [pkg(10, BASE + timedelta(hours=3)), pkg(11, BASE + timedelta(hours=4))]
    respx.post(PROXY_URL).mock(side_effect=FakeSolr(upstream))

    result = await incremental(client(), store)

    # Window = [BASE+9min-1h, *): all 10 seeded ones except the stale edit, + 2 new.
    assert result.upserts == 11
    assert (result.inserted, result.changed) == (2, 1)
    assert title(store, 0) == "Dataset 0"  # not fetched, not touched
    assert title(store, 5) == "fresh edit"
    assert store.conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 12


@respx.mock
async def test_overlap_window_does_not_double_count_changed(store: Store) -> None:
    stored = seed(store)
    upstream = [*stored, pkg(10, BASE + timedelta(hours=1))]
    respx.post(PROXY_URL).mock(side_effect=FakeSolr(upstream))

    first = await incremental(client(), store)
    second = await incremental(client(), store)

    assert (first.upserts, first.inserted, first.changed, first.unchanged) == (11, 1, 0, 10)
    # Second run: watermark moved to BASE+1h; the overlap re-fetches 10+1 rows, all unchanged.
    assert (second.inserted, second.changed, second.unchanged) == (0, 0, second.upserts)
    runs = store.conn.execute(
        "SELECT kind, count_search, upserts, changed FROM runs ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in runs] == [
        ("incremental", 11, 11, 1),
        ("incremental", second.upserts, second.upserts, 0),
    ]


@respx.mock
async def test_pages_through_more_than_one_page(store: Store) -> None:
    seed(store)
    upstream = [pkg(100 + i, BASE + timedelta(hours=1, seconds=i)) for i in range(25)]
    solr = FakeSolr(upstream)
    respx.post(PROXY_URL).mock(side_effect=solr)

    result = await incremental(client(), store, page_size=10)

    assert [int(c["start"]) for c in solr.pages()] == [0, 10, 20]
    assert (result.count_search, result.inserted, result.pages) == (25, 25, 3)


@respx.mock
async def test_empty_page_before_count_is_schema_drift(store: Store) -> None:
    seed(store)
    upstream = [pkg(100 + i, BASE + timedelta(hours=1, seconds=i)) for i in range(25)]
    solr = FakeSolr(upstream)
    solr.drop_page_at = 10
    respx.post(PROXY_URL).mock(side_effect=solr)

    with pytest.raises(SchemaDrift, match="start=10"):
        await incremental(client(), store, page_size=10)
