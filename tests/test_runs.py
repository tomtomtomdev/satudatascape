import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from satudatascape.client import PROXY_URL, SchemaDrift
from satudatascape.crawler import (
    CountDrop,
    NeedsFullCrawl,
    reconcile,
    run_full,
    run_incremental,
    run_reconcile,
)
from satudatascape.runs import LockHeld, RunLock, finish_run, start_run
from satudatascape.store import Store
from tests.test_crawler_reconcile import FakeCatalog, client, deleted, ok

T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class DriftingCatalog(FakeCatalog):
    """A catalogue whose `package_search` serves an empty page at `empty_at`."""

    def __init__(self, n: int, *, empty_at: int | None = None) -> None:
        super().__init__(n)
        self.empty_at = empty_at

    def _search(self, params: dict[str, str]) -> httpx.Response:
        rows, start = int(params.get("rows", 10)), int(params.get("start", 0))
        if rows > 1 and start == self.empty_at:
            return ok({"count": len(self.packages), "results": []})
        return super()._search(params)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "sds.db"


@pytest.fixture
def store(db: Path, clock: MutableClock) -> Iterator[Store]:
    s = Store.open(db, clock=clock)
    yield s
    s.close()


def runs(store: Store) -> list[dict[str, Any]]:
    return [dict(r) for r in store.conn.execute("SELECT * FROM runs ORDER BY id")]


def actions(catalog: FakeCatalog) -> list[str]:
    return [a for a, _ in catalog.calls]


# --- run rows -------------------------------------------------------------------------------


def test_start_and_finish_run_write_status_and_finished_at(
    store: Store, clock: MutableClock
) -> None:
    run_id = start_run(store, "incremental")
    clock.now = T0 + timedelta(minutes=5)

    finish_run(store, run_id, "ok")

    [run] = runs(store)
    assert (run["kind"], run["status"], run["error"]) == ("incremental", "ok", None)
    assert run["errors"] == 0
    assert (run["started_at"], run["finished_at"]) == (
        T0.isoformat(),
        (T0 + timedelta(minutes=5)).isoformat(),
    )


@respx.mock
async def test_run_full_writes_an_ok_run_row_and_reconciles(store: Store) -> None:
    catalog = FakeCatalog(25)
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await run_full(client(), store, page_size=10)

    [run] = runs(store)
    assert run["id"] == result.run_id
    assert (run["kind"], run["status"], run["error"], run["finished_at"]) == (
        "full",
        "ok",
        None,
        T0.isoformat(),
    )
    assert (run["count_search"], run["count_list"], run["count_stored"]) == (25, 25, 25)
    assert actions(catalog)[0] == "package_search"  # the probe
    assert catalog.calls[0][1]["rows"] == "1"
    assert "package_list" in actions(catalog)


@respx.mock
async def test_run_row_records_failed_status_and_error_on_exception(store: Store) -> None:
    catalog = FakeCatalog(5)
    catalog.list_result = {"not": "a list"}
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with pytest.raises(SchemaDrift):
        await run_full(client(), store)

    [run] = runs(store)
    assert (run["status"], run["errors"], run["finished_at"]) == ("failed", 1, T0.isoformat())
    assert "SchemaDrift" in run["error"] and "package_list" in run["error"]


@respx.mock
async def test_run_incremental_and_run_reconcile_finish_their_rows(store: Store) -> None:
    catalog = FakeCatalog(20)
    for p in catalog.packages:
        p["metadata_modified"] = "2026-10-08T00:00:00.000000"
    store.upsert_packages(catalog.packages)
    respx.post(PROXY_URL).mock(side_effect=catalog)

    await run_incremental(client(), store)
    await run_reconcile(client(), store)

    assert [(r["kind"], r["status"]) for r in runs(store)] == [
        ("incremental", "ok"),
        ("reconcile", "ok"),
    ]
    assert all(r["finished_at"] == T0.isoformat() for r in runs(store))


async def test_run_incremental_on_an_empty_store_needs_a_full_crawl_and_writes_no_row(
    store: Store,
) -> None:
    with pytest.raises(NeedsFullCrawl):
        await run_incremental(client(), store)
    assert runs(store) == []
    assert store.conn.execute("SELECT count(*) FROM run_lock").fetchone()[0] == 0


# --- lock -----------------------------------------------------------------------------------


def test_second_concurrent_lock_is_refused(db: Path, store: Store, clock: MutableClock) -> None:
    other = Store.open(db, clock=clock)
    try:
        with RunLock(store, holder="a"), pytest.raises(LockHeld, match="held by a"):
            RunLock(other, holder="b").acquire()
        with RunLock(other, holder="b"):  # released on exit, so it can be taken again
            pass
    finally:
        other.close()


@respx.mock
async def test_a_second_concurrent_run_is_refused_without_a_run_row(store: Store) -> None:
    catalog = FakeCatalog(5)
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with RunLock(store, holder="scheduler"), pytest.raises(LockHeld):
        await run_full(client(), store)

    assert runs(store) == []
    assert catalog.calls == []


def test_stale_lock_is_taken_over_and_its_running_run_marked_failed(
    db: Path, store: Store, clock: MutableClock
) -> None:
    crashed = RunLock(store, holder="crashed")
    crashed.acquire()
    run_id = start_run(store, "full")
    crashed.attach(run_id)

    clock.now = T0 + timedelta(minutes=29)
    with pytest.raises(LockHeld):
        RunLock(store, holder="early").acquire()

    clock.now = T0 + timedelta(minutes=31)
    with RunLock(store, holder="new") as lock:
        assert lock.holder == "new"
        row = store.conn.execute("SELECT holder, acquired_at FROM run_lock").fetchone()
        assert tuple(row) == ("new", clock.now.isoformat())
    [run] = runs(store)
    assert run["status"] == "failed"
    assert "stale" in run["error"]


