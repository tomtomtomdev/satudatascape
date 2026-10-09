"""Catalogue crawler (SPEC §3.2, §7): full crawl, reconcile, incremental, and `run_*` wrappers."""

import asyncio
import gzip
import json
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from satudatascape.client import ProxyClient, ProxyError, SchemaDrift
from satudatascape.normalize import parse_ckan_ts, solr_ts
from satudatascape.runs import RunLock, finish_run, reopen_run, start_run
from satudatascape.store import Package, Store, UpsertStats, canonical_json

PAGE_SIZE = 1000  # the proxy caps `rows` at 1000 (SPEC §1.3)
WORKERS = 4  # pages in flight; the client's own limits still apply
RAW_KEEP = 3  # raw run directories kept by rotation
LIST_BATCH = 50_000  # package_list names inserted per executemany
OVERLAP = timedelta(hours=1)  # incremental re-fetch window below the watermark
INCREMENTAL_SORT = "metadata_modified asc, id asc"
MAX_DROP = 0.10  # drift gate: a count more than 10% below its baseline aborts (SPEC §7)
_DAY_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class FullCrawlResult:
    run_id: int
    count_search: int
    pages: int = 0  # fetched by this call (a resume skips checkpointed pages)
    upserts: int = 0
    inserted: int = 0
    changed: int = 0
    count_stored: int = 0


async def full_crawl(
    client: ProxyClient,
    store: Store,
    run_id: int | None = None,
    *,
    page_size: int = PAGE_SIZE,
    workers: int = WORKERS,
    raw_dir: Path | None = None,
    raw_keep: int = RAW_KEEP,
) -> FullCrawlResult:
    """Page `package_search` by `id asc` and upsert every package.

    Without `run_id` a new `runs(kind='full')` row is created; with one, the crawl resumes
    from that row's checkpoint. The checkpoint only advances over contiguous finished pages,
    so a crash never skips a page. An empty page before `start >= count` is `SchemaDrift`.
    Finishing the run (status, finished_at, reconcile) is left to the caller (S8 `run_full`).
    """
    if run_id is None:
        run_id = _start_run(store)
    next_start = _read_checkpoint(store, run_id)
    count = int((await client.action("package_search", rows=0))["count"])
    result = FullCrawlResult(run_id, count)
    with store.conn:
        store.conn.execute("UPDATE runs SET count_search = ? WHERE id = ?", (count, run_id))
    raw = _RawWriter.for_run(raw_dir, _run_day(store, run_id), raw_keep) if raw_dir else None
    frontier = _Frontier(next_start, page_size, count)
    starts = iter(range(next_start, count, page_size))
    errors: list[Exception] = []

    async def crawl_page(start: int) -> None:
        page = await client.action("package_search", sort="id asc", rows=page_size, start=start)
        packages: list[Package] = page["results"]
        if not packages:
            raise SchemaDrift(
                f"package_search: empty page at start={start} before count={count}", status=200
            )
        if raw is not None:
            raw.write(start, packages)
        stats = store.upsert_packages(packages)
        result.pages += 1
        result.upserts += len(packages)
        result.inserted += stats.inserted
        result.changed += stats.changed
        with store.conn:
            store.conn.execute(
                "UPDATE runs SET upserts = upserts + ?, changed = changed + ?, checkpoint = ?"
                " WHERE id = ?",
                (
                    len(packages),
                    stats.inserted + stats.changed,
                    _checkpoint(frontier.done(start), page_size),
                    run_id,
                ),
            )

    async def worker() -> None:
        while not errors and (start := next(starts, None)) is not None:
            try:
                await crawl_page(start)
            except Exception as exc:  # stop handing out pages; in-flight ones finish
                errors.append(exc)

    await asyncio.gather(*(worker() for _ in range(workers)))
    if errors:
        raise errors[0]

    result.count_stored = _count_stored(store)
    with store.conn:
        store.conn.execute(
            "UPDATE runs SET count_stored = ?, checkpoint = ? WHERE id = ?",
            (result.count_stored, _checkpoint(count, page_size), run_id),
        )
    return result


@dataclass
class ReconcileResult:
    run_id: int
    count_list: int
    missing: int = 0  # listed but not stored (or soft-deleted)
    fetched: int = 0  # missing ones `package_show` returned and were upserted
    not_found: int = 0  # missing ones `package_show` answered `404 NOT FOUND`
    deleted: int = 0  # stored but no longer listed → soft-deleted
    count_stored: int = 0


