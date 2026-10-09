"""SQLite store: package-shipped migrations and the idempotent package upsert (SPEC §4)."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from satudatascape.normalize import format_norm, org_type, parse_ckan_ts, prioritas_years

Clock = Callable[[], datetime]
Package = dict[str, Any]

_MIGRATIONS = files("satudatascape").joinpath("migrations")


@dataclass
class UpsertStats:
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    restored: int = 0  # previously soft-deleted, seen again (counted in addition)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, conn: sqlite3.Connection, clock: Clock = _utc_now) -> None:
        self.conn = conn
        self._clock = clock

    @classmethod
    def open(cls, path: Path | str, clock: Clock = _utc_now) -> "Store":
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        store = cls(conn, clock)
        store.migrate()
        return store

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> list[str]:
        """Apply pending `migrations/NNNN_*.sql` in order; returns the versions applied."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (version TEXT PRIMARY KEY, applied_at TEXT)"
        )
        done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        scripts = sorted(
            (f for f in _MIGRATIONS.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
        )
        applied = []
        for script in scripts:
            version = script.name.removesuffix(".sql")
            if version in done:
                continue
            record = (
                "INSERT INTO schema_migrations VALUES "
                f"('{version}', '{self.now()}');"  # trusted: package file name + clock
            )
            try:
                self.conn.executescript(f"BEGIN;\n{script.read_text('utf-8')}\n{record}\nCOMMIT;")
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise
            applied.append(version)
        return applied

    def upsert_packages(self, pkgs: Iterable[Package]) -> UpsertStats:
        """Insert or update packages in one transaction; only a new hash writes a version."""
        stats = UpsertStats()
        now = self.now()
        with self.conn:
            for pkg in pkgs:
                self._upsert_one(pkg, now, stats)
        return stats

    def _upsert_one(self, pkg: Package, now: str, stats: UpsertStats) -> None:
        raw = canonical_json(pkg)
        digest = content_hash(raw)
        prev = self.conn.execute(
            "SELECT content_hash, deleted_at FROM packages WHERE id = ?", (pkg["id"],)
        ).fetchone()
        if prev is not None and prev[1] is not None:
            stats.restored += 1
        if prev is not None and prev[0] == digest:
            stats.unchanged += 1
            self.conn.execute(
                "UPDATE packages SET last_seen_at = ?, deleted_at = NULL WHERE id = ?",
                (now, pkg["id"]),
            )
            return
        if prev is None:
            stats.inserted += 1
        else:
            stats.changed += 1
        self._write_package(pkg, raw, digest, now)
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions VALUES (?, ?, ?, ?)",
            (pkg["id"], digest, raw, now),
        )
        self._replace_resources(pkg)
        self._upsert_org(pkg.get("organization"))

    def _write_package(self, pkg: Package, raw: str, digest: str, now: str) -> None:
        org = pkg.get("organization") or {}
        extras = _extras(pkg)
        self.conn.execute(
            """
            INSERT INTO packages (
                id, name, title, org_id, org_name, org_type, prioritas_years,
                harvest_source_id, harvest_source_title, metadata_created, metadata_modified,
                num_resources, private, state, raw_json, content_hash,
                first_seen_at, last_seen_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, title = excluded.title, org_id = excluded.org_id,
                org_name = excluded.org_name, org_type = excluded.org_type,
                prioritas_years = excluded.prioritas_years,
                harvest_source_id = excluded.harvest_source_id,
                harvest_source_title = excluded.harvest_source_title,
                metadata_created = excluded.metadata_created,
                metadata_modified = excluded.metadata_modified,
                num_resources = excluded.num_resources, private = excluded.private,
                state = excluded.state, raw_json = excluded.raw_json,
                content_hash = excluded.content_hash,
                last_seen_at = excluded.last_seen_at, deleted_at = NULL
            """,
            (
                pkg["id"],
                pkg.get("name"),
                pkg.get("title"),
                org.get("id") or pkg.get("owner_org"),
                org.get("name"),
                org_type(org.get("name")),
                json.dumps(
                    prioritas_years(extras.get("prioritas_tahun", pkg.get("prioritas_tahun")))
                ),
                extras.get("harvest_source_id"),
                extras.get("harvest_source_title"),
                _ts(pkg.get("metadata_created")),
                _ts(pkg.get("metadata_modified")),
                pkg.get("num_resources"),
                int(bool(pkg.get("private"))),
                pkg.get("state"),
                raw,
                digest,
                now,
                now,
            ),
        )

    def _replace_resources(self, pkg: Package) -> None:
        self.conn.execute("DELETE FROM resources WHERE package_id = ?", (pkg["id"],))
        self.conn.executemany(
            "INSERT OR REPLACE INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    res["id"],
                    pkg["id"],
                    res.get("name"),
                    res.get("url"),
                    _host(res.get("url")),
                    res.get("format"),
                    format_norm(res.get("format")),
                    _ts(res.get("metadata_modified")),
                    _ts(res.get("last_modified")),
                )
                for res in pkg.get("resources") or []
            ],
        )

    def _upsert_org(self, org: dict[str, Any] | None) -> None:
        if not org or not org.get("id"):
            return
        self.conn.execute(
            """
            INSERT INTO organizations VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, title = excluded.title, raw_json = excluded.raw_json
            """,
            (org["id"], org.get("name"), org.get("title"), canonical_json(org)),
        )

    def now(self) -> str:
        """The store clock as a UTC ISO string."""
        return self._clock().astimezone(UTC).isoformat()


def _extras(pkg: Package) -> dict[str, Any]:
    return {e.get("key"): e.get("value") for e in pkg.get("extras") or []}


def _ts(raw: str | None) -> str | None:
    """CKAN timestamp → UTC ISO with microseconds, so stored values sort as text."""
    dt = parse_ckan_ts(raw)
    return dt.isoformat(timespec="microseconds") if dt else None


def _host(url: str | None) -> str | None:
    try:
        return urlsplit(url or "").hostname
    except ValueError:
        return None
