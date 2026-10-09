import json
import resource
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from satudatascape.client import PROXY_URL, ProxyClient, SchemaDrift
from satudatascape.crawler import full_crawl, reconcile
from satudatascape.store import Store
from tests.test_crawler_full import FakeClock, pkg

T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)


class FakeCatalog:
    """`package_search` / `package_list` / `package_show` over one mutable package list."""

    def __init__(self, n: int, *, shift_at: int | None = None) -> None:
        self.packages = [pkg(i) for i in range(n)]
        self.shift_at = shift_at  # insert a new first package when this page is served
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.list_result: Any = None  # override the package_list result (drift tests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = json.loads(request.content)["endpoint"]
        parts = urlsplit(endpoint)
        action = parts.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((action, params))
        if action == "package_search":
            return self._search(params)
        if action == "package_list":
            names = [p["name"] for p in self.packages]
            return ok(names if self.list_result is None else self.list_result)
        if action == "package_show":
            found = [p for p in self.packages if params["id"] in (p["id"], p["name"])]
            if found:
                return ok(found[0])
            return httpx.Response(
                500,
                json={
                    "status": "500 INTERNAL_SERVER_ERROR",
                    "message": f'404 NOT FOUND: "{{"success": false}}" for {params["id"]}',
                },
            )
        raise AssertionError(f"unexpected action {action}")

    def _search(self, params: dict[str, str]) -> httpx.Response:
        rows, start = int(params.get("rows", 10)), int(params.get("start", 0))
        count = len(self.packages)
        if rows and start == self.shift_at:
            self.packages.insert(0, {"id": "id-0000-new", "name": "pkg-new", "resources": []})
        results = self.packages[start : start + rows] if rows else []
        return ok({"count": count, "results": results})

    def shows(self) -> list[str]:
        return sorted(p["id"] for a, p in self.calls if a == "package_show")


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "200 OK", "message": "Sucess", "result": result})


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store.open(tmp_path / "sds.db", clock=lambda: T0)
    yield s
    s.close()


def client() -> ProxyClient:
    clock = FakeClock()
    return ProxyClient(clock=clock, sleep=clock.sleep)


def live_names(store: Store) -> set[str]:
    rows = store.conn.execute("SELECT name FROM packages WHERE deleted_at IS NULL")
    return {r[0] for r in rows}


def deleted(store: Store) -> dict[str, str]:
    rows = store.conn.execute("SELECT name, deleted_at FROM packages WHERE deleted_at IS NOT NULL")
    return {r[0]: r[1] for r in rows}


@respx.mock
async def test_items_inserted_mid_crawl_are_fetched_with_package_show(store: Store) -> None:
    catalog = FakeCatalog(25, shift_at=20)
    respx.post(PROXY_URL).mock(side_effect=catalog)
    await full_crawl(client(), store, page_size=10)
    skipped = {p["name"] for p in catalog.packages} - live_names(store)
    assert skipped == {"pkg-new"}  # inserted ahead of pages already served

    result = await reconcile(client(), store)

    assert catalog.shows() == ["pkg-new"]
    assert (result.count_list, result.missing, result.fetched, result.deleted) == (26, 1, 1, 0)
    assert live_names(store) == {p["name"] for p in catalog.packages}
    assert result.count_stored == 26


@respx.mock
async def test_a_name_no_longer_listed_is_soft_deleted(store: Store) -> None:
    catalog = FakeCatalog(5)
    store.upsert_packages(catalog.packages)
    gone = catalog.packages.pop(2)
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    assert (result.fetched, result.deleted) == (0, 1)
    assert deleted(store) == {gone["name"]: T0.isoformat()}
    assert store.conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 5  # kept
    assert result.count_stored == 4


@respx.mock
async def test_in_sync_is_a_no_op(store: Store) -> None:
    catalog = FakeCatalog(5)
    store.upsert_packages(catalog.packages)
    before = store.conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    assert [a for a, _ in catalog.calls] == ["package_list"]
    assert (result.missing, result.fetched, result.not_found, result.deleted) == (0, 0, 0, 0)
    after = store.conn.execute("SELECT * FROM packages ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


@respx.mock
async def test_listed_name_that_package_show_cannot_find_is_skipped_and_counted(
    store: Store,
) -> None:
    catalog = FakeCatalog(3)
    store.upsert_packages(catalog.packages)
    catalog.list_result = [*(p["name"] for p in catalog.packages), "ghost"]
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    assert catalog.shows() == ["ghost"]  # one request: the wrapped 404 is not retried
    assert (result.missing, result.fetched, result.not_found, result.deleted) == (1, 0, 1, 0)
    assert len(live_names(store)) == 3


@respx.mock
async def test_soft_deleted_package_listed_again_is_restored(store: Store) -> None:
    catalog = FakeCatalog(3)
    store.upsert_packages(catalog.packages)
    store.conn.execute("UPDATE packages SET deleted_at = 'x' WHERE name = 'pkg-00001'")
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    assert catalog.shows() == ["pkg-00001"]
    assert (result.fetched, result.deleted) == (1, 0)
    assert deleted(store) == {}


@respx.mock
async def test_package_list_that_is_not_a_list_of_names_is_drift_and_deletes_nothing(
    store: Store,
) -> None:
    catalog = FakeCatalog(3)
    store.upsert_packages(catalog.packages)
    catalog.list_result = {"names": ["pkg-00000"]}
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with pytest.raises(SchemaDrift, match="package_list"):
        await reconcile(client(), store)

    assert deleted(store) == {}


@respx.mock
async def test_reconcile_records_list_and_stored_counts_on_the_run(store: Store) -> None:
    catalog = FakeCatalog(4)
    store.upsert_packages(catalog.packages[:3])
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    run = dict(store.conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone())
    assert run["kind"] == "reconcile"
    assert (run["count_list"], run["count_stored"], run["upserts"], run["changed"]) == (4, 4, 1, 1)


@respx.mock
async def test_reconcile_reuses_a_given_run(store: Store) -> None:
    catalog = FakeCatalog(2)
    respx.post(PROXY_URL).mock(side_effect=catalog)
    full = await full_crawl(client(), store)

    result = await reconcile(client(), store, full.run_id)

    assert result.run_id == full.run_id
    assert store.conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert store.conn.execute("SELECT count_list FROM runs").fetchone()[0] == 2


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


@pytest.mark.slow
@respx.mock
async def test_600k_names_diff_under_10s_and_300mb(store: Store) -> None:
    n = 600_000
    with store.conn:
        store.conn.executemany(
            "INSERT INTO packages (id, name, raw_json, content_hash) VALUES (?, ?, '{}', '')",
            ((f"id-{i:06d}", f"pkg-{i:06d}") for i in range(n)),
        )
    names = [f"pkg-{i:06d}" for i in range(1, n)] + ["pkg-new"]  # one removed, one new
    body = json.dumps({"status": "200 OK", "result": names}).encode()
    del names

    def handler(request: httpx.Request) -> httpx.Response:
        action = urlsplit(json.loads(request.content)["endpoint"]).path.rsplit("/", 1)[-1]
        if action == "package_list":
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})
        return ok({"id": "id-new", "name": "pkg-new", "resources": []})

    respx.post(PROXY_URL).mock(side_effect=handler)

    started = time.perf_counter()
    result = await reconcile(client(), store)
    elapsed = time.perf_counter() - started
    peak = _peak_rss_mb()
    print(f"\nreconcile 600k: {elapsed:.2f} s, peak RSS {peak:.0f} MB")

    assert (result.count_list, result.fetched, result.deleted) == (n, 1, 1)
    assert elapsed < 10
    assert peak < 300
