"""Catalogue crawler (SPEC §3.2): full crawl with a resumable checkpoint in `runs.checkpoint`."""

import asyncio
import gzip
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from satudatascape.client import ProxyClient, SchemaDrift
from satudatascape.store import Package, Store, canonical_json

PAGE_SIZE = 1000  # the proxy caps `rows` at 1000 (SPEC §1.3)
WORKERS = 4  # pages in flight; the client's own limits still apply
RAW_KEEP = 3  # raw run directories kept by rotation
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

    result.count_stored = int(
        store.conn.execute("SELECT count(*) FROM packages WHERE deleted_at IS NULL").fetchone()[0]
    )
    with store.conn:
        store.conn.execute(
            "UPDATE runs SET count_stored = ?, checkpoint = ? WHERE id = ?",
            (result.count_stored, _checkpoint(count, page_size), run_id),
        )
    return result


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


def _start_run(store: Store) -> int:
    with store.conn:
        cur = store.conn.execute(
            "INSERT INTO runs (kind, started_at, status, upserts, changed, errors, checkpoint)"
            " VALUES ('full', ?, 'running', 0, 0, 0, ?)",
            (store.now(), _checkpoint(0, PAGE_SIZE)),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _read_checkpoint(store: Store, run_id: int) -> int:
    row = store.conn.execute("SELECT checkpoint FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no runs row with id {run_id}")
    state: dict[str, Any] = json.loads(row[0]) if row[0] else {}
    return int(state.get("next_start", 0))


def _run_day(store: Store, run_id: int) -> str:
    row = store.conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    return str(row[0])[:10]


def _checkpoint(next_start: int, page_size: int) -> str:
    return json.dumps({"next_start": next_start, "page_size": page_size})