def test_heartbeat_keeps_the_lock_fresh(store: Store, clock: MutableClock) -> None:
    lock = RunLock(store, holder="a")
    lock.acquire()
    clock.now = T0 + timedelta(minutes=20)
    assert lock.heartbeat() is True

    clock.now = T0 + timedelta(minutes=45)  # 45 min since acquire, 25 since the heartbeat
    with pytest.raises(LockHeld):
        RunLock(store, holder="b").acquire()


async def test_async_lock_heartbeats_in_the_background(store: Store, clock: MutableClock) -> None:
    beats: list[str] = []

    async def fake_sleep(_: float) -> None:
        clock.now += timedelta(minutes=1)
        await asyncio.sleep(0)

    async with RunLock(store, holder="a", sleep=fake_sleep):
        for _ in range(3):
            await asyncio.sleep(0)
            beats.append(store.conn.execute("SELECT heartbeat_at FROM run_lock").fetchone()[0])

    assert beats[-1] > T0.isoformat()
    assert store.conn.execute("SELECT count(*) FROM run_lock").fetchone()[0] == 0


def test_release_does_not_drop_a_lock_taken_over_by_someone_else(
    store: Store, clock: MutableClock
) -> None:
    old = RunLock(store, holder="old")
    old.acquire()
    clock.now = T0 + timedelta(hours=1)
    RunLock(store, holder="new").acquire()

    old.release()
    assert old.heartbeat() is False

    assert store.conn.execute("SELECT holder FROM run_lock").fetchone()[0] == "new"


# --- drift gates ----------------------------------------------------------------------------


@respx.mock
async def test_15_percent_package_list_drop_aborts_reconcile_before_any_soft_delete(
    store: Store,
) -> None:
    catalog = FakeCatalog(100)
    store.upsert_packages(catalog.packages)
    del catalog.packages[85:]  # 15% gone from package_list
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with pytest.raises(CountDrop, match="count_list"):
        await reconcile(client(), store)

    assert deleted(store) == {}
    assert "package_show" not in actions(catalog)


@respx.mock
async def test_empty_package_list_aborts_reconcile(store: Store) -> None:
    catalog = FakeCatalog(30)
    store.upsert_packages(catalog.packages)
    catalog.list_result = []
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with pytest.raises(CountDrop):
        await run_reconcile(client(), store)

    assert deleted(store) == {}
    assert runs(store)[0]["status"] == "failed"


@respx.mock
async def test_a_10_percent_drop_is_within_the_gate(store: Store) -> None:
    catalog = FakeCatalog(100)
    store.upsert_packages(catalog.packages)
    del catalog.packages[90:]
    respx.post(PROXY_URL).mock(side_effect=catalog)

    result = await reconcile(client(), store)

    assert result.deleted == 10


@respx.mock
async def test_15_percent_count_search_drop_aborts_run_full_before_crawling(
    store: Store,
) -> None:
    catalog = FakeCatalog(100)
    respx.post(PROXY_URL).mock(side_effect=catalog)
    await run_full(client(), store, page_size=50)
    del catalog.packages[85:]
    catalog.calls.clear()

    with pytest.raises(CountDrop, match="count_search"):
        await run_full(client(), store, page_size=50)

    assert actions(catalog) == ["package_search"]  # the probe only
    assert deleted(store) == {}
    assert [r["status"] for r in runs(store)] == ["ok", "failed"]


@respx.mock
async def test_startup_probe_against_a_bad_envelope_fails_fast(store: Store) -> None:
    route = respx.post(PROXY_URL).mock(
        return_value=httpx.Response(200, json={"status": "200 OK", "message": "no result"})
    )

    with pytest.raises(SchemaDrift, match="probe"):
        await run_full(client(), store)

    assert route.call_count == 1
    [run] = runs(store)
    assert run["status"] == "failed" and "probe" in run["error"]


@respx.mock
async def test_run_full_with_empty_page_drift_mid_crawl_never_reconciles(store: Store) -> None:
    catalog = DriftingCatalog(30, empty_at=10)
    store.upsert_packages([{"id": "id-gone", "name": "pkg-gone", "resources": []}])
    respx.post(PROXY_URL).mock(side_effect=catalog)

    with pytest.raises(SchemaDrift, match="empty page"):
        await run_full(client(), store, page_size=10, workers=1)

    assert "package_list" not in actions(catalog)
    assert deleted(store) == {}
    assert runs(store)[0]["status"] == "failed"


@respx.mock
async def test_run_full_resumes_an_unfinished_full_run(store: Store) -> None:
    catalog = DriftingCatalog(30, empty_at=20)
    respx.post(PROXY_URL).mock(side_effect=catalog)
    with pytest.raises(SchemaDrift):
        await run_full(client(), store, page_size=10, workers=1)
    catalog.empty_at = None
    catalog.calls.clear()

    result = await run_full(client(), store, page_size=10, workers=1)

    [run] = runs(store)
    assert (run["id"], run["status"], run["error"]) == (result.run_id, "ok", None)
    pages = [int(p["start"]) for a, p in catalog.calls if p.get("rows") == "10"]
    assert pages == [20]
