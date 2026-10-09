from datetime import timedelta
from pathlib import Path

import pytest

from satudatascape.client import PROXY_URL, USER_AGENT
from satudatascape.config import ConfigError, Settings, load, parse_duration, parse_size


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "satudatascape.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_match_spec() -> None:
    s = load(None, {})
    assert s.base_url == PROXY_URL == "https://data.go.id/api/proxy"
    assert s.concurrency == 4
    assert s.rps == 2
    assert s.page_size == 1000
    assert s.overlap == timedelta(hours=1)
    assert s.user_agent == USER_AGENT
    assert (s.connect_timeout, s.read_timeout) == (10.0, 90.0)
    assert s.workers == 4
    assert s.raw_dir == Path("data/raw")
    assert s.raw_keep == 3
    assert s.lock_stale_after == timedelta(minutes=30)
    assert s.db == Path("data/satudata.db")
    assert s.download.per_host_concurrency == 1
    assert s.download.global_concurrency == 8
    assert s.download.max_size == 500_000_000
    assert s.download.insecure_hosts == []
    assert s.scheduler.enabled is True
    assert (s.auth.user, s.auth.password) == (None, None)
    assert s.backup.retention == 7


def test_defaults_without_file_equal_settings() -> None:
    assert load(None, {}) == Settings()


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"nope\.toml"):
        load(tmp_path / "nope.toml", {})


def test_file_overrides_defaults(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
rps = 1
concurrency = 2
overlap = "30m"
db = "/srv/sds.db"
raw_dir = "/srv/raw"
[download]
max_size = "2GB"
insecure_hosts = ["data.kamparkab.go.id"]
[scheduler]
enabled = false
[auth]
user = "ops"
password = "secret"
[backup]
retention = 3
""",
    )
    s = load(path, {})
    assert s.rps == 1
    assert s.concurrency == 2
    assert s.overlap == timedelta(minutes=30)
    assert s.db == Path("/srv/sds.db")
    assert s.raw_dir == Path("/srv/raw")
    assert s.download.max_size == 2_000_000_000
    assert s.download.insecure_hosts == ["data.kamparkab.go.id"]
    assert s.download.per_host_concurrency == 1  # untouched default
    assert s.scheduler.enabled is False
    assert (s.auth.user, s.auth.password) == ("ops", "secret")
    assert s.backup.retention == 3
    assert s.page_size == 1000  # untouched default


def test_env_overrides_file(tmp_path: Path) -> None:
    path = write(tmp_path, 'rps = 1\nconcurrency = 2\n[download]\nmax_size = "2GB"\n')
    env = {
        "SDS_RPS": "0.5",
        "SDS_OVERLAP": "2h",
        "SDS_DOWNLOAD_MAX_SIZE": "10MB",
        "SDS_DOWNLOAD_INSECURE_HOSTS": "a.go.id, b.go.id",
        "SDS_SCHEDULER_ENABLED": "false",
        "SDS_AUTH_PASSWORD": "from-env",
        "SDS_BACKUP_RETENTION": "14",
        "SDS_DB": "/tmp/x.db",
        "PATH": "/usr/bin",  # non-SDS variables are ignored
    }
    s = load(path, env)
    assert s.rps == 0.5
    assert s.concurrency == 2  # file value survives
    assert s.overlap == timedelta(hours=2)
    assert s.download.max_size == 10_000_000
    assert s.download.insecure_hosts == ["a.go.id", "b.go.id"]
    assert s.scheduler.enabled is False
    assert s.auth.password == "from-env"
    assert s.backup.retention == 14
    assert s.db == Path("/tmp/x.db")


def test_env_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDS_RPS", "1")
    assert load(None).rps == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1h", timedelta(hours=1)),
        ("30m", timedelta(minutes=30)),
        ("90s", timedelta(seconds=90)),
        ("1d", timedelta(days=1)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        (" 2H ", timedelta(hours=2)),
        (3600, timedelta(hours=1)),
    ],
)
def test_parse_duration(text: str | int, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500MB", 500_000_000),
        ("500 mb", 500_000_000),
        ("1GB", 1_000_000_000),
        ("10KB", 10_000),
        ("1MiB", 1_048_576),
        ("2GiB", 2 * 1024**3),
        ("123B", 123),
        ("123", 123),
        (1024, 1024),
    ],
)
def test_parse_size(text: str | int, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "1y", "h", "-1h", "1.5h"])
def test_bad_duration(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_duration(bad)


@pytest.mark.parametrize("bad", ["", "MB", "5TBB", "-1MB", "1.5MB"])
def test_bad_size(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_size(bad)


def test_unknown_top_level_key_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="rsp"):
        load(write(tmp_path, "rsp = 1\n"), {})


def test_unknown_section_key_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"download\.max_szie"):
        load(write(tmp_path, '[download]\nmax_szie = "1MB"\n'), {})


def test_unknown_section_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="downlaod"):
        load(write(tmp_path, "[downlaod]\nmax_size = 1\n"), {})


def test_unknown_env_key_is_an_error() -> None:
    with pytest.raises(ConfigError, match="SDS_RSP"):
        load(None, {"SDS_RSP": "1"})


def test_section_given_as_scalar_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="download"):
        load(write(tmp_path, "download = 1\n"), {})


@pytest.mark.parametrize(
    "text",
    [
        'rps = "fast"\n',
        "concurrency = 1.5\n",
        "concurrency = true\n",
        "base_url = 3\n",
        '[scheduler]\nenabled = "yes"\n',
        "[download]\ninsecure_hosts = [1]\n",
        "concurrency = 0\n",
        "rps = 0\n",
    ],
)
def test_bad_value_in_file_is_an_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, text), {})


@pytest.mark.parametrize(
    "env",
    [
        {"SDS_RPS": "fast"},
        {"SDS_CONCURRENCY": "1.5"},
        {"SDS_SCHEDULER_ENABLED": "maybe"},
        {"SDS_OVERLAP": "soon"},
    ],
)
def test_bad_value_in_env_is_an_error(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match=next(iter(env))):
        load(None, env)


def test_invalid_toml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"satudatascape\.toml"):
        load(write(tmp_path, "rps = = 1\n"), {})


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_env_true_spellings(value: str) -> None:
    assert load(None, {"SDS_SCHEDULER_ENABLED": value}).scheduler.enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_env_false_spellings(value: str) -> None:
    assert load(None, {"SDS_SCHEDULER_ENABLED": value}).scheduler.enabled is False


def test_empty_env_list_is_empty() -> None:
    assert load(None, {"SDS_DOWNLOAD_INSECURE_HOSTS": ""}).download.insecure_hosts == []