async def reconcile(
    client: ProxyClient,
    store: Store,
    run_id: int | None = None,
    *,
    workers: int = WORKERS,
    batch: int = LIST_BATCH,
    max_drop: float = MAX_DROP,
) -> ReconcileResult:
    """Diff `package_list` names against the store (SPEC §3.2 step 4).

    The names land in a temp table in batches and the diff runs in SQL. Listed names that are
    not stored (or are soft-deleted) are fetched with `package_show` and upserted; a
    `404 NOT FOUND` is skipped and counted. Only then are stored names that are no longer
    listed soft-deleted, so a rename is an update, not a delete. A list more than `max_drop`
    below the live stored count raises `CountDrop` before anything is fetched or deleted.
    Without `run_id` a `runs(kind='reconcile')` row is created; finishing it is left to the
    caller (`run_reconcile` / `run_full`).
    """
    if run_id is None:
        run_id = _start_run(store, "reconcile")
    names = await client.action("package_list")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise SchemaDrift("package_list: result is not a list of names", status=200)
    conn = store.conn
    with conn:
        conn.execute("DROP TABLE IF EXISTS temp.listed")
        conn.execute("CREATE TEMP TABLE listed (name TEXT PRIMARY KEY) WITHOUT ROWID")
        for i in range(0, len(names), batch):
            conn.executemany(
                "INSERT OR IGNORE INTO temp.listed VALUES (?)", ((n,) for n in names[i : i + batch])
            )
    del names
    try:
        result = ReconcileResult(run_id, _scalar(store, "SELECT count(*) FROM temp.listed"))
        with conn:
            conn.execute("UPDATE runs SET count_list = ? WHERE id = ?", (result.count_list, run_id))
        _gate("count_list", result.count_list, _count_stored(store), max_drop)
        missing = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM temp.listed l WHERE NOT EXISTS (SELECT 1 FROM packages p"
                " WHERE p.name = l.name AND p.deleted_at IS NULL) ORDER BY name"
            )
        ]
        result.missing = len(missing)
        stats = await _fetch_missing(client, store, missing, workers, result)
        with conn:
            result.deleted = conn.execute(
                "UPDATE packages SET deleted_at = ? WHERE deleted_at IS NULL AND NOT EXISTS"
                " (SELECT 1 FROM temp.listed l WHERE l.name = packages.name)",
                (store.now(),),
            ).rowcount
        result.count_stored = _count_stored(store)
        with conn:
            conn.execute(
                "UPDATE runs SET count_list = ?, count_stored = ?, upserts = upserts + ?,"
                " changed = changed + ? WHERE id = ?",
                (
                    result.count_list,
                    result.count_stored,
                    result.fetched,
                    stats.inserted + stats.changed,
                    run_id,
                ),
            )
    finally:
        with conn:
            conn.execute("DROP TABLE IF EXISTS temp.listed")
    return result


async def _fetch_missing(
    client: ProxyClient,
    store: Store,
    missing: list[str],
    workers: int,
    result: ReconcileResult,
) -> UpsertStats:
    """`package_show` each name through the client's limits; upsert what comes back."""
    pending = iter(missing)
    found: list[Package] = []

    async def worker() -> None:
        while (name := next(pending, None)) is not None:
            try:
                found.append(await client.action("package_show", id=name))
            except ProxyError as exc:
                if "404 NOT FOUND" not in str(exc):
                    raise
                result.not_found += 1

    await asyncio.gather(*(worker() for _ in range(workers)))
    result.fetched = len(found)
    return store.upsert_packages(found)


class NeedsFullCrawl(Exception):
    """The store has no packages, so there is no watermark to crawl from."""


@dataclass
class IncrementalResult:
    run_id: int
    watermark: datetime
    q: str
    count_search: int = 0
    pages: int = 0
    upserts: int = 0
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    count_stored: int = 0


def watermark(store: Store) -> datetime | None:
    """The newest stored `metadata_modified` (UTC), or None for an empty store."""
    row = store.conn.execute("SELECT max(metadata_modified) FROM packages").fetchone()
    return parse_ckan_ts(row[0])


def incremental_q(mark: datetime, overlap: timedelta = OVERLAP) -> str:
    """Solr range from the watermark minus the overlap (SPEC §3.2)."""
    return f"metadata_modified:[{solr_ts(mark - overlap)} TO *]"


