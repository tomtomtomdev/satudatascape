"""Run records and the single-run lock (SPEC §7; PLAN S8).

Every crawl writes a `runs` row (`status` running → ok | failed, `error`, `finished_at`).
Only one crawl runs at a time: `RunLock` holds the one row of `run_lock`, refreshes its
`heartbeat_at` while the run is alive, and a lock with no heartbeat for `STALE_AFTER` is
taken over (its run, if still `running`, is marked `failed`).
"""

import asyncio
import contextlib
import os
import socket
import uuid
from datetime import datetime, timedelta
from types import TracebackType
from typing import Literal

from satudatascape.ratelimit import Sleep
from satudatascape.store import Store

STALE_AFTER = timedelta(minutes=30)
HEARTBEAT_EVERY = 60.0  # seconds

Status = Literal["ok", "failed"]


def start_run(store: Store, kind: str, checkpoint: str | None = None) -> int:
    """Insert a `runs(status='running')` row and return its id."""
    with store.conn:
        cur = store.conn.execute(
            "INSERT INTO runs (kind, started_at, status, upserts, changed, errors, checkpoint)"
            " VALUES (?, ?, 'running', 0, 0, 0, ?)",
            (kind, store.now(), checkpoint),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def reopen_run(store: Store, run_id: int) -> None:
    """Put an unfinished run back to `running` so it can be resumed."""
    with store.conn:
        store.conn.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL WHERE id = ?",
            (run_id,),
        )


def finish_run(store: Store, run_id: int, status: Status, error: str | None = None) -> None:
    """Close a run: `status`, `finished_at`, `error`; a failure also bumps `errors`."""
    with store.conn:
        store.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ?,"
            " errors = coalesce(errors, 0) + ? WHERE id = ?",
            (status, store.now(), error, int(status == "failed"), run_id),
        )


class LockHeld(Exception):
    """Another run holds a fresh lock."""

    def __init__(self, holder: str, run_id: int | None, heartbeat_at: str) -> None:
        super().__init__(f"run lock held by {holder} (run {run_id}), last heartbeat {heartbeat_at}")
        self.holder = holder
        self.run_id = run_id
        self.heartbeat_at = heartbeat_at


def _default_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RunLock:
    """The single `run_lock` row. `with` acquires/releases; `async with` also heartbeats."""

    def __init__(
        self,
        store: Store,
        *,
        holder: str | None = None,
        stale_after: timedelta = STALE_AFTER,
        heartbeat_every: float = HEARTBEAT_EVERY,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.store = store
        self.holder = holder or _default_holder()
        self.stale_after = stale_after
        self.heartbeat_every = heartbeat_every
        self._sleep = sleep
        self._beat: asyncio.Task[None] | None = None

    def acquire(self) -> None:
        """Take the lock, or take over a stale one; raise `LockHeld` if it is fresh."""
        conn = self.store.conn
        now = self.store.now()
        conn.execute("BEGIN IMMEDIATE")  # serialises acquirers across connections
        try:
            row = conn.execute("SELECT holder, run_id, heartbeat_at FROM run_lock").fetchone()
            if row is not None:
                holder, run_id, beat = row
                if datetime.fromisoformat(now) - datetime.fromisoformat(beat) < self.stale_after:
                    raise LockHeld(holder, run_id, beat)
                conn.execute(
                    "UPDATE runs SET status = 'failed', finished_at = ?, error = ?,"
                    " errors = coalesce(errors, 0) + 1 WHERE id = ? AND status = 'running'",
                    (now, f"stale lock taken over: {holder} last heartbeat {beat}", run_id),
                )
            conn.execute(
                "INSERT OR REPLACE INTO run_lock (id, run_id, holder, acquired_at, heartbeat_at)"
                " VALUES (1, NULL, ?, ?, ?)",
                (self.holder, now, now),
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def attach(self, run_id: int) -> None:
        """Record which run holds the lock."""
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE run_lock SET run_id = ? WHERE holder = ?", (run_id, self.holder)
            )

    def heartbeat(self) -> bool:
        """Refresh `heartbeat_at`; False if the lock is no longer ours."""
        with self.store.conn:
            cur = self.store.conn.execute(
                "UPDATE run_lock SET heartbeat_at = ? WHERE holder = ?",
                (self.store.now(), self.holder),
            )
        return cur.rowcount == 1

    def release(self) -> None:
        """Drop the lock if it is still ours."""
        with self.store.conn:
            self.store.conn.execute("DELETE FROM run_lock WHERE holder = ?", (self.holder,))

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    async def __aenter__(self) -> "RunLock":
        self.acquire()
        self._beat = asyncio.create_task(self._beat_forever())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._beat is not None:
            self._beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beat
            self._beat = None
        self.release()

    async def _beat_forever(self) -> None:
        while True:
            await self._sleep(self.heartbeat_every)
            self.heartbeat()
