"""Normalisers for dirty catalogue values (SPEC §1.4-1.5, §3.2)."""

import re
from datetime import UTC, datetime

_FORMAT_ALIASES = {
    "XLXS": "XLSX",
    "XSLX": "XLSX",
    "XLXX": "XLSX",
    "GOOGLE SPREADSHEET": "GSHEET",
}
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_MIN_YEAR, _MAX_YEAR = 2000, 2099  # "1970" is an epoch placeholder, not a priority year
_ORG_TYPES = frozenset({"kota", "kabupaten", "provinsi", "kementerian", "badan"})
_SOLR_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_norm(raw: str | None) -> str:
    """Canonical upper-case format: typos fixed, leading dot dropped, `a/b` → `a`."""
    value = (raw or "").strip().split("/")[0].strip().lstrip(".").upper()
    return _FORMAT_ALIASES.get(value, value)


def prioritas_years(raw: str | None) -> list[int]:
    """Sorted, de-duplicated priority years found in a free-text `prioritas_tahun`."""
    years = {int(y) for y in _YEAR.findall(raw or "")}
    return sorted(y for y in years if _MIN_YEAR <= y <= _MAX_YEAR)


def org_type(name: str | None) -> str:
    """Publisher type from the org `name` prefix (`kota-malang` → `kota`), else `other`."""
    prefix = (name or "").split("-", 1)[0]
    return prefix if prefix in _ORG_TYPES else "other"


def parse_ckan_ts(raw: str | None) -> datetime | None:
    """CKAN timestamp → aware UTC; naive strings are UTC (SPEC §3.2)."""
    if not raw:
        return None
    return _as_utc(datetime.fromisoformat(raw))


def solr_ts(dt: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ` for Solr range queries; naive input is taken as UTC."""
    return _as_utc(dt).strftime(_SOLR_FORMAT)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