async def incremental(
    client: ProxyClient,
    store: Store,
    run_id: int | None = None,
    *,
    page_size: int = PAGE_SIZE,
    overlap: timedelta = OVERLAP,
) -> IncrementalResult:
    """Fetch only packages modified since the watermark minus `overlap`, and upsert them.

    Pages are fetched in order on `metadata_modified asc, id asc`; the upsert is idempotent,
    so the overlap re-fetch shows up as `unchanged`, never as `changed`. An empty store has
    no watermark and raises `NeedsFullCrawl` before any request. Deletions are left to the
    weekly reconcile; finishing the run is left to the caller (S8 `run_incremental`).
    """
    mark = watermark(store)
    if mark is None:
        raise NeedsFullCrawl("no stored packages: run a full crawl first")
    if run_id is None:
        run_id = _start_run(store, "incremental")
    result = IncrementalResult(run_id, mark, incremental_q(mark, overlap))
    start, count = 0, None
    while count is None or start < count:
        page = await client.action(
            "package_search", q=result.q, sort=INCREMENTAL_SORT, rows=page_size, start=start
        )
        count = int(page["count"])
        packages: list[Package] = page["results"]
        if not packages:
            if start < count:
                raise SchemaDrift(
                    f"package_search: empty page at start={start} before count={count}",
                    status=200,
                )
            break
        stats = store.upsert_packages(packages)
        result.pages += 1
        result.upserts += len(packages)
        result.inserted += stats.inserted
        result.changed += stats.changed
        result.unchanged += stats.unchanged
        start += page_size
    result.count_search = count
    result.count_stored = _count_stored(store)
    with store.conn:
        store.conn.execute(
            "UPDATE runs SET count_search = ?, count_stored = ?, upserts = upserts + ?,"
            " changed = changed + ? WHERE id = ?",
            (
                result.count_search,
                result.count_stored,
                result.upserts,
                result.inserted + result.changed,
                run_id,
            ),
        )
    return result


class CountDrop(SchemaDrift):
    """A count fell more than the drift gate allows against its baseline (SPEC §7)."""


def _gate(what: str, count: int, baseline: int | None, max_drop: float) -> None:
    if baseline and count < baseline * (1 - max_drop):
        raise CountDrop(
            f"{what} {count} is more than {max_drop:.0%} below {baseline}: aborting, nothing"
            " soft-deleted",
            status=200,
        )


async def probe(client: ProxyClient) -> int:
    """Startup probe (SPEC §7): `package_search rows=1`; fail fast if the envelope drifted."""
    try:
        page = await client.action("package_search", rows=1)
    except SchemaDrift as exc:
        raise SchemaDrift(f"startup probe failed: {exc}", status=exc.status, path=exc.path) from exc
    return int(page["count"])


@dataclass
class FullRun:
    run_id: int
    crawl: FullCrawlResult
    reconcile: ReconcileResult


async def run_full(
    client: ProxyClient,
    store: Store,
    *,
    page_size: int = PAGE_SIZE,
    workers: int = WORKERS,
    raw_dir: Path | None = None,
    raw_keep: int = RAW_KEEP,
    max_drop: float = MAX_DROP,
) -> FullRun:
    """Probe, gate, full crawl, then reconcile, under the run lock, as one `runs` row.

    The most recent full run that did not finish `ok` is resumed from its checkpoint. A
    `count_search` more than `max_drop` below the last ok full run (or, with none, the live
    stored count) aborts before any page is fetched; any crawl error ends the run before
    reconcile, so drift never soft-deletes.
    """
    resume = _unfinished_full(store)

    async def body(run_id: int) -> FullRun:
        count = await probe(client)
        _gate("count_search", count, _search_baseline(store, run_id), max_drop)
        crawl = await full_crawl(
            client,
            store,
            run_id,
            page_size=page_size,
            workers=workers,
            raw_dir=raw_dir,
            raw_keep=raw_keep,
        )
        rec = await reconcile(client, store, run_id, workers=workers, max_drop=max_drop)
        return FullRun(run_id, crawl, rec)

    return await _supervised(store, "full", body, resume)


