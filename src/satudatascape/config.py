"""Settings from `satudatascape.toml` with `SDS_*` env overrides (SPEC §6).

Precedence: defaults < TOML file < environment. Top-level keys map to `SDS_<KEY>`; a
section key maps to `SDS_<SECTION>_<KEY>` (e.g. `SDS_DOWNLOAD_MAX_SIZE`). Any unknown key,
in the file or in an `SDS_*` variable, is an error rather than silently ignored.
"""

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, get_type_hints

from satudatascape.client import PROXY_URL, USER_AGENT
from satudatascape.crawler import OVERLAP, PAGE_SIZE, RAW_KEEP, WORKERS
from satudatascape.runs import STALE_AFTER

ENV_PREFIX = "SDS_"

_DURATION_PART = re.compile(r"(\d+)([dhms])")
_DURATION = re.compile(r"(?:\d+[dhms])+")
_DURATION_UNITS = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
_SIZE = re.compile(r"(\d+)\s*(b|kb|mb|gb|kib|mib|gib)?")
_SIZE_UNITS = {
    None: 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """A config file or `SDS_*` variable that can't be loaded."""


def parse_duration(value: str | int) -> timedelta:
    """`"1h"`, `"30m"`, `"1h30m"`, `"1d"`, `"90s"`, or whole seconds as an int."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return timedelta(seconds=value)
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _DURATION.fullmatch(text):
        raise ConfigError(f"bad duration {value!r} (want e.g. '1h', '30m', '1h30m')")
    parts = {_DURATION_UNITS[unit]: 0 for unit in _DURATION_UNITS}
    for number, unit in _DURATION_PART.findall(text):
        parts[_DURATION_UNITS[unit]] += int(number)
    return timedelta(**parts)


def parse_size(value: str | int) -> int:
    """`"500MB"` → 500_000_000 bytes (KB/MB/GB are decimal, KiB/MiB/GiB binary)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    text = value.strip().lower() if isinstance(value, str) else ""
    match = _SIZE.fullmatch(text)
    if not match:
        raise ConfigError(f"bad size {value!r} (want e.g. '500MB', '2GiB')")
    return int(match[1]) * _SIZE_UNITS[match[2]]


def _size(**kw: Any) -> Any:
    return field(metadata={"parse": "size"}, **kw)


def _positive(default: Any) -> Any:
    return field(default=default, metadata={"positive": True})


@dataclass
class DownloadSettings:
    per_host_concurrency: int = _positive(1)
    global_concurrency: int = _positive(8)
    max_size: int = _size(default=parse_size("500MB"))
    insecure_hosts: list[str] = field(default_factory=list)


@dataclass
class SchedulerSettings:
    enabled: bool = True


@dataclass
class AuthSettings:
    user: str | None = None
    password: str | None = None


@dataclass
class BackupSettings:
    retention: int = _positive(7)  # nightly copies kept


@dataclass
class Settings:
    base_url: str = PROXY_URL
    concurrency: int = _positive(4)
    rps: float = _positive(2.0)
    connect_timeout: float = _positive(10.0)  # seconds
    read_timeout: float = _positive(90.0)  # seconds
    user_agent: str = USER_AGENT
    page_size: int = _positive(PAGE_SIZE)
    workers: int = _positive(WORKERS)
    overlap: timedelta = OVERLAP
    raw_dir: Path = Path("data/raw")
    raw_keep: int = _positive(RAW_KEEP)
    lock_stale_after: timedelta = STALE_AFTER
    db: Path = Path("data/satudata.db")
    download: DownloadSettings = field(default_factory=DownloadSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)


_SECTIONS: dict[str, type[Any]] = {
    "download": DownloadSettings,
    "scheduler": SchedulerSettings,
    "auth": AuthSettings,
    "backup": BackupSettings,
}


def load(path: Path | None, env: Mapping[str, str] | None = None) -> Settings:
    """Defaults, then the TOML file at `path` (if any), then `SDS_*` from `env` (default:
    `os.environ`). Raises `ConfigError` naming the offending file, key or variable."""
    top: dict[str, Any] = {}
    sections: dict[str, dict[str, Any]] = {name: {} for name in _SECTIONS}
    if path is not None:
        _apply_file(path, top, sections)
    _apply_env(os.environ if env is None else env, top, sections)
    built = {name: replace(cls(), **sections[name]) for name, cls in _SECTIONS.items()}
    return replace(Settings(), **top, **built)


def _apply_file(path: Path, top: dict[str, Any], sections: dict[str, dict[str, Any]]) -> None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    for key, value in data.items():
        if key in _SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: [{key}] must be a table")
            for sub, subvalue in value.items():
                label = f"{path}: {key}.{sub}"
                sections[key][sub] = _from_toml(_SECTIONS[key], sub, subvalue, label)
        else:
            top[key] = _from_toml(Settings, key, value, f"{path}: {key}")


def _apply_env(
    env: Mapping[str, str], top: dict[str, Any], sections: dict[str, dict[str, Any]]
) -> None:
    for var, raw in env.items():
        if not var.startswith(ENV_PREFIX):
            continue
        name = var[len(ENV_PREFIX) :].lower()
        section = next((s for s in _SECTIONS if name.startswith(s + "_")), None)
        if section is None:
            top[name] = _from_env(Settings, name, raw, var)
        else:
            key = name[len(section) + 1 :]
            sections[section][key] = _from_env(_SECTIONS[section], key, raw, var)


def _spec(cls: type[Any], key: str, label: str) -> tuple[Any, dict[str, Any]]:
    hints = get_type_hints(cls)
    for f in fields(cls):
        if f.name == key and f.name not in _SECTIONS:
            return hints[key], dict(f.metadata)
    raise ConfigError(f"{label}: unknown config key")


def _from_toml(cls: type[Any], key: str, value: Any, label: str) -> Any:
    kind, meta = _spec(cls, key, label)
    try:
        if meta.get("parse") == "size":
            result: Any = parse_size(value)
        elif kind is timedelta:
            result = parse_duration(value)
        elif kind is Path:
            result = Path(_expect(value, str))
        elif kind is float:
            result = float(_expect(value, int, float))
        elif kind == list[str]:
            items = _expect(value, list)
            result = [_expect(item, str) for item in items]
        elif kind == str | None:
            result = _expect(value, str)
        else:
            result = _expect(value, kind)
    except ConfigError as exc:
        raise ConfigError(f"{label}: {exc}") from None
    return _check(result, meta, label)


def _from_env(cls: type[Any], key: str, raw: str, label: str) -> Any:
    kind, meta = _spec(cls, key, label)
    parsers: dict[Any, Callable[[str], Any]] = {
        timedelta: parse_duration,
        Path: Path,
        str: str,
        str | None: str,
        int: int,
        float: float,
        bool: _env_bool,
        list[str]: lambda text: [item.strip() for item in text.split(",") if item.strip()],
    }
    parse = parse_size if meta.get("parse") == "size" else parsers[kind]
    try:
        result = parse(raw)
    except ValueError as exc:  # ConfigError is a ValueError
        raise ConfigError(f"{label}={raw!r}: {exc}") from None
    return _check(result, meta, label)


def _expect(value: Any, *kinds: type[Any]) -> Any:
    if isinstance(value, bool) and bool not in kinds:
        raise ConfigError(f"expected {_names(kinds)}, got {value!r}")
    if not isinstance(value, kinds):
        raise ConfigError(f"expected {_names(kinds)}, got {value!r}")
    return value


def _names(kinds: tuple[type[Any], ...]) -> str:
    return " or ".join(k.__name__ for k in kinds)


def _env_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"expected a boolean ({'/'.join(sorted(_TRUE | _FALSE))})")


def _check(value: Any, meta: dict[str, Any], label: str) -> Any:
    if meta.get("positive") and not value > 0:
        raise ConfigError(f"{label}: must be > 0, got {value!r}")
    return value
