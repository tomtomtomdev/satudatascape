from datetime import UTC, datetime, timedelta, timezone

import pytest

from satudatascape.normalize import format_norm, org_type, parse_ckan_ts, prioritas_years, solr_ts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("XLSX", "XLSX"),
        ("xlxs", "XLSX"),
        ("xslx", "XLSX"),
        ("xlxx", "XLSX"),
        (".xlsx", "XLSX"),
        ("xlsx ", "XLSX"),
        (" .XLSX", "XLSX"),
        ("csv/xlsx", "CSV"),
        (".csv", "CSV"),
        ("google spreadsheet", "GSHEET"),
        ("Google Spreadsheet", "GSHEET"),
        ("PDF ", "PDF"),
        ("pdf", "PDF"),
        ("wms", "WMS"),
        ("some format", "SOME FORMAT"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_format_norm(raw: str | None, expected: str) -> None:
    assert format_norm(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2023, 2024", [2023, 2024]),
        ("2025;2026", [2025, 2026]),
        ("Prioritas 2025", [2025]),
        ("2024", [2024]),
        ("2024, 2023, 2024", [2023, 2024]),
        ("1970", []),
        ("", []),
        ("Prioritas", []),
        (None, []),
    ],
)
def test_prioritas_years(raw: str | None, expected: list[int]) -> None:
    assert prioritas_years(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("kota-malang", "kota"),
        ("kabupaten-demak", "kabupaten"),
        ("kabupaten-kotawaringin-barat", "kabupaten"),
        ("provinsi-sumatera-barat", "provinsi"),
        ("kementerian-kesehatan", "kementerian"),
        ("badan-pusat-statistik", "badan"),
        ("bssn", "other"),
        ("kotabaru-something", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_org_type(name: str | None, expected: str) -> None:
    assert org_type(name) == expected


def test_parse_ckan_ts_naive_is_utc() -> None:
    got = parse_ckan_ts("2026-06-17T08:23:12.600209")
    assert got == datetime(2026, 6, 17, 8, 23, 12, 600209, tzinfo=UTC)
    assert got is not None and got.utcoffset() == timedelta(0)


def test_parse_ckan_ts_aware_converts_to_utc() -> None:
    assert parse_ckan_ts("2026-06-17T15:23:12+07:00") == datetime(
        2026, 6, 17, 8, 23, 12, tzinfo=UTC
    )


@pytest.mark.parametrize("raw", [None, ""])
def test_parse_ckan_ts_missing(raw: str | None) -> None:
    assert parse_ckan_ts(raw) is None


@pytest.mark.parametrize(
    ("dt", "expected"),
    [
        (datetime(2026, 6, 17, 8, 23, 12, 600209, tzinfo=UTC), "2026-06-17T08:23:12Z"),
        (
            datetime(2026, 6, 17, 15, 23, 12, tzinfo=timezone(timedelta(hours=7))),
            "2026-06-17T08:23:12Z",
        ),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05Z"),
    ],
)
def test_solr_ts(dt: datetime, expected: str) -> None:
    assert solr_ts(dt) == expected


def test_solr_ts_roundtrips_ckan_ts() -> None:
    parsed = parse_ckan_ts("2026-07-09T12:44:39.449898")
    assert parsed is not None
    assert solr_ts(parsed - timedelta(hours=1)) == "2026-07-09T11:44:39Z"