async def run_incremental(
    client: ProxyClient,
    store: Store,
    *,
    page_size: int = PAGE_SIZE,
    overlap: timedelta = OVERLAP,
) -> IncrementalResult:
    """Probe and incremental crawl under the run lock; `NeedsFullCrawl` writes no row."""
    if watermark(store) is None:
        raise NeedsFullCrawl("no stored packages: run a full crawl first")

    async def body(run_id: int) -> IncrementalResult:
        await probe(client)
        return await incremental(client, store, run_id, page_size=page_size, overlap=overlap)

    return await _supervised(store, "incremental", body)


async def run_reconcile(
    client: ProxyClient,
    store: Store,
    *,
    workers: int = WORKERS,
    max_drop: float = MAX_DROP,
) -> ReconcileResult:
    """Probe and `package_list` reconcile under the run lock (the weekly job)."""

    async def body(run_id: int) -> ReconcileResult:
        await probe(client)
        return await reconcile(client, store, run_id, workers=workers, max_drop=max_drop)

    return await _supervised(store, "reconcile", body)


async def _supervised[T](
    store: Store,
    kind: str,
    body: Callable[[int], Awaitable[T]],
    resume: int | None = None,
) -> T:
    """Hold the lock, open (or reopen) the run row, and close it `ok` or `failed`."""
    async with RunLock(store) as lock:
        if resume is None:
            run_id = _start_run(store, kind)
        else:
            run_id = resume
            reopen_run(store, run_id)
        lock.attach(run_id)
        try:
            result = await body(run_id)
        except BaseException as exc:
            finish_run(store, run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finish_run(store, run_id, "ok")
        return result


def _unfinished_full(store: Store) -> int | None:
    row = store.conn.execute(
        "SELECT id, status FROM runs WHERE kind = 'full' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row[0]) if row is not None and row[1] != "ok" else None


def _search_baseline(store: Store, run_id: int) -> int:
    row = store.conn.execute(
        "SELECT count_search FROM runs WHERE kind = 'full' AND status = 'ok' AND id != ?"
        " AND count_search IS NOT NULL ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row is not None else _count_stored(store)


class _Frontier:
    """The first offset not yet covered by a contiguous run of finished pages."""

    def __init__(self, start: int, page_size: int, count: int) -> None:
        self._next = start
        self._page_size = page_size
        self._count = count
        self._finished: set[int] = set()

    def done(self, start: int) -> int:
        self._finished.add(start)
        while self._next in self._finished:
            self._finished.remove(self._next)
            self._next += self._page_size
        return min(self._next, self._count)


class _RawWriter:
    """Writes `<root>/<YYYY-MM-DD>/page-<start>.jsonl.gz` (SPEC §4), one package per line."""

    def __init__(self, day_dir: Path) -> None:
        self.day_dir = day_dir

    @classmethod
    def for_run(cls, root: Path, day: str, keep: int) -> "_RawWriter":
        day_dir = root / day
        day_dir.mkdir(parents=True, exist_ok=True)
        old = sorted(p for p in root.iterdir() if p.is_dir() and _DAY_DIR.fullmatch(p.name))
        for stale in old[: max(0, len(old) - keep)]:
            if stale != day_dir:
                shutil.rmtree(stale)
        return cls(day_dir)

    def write(self, start: int, packages: Sequence[Package]) -> None:
        final = self.day_dir / f"page-{start}.jsonl.gz"
        part = final.with_name(final.name + ".part")
        with gzip.open(part, "wt", encoding="utf-8") as f:
            for package in packages:
                f.write(canonical_json(package) + "\n")
        part.replace(final)


def _start_run(store: Store, kind: str = "full") -> int:
    return start_run(store, kind, _checkpoint(0, PAGE_SIZE) if kind == "full" else None)


def _read_checkpoint(store: Store, run_id: int) -> int:
    row = store.conn.execute("SELECT checkpoint FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no runs row with id {run_id}")
    state: dict[str, Any] = json.loads(row[0]) if row[0] else {}
    return int(state.get("next_start", 0))


def _count_stored(store: Store) -> int:
    return _scalar(store, "SELECT count(*) FROM packages WHERE deleted_at IS NULL")


def _scalar(store: Store, sql: str) -> int:
    return int(store.conn.execute(sql).fetchone()[0])


def _run_day(store: Store, run_id: int) -> str:
    row = store.conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    return str(row[0])[:10]


def _checkpoint(next_start: int, page_size: int) -> str:
    return json.dumps({"next_start": next_start, "page_size": page_size})
