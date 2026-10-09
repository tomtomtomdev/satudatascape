import copy
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from satudatascape.store import Store, UpsertStats

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 9, 1, 0, 0, tzinfo=UTC)


def show_pkg() -> dict[str, Any]:
    result: dict[str, Any] = json.loads((FIXTURES / "package_show.json").read_text())["result"]
    return result


def search_pkgs() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "package_search_rows2.json").read_text())
    results: list[dict[str, Any]] = data["result"]["results"]
    return results


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Iterator[Store]:
    s = Store.open(tmp_path / "sds.db", clock=clock)
    yield s
    s.close()


def row(store: Store, sql: str, *args: Any) -> sqlite3.Row:
    r: sqlite3.Row | None = store.conn.execute(sql, args).fetchone()
    assert r is not None
    return r


def count(store: Store, table: str) -> int:
    return int(row(store, f"SELECT count(*) FROM {table}")[0])


def test_migrations_are_idempotent_and_wal_is_on(tmp_path: Path) -> None:
    path = tmp_path / "sds.db"
    first = Store.open(path)
    applied = [r[0] for r in first.conn.execute("SELECT version FROM schema_migrations")]
    first.close()

    second = Store.open(path)
    assert second.migrate() == []
    again = [r[0] for r in second.conn.execute("SELECT version FROM schema_migrations")]
    mode = second.conn.execute("PRAGMA journal_mode").fetchone()[0]
    tables = {
        r[0] for r in second.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    run_cols = {r[1] for r in second.conn.execute("PRAGMA table_info(runs)")}
    second.close()

    assert applied == again == ["0001_init", "0002_runlock"]
    assert mode == "wal"
    assert {"packages", "resources", "organizations", "package_versions", "runs"} <= tables
    assert "run_lock" in tables
    assert {"status", "error", "checkpoint"} <= run_cols


def test_new_package_counts_as_inserted(store: Store) -> None:
    stats = store.upsert_packages([show_pkg()])

    assert stats == UpsertStats(inserted=1)
    pkg = row(store, "SELECT * FROM packages")
    assert pkg["name"] == "angka-kematian-ibu-aki14"
    assert pkg["first_seen_at"] == pkg["last_seen_at"] == T0.isoformat()
    assert pkg["deleted_at"] is None
    assert pkg["metadata_modified"] == "2026-07-09T12:44:39.449898+00:00"
    assert pkg["harvest_source_title"] == "Kabupaten Kampar - CKAN"
    assert json.loads(pkg["raw_json"]) == show_pkg()
    assert len(pkg["content_hash"]) == 64
    assert count(store, "package_versions") == 1


def test_identical_reupsert_changes_nothing_but_bumps_last_seen(
    store: Store, clock: FakeClock
) -> None:
    store.upsert_packages([show_pkg()])
    before = dict(row(store, "SELECT * FROM packages"))
    clock.tick(hours=1)

    reordered = dict(reversed(list(show_pkg().items())))  # key order must not matter
    stats = store.upsert_packages([reordered])

    after = dict(row(store, "SELECT * FROM packages"))
    assert stats == UpsertStats(unchanged=1)
    assert after.pop("last_seen_at") == (T0 + timedelta(hours=1)).isoformat()
    before.pop("last_seen_at")
    assert after == before
    assert count(store, "package_versions") == 1


def test_changed_field_writes_a_version_row(store: Store, clock: FakeClock) -> None:
    store.upsert_packages([show_pkg()])
    clock.tick(hours=1)
    edited = show_pkg()
    edited["title"] = "Angka kematian ibu (AKI) tahun 2026"

    stats = store.upsert_packages([edited])

    assert stats == UpsertStats(changed=1)
    assert row(store, "SELECT title FROM packages")[0] == edited["title"]
    versions = store.conn.execute(
        "SELECT raw_json, seen_at FROM package_versions ORDER BY seen_at"
    ).fetchall()
    assert [json.loads(v[0])["title"] for v in versions] == [
        "Angka kematian ibu (AKI) tahun 2025",
        "Angka kematian ibu (AKI) tahun 2026",
    ]
    assert row(store, "SELECT first_seen_at FROM packages")[0] == T0.isoformat()


def test_resources_are_replaced_when_the_list_shrinks(store: Store) -> None:
    pkg = search_pkgs()[1]
    store.upsert_packages([pkg])
    assert count(store, "resources") == 15

    shrunk = copy.deepcopy(pkg)
    shrunk["resources"] = shrunk["resources"][:2]
    shrunk["num_resources"] = 2
    store.upsert_packages([shrunk])

    ids = {r[0] for r in store.conn.execute("SELECT id FROM resources")}
    assert ids == {r["id"] for r in pkg["resources"][:2]}


def test_org_type_and_format_norm_are_populated(store: Store) -> None:
    pkgs = search_pkgs()
    stats = store.upsert_packages(pkgs)

    assert stats == UpsertStats(inserted=2)
    types = dict(store.conn.execute("SELECT name, org_type FROM packages").fetchall())
    assert types == {
        "angka-kematian-ibu-aki14": "kabupaten",
        "skpg-sumatera-barat-april-2023": "provinsi",
    }
    formats = [
        r[0]
        for r in store.conn.execute(
            "SELECT format_norm FROM resources WHERE package_id = ? ORDER BY format_norm",
            (pkgs[1]["id"],),
        )
    ]
    assert formats.count("") == 2  # blank formats normalise to "" (S3 note)
    assert set(formats) == {"", "WMS", "WFS", "JPEG"}
    res = row(store, "SELECT * FROM resources WHERE package_id = ?", pkgs[0]["id"])
    assert res["format_norm"] == "XLSX"
    assert res["url_host"] == "data.kamparkab.go.id"
    orgs = dict(store.conn.execute("SELECT name, title FROM organizations").fetchall())
    assert orgs["kabupaten-kampar"] == "Kabupaten Kampar"
    assert len(orgs) == 2


def test_prioritas_years_are_normalised_from_extras(store: Store) -> None:
    pkg = show_pkg()
    pkg["extras"].append({"key": "prioritas_tahun", "value": "2023, 2024"})
    store.upsert_packages([pkg])

    assert json.loads(row(store, "SELECT prioritas_years FROM packages")[0]) == [2023, 2024]


def test_soft_deleted_package_that_reappears_clears_deleted_at(
    store: Store, clock: FakeClock
) -> None:
    store.upsert_packages([show_pkg()])
    store.conn.execute("UPDATE packages SET deleted_at = ?", (T0.isoformat(),))
    clock.tick(days=1)

    stats = store.upsert_packages([show_pkg()])

    assert stats == UpsertStats(unchanged=1, restored=1)
    assert row(store, "SELECT deleted_at FROM packages")[0] is None
