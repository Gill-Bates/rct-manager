#!/usr/bin/env python3
#
# tests/test_export_config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Push export configuration: disabled by default, per-backend rules, credential guard."""

import pytest
from pydantic import ValidationError

from app.config import DbType, QuestDbDownsampling, Settings, format_validation_error

BASE = {"devices": "wr1=10.0.0.5:8899"}
V2 = {
    "db_type": "influxdb_v2", "influxdb_hostname": "https://db.example", "influxdb_token": "secret-token",
    "influxdb_organization": "org", "influxdb_bucket": "bucket",
}
QDB = {"db_type": "questdb", "questdb_hostname": "localhost"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # conftest already cleared the process env


def _make(**kwargs: object) -> Settings:
    return Settings(**BASE, **kwargs)


def _error(**kwargs: object) -> str:
    with pytest.raises(ValidationError) as info:
        _make(**kwargs)
    return format_validation_error(info.value)


def test_disabled_by_default() -> None:
    s = _make()
    assert s.db_type is None and s.export_endpoint() is None
    assert s.metrics_export_interval_seconds == 30
    assert s.questdb_downsampling is QuestDbDownsampling.OFF
    # Defaults to True: an already-configured db_type keeps exporting for anyone upgrading from
    # before this field existed, without having to flip a new toggle to restore prior behavior.
    assert s.metrics_export_enabled is True


def test_env_names_and_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_TYPE", "")
    monkeypatch.setenv("QUESTDB_DOWNSAMPLING", "")
    assert _make().db_type is None
    monkeypatch.setenv("DB_TYPE", "QuestDB")
    monkeypatch.setenv("QUESTDB_HOSTNAME", "q.local")
    monkeypatch.setenv("QUESTDB_DOWNSAMPLING", "Medium")
    monkeypatch.setenv("QUESTDB_RAW_RETENTION_DAYS", "3")
    s = Settings(**BASE)
    assert s.db_type is DbType.QUESTDB and s.export_endpoint().port == 9000
    assert s.questdb_downsampling is QuestDbDownsampling.MEDIUM and s.questdb_raw_retention_days == 3


def test_influxdb_v1_is_gone() -> None:
    assert _error(db_type="influxdb_v1")


@pytest.mark.parametrize("missing", ["influxdb_hostname", "influxdb_token", "influxdb_organization", "influxdb_bucket"])
def test_v2_required_fields(missing: str) -> None:
    assert missing.upper() in _error(**{**V2, missing: None})


def test_questdb_requires_hostname_and_paired_basic_auth() -> None:
    assert "QUESTDB_HOSTNAME" in _error(db_type="questdb")
    assert "together" in _error(**QDB, questdb_username="u")
    assert "together" in _error(**QDB, questdb_password="p")
    assert _make(**QDB, questdb_username="u", questdb_password="p").questdb_username == "u"


def test_other_backend_settings_are_ignored() -> None:
    s = _make(**V2, questdb_downsampling="high", questdb_raw_retention_days=900, questdb_retention_days=10)
    assert s.db_type is DbType.INFLUXDB_V2
    assert _make(**QDB, influxdb_bucket="b").db_type is DbType.QUESTDB


def test_raw_retention_must_not_exceed_total() -> None:
    assert "QUESTDB_RAW_RETENTION_DAYS" in _error(**QDB, questdb_downsampling="low", questdb_raw_retention_days=60,
                                                  questdb_retention_days=30)
    assert _make(**QDB, questdb_downsampling="low", questdb_raw_retention_days=30, questdb_retention_days=30)
    assert _make(**QDB, questdb_downsampling="low", questdb_raw_retention_days=999, questdb_retention_days=0)
    assert _make(**QDB, questdb_raw_retention_days=999, questdb_retention_days=30)  # downsampling off
    manual = _make(**QDB, questdb_downsampling="manual", questdb_raw_retention_days=999,
                   questdb_retention_days=30)
    assert manual.questdb_downsampling is QuestDbDownsampling.MANUAL
    assert "QUESTDB_RAW_RETENTION_DAYS" in _error(**QDB, questdb_downsampling="manual")


def test_plaintext_credentials_are_refused() -> None:
    plain = {**V2, "influxdb_hostname": "db.example"}
    assert "plain HTTP" in _error(**plain)
    assert _make(**plain, influxdb_allow_plaintext_credentials=True)
    assert _make(**{**plain, "influxdb_hostname": "localhost"})
    assert _make(**plain, influxdb_tls_enabled=True)
    assert "plain HTTP" in _error(**{**V2, "influxdb_hostname": "http://db.example:8086"})
    qplain = {"db_type": "questdb", "questdb_hostname": "db.example", "questdb_username": "u", "questdb_password": "p"}
    assert "plain HTTP" in _error(**qplain)
    assert _make(**qplain, questdb_allow_plaintext_credentials=True)
    assert _make(db_type="questdb", questdb_hostname="db.example")  # no credentials, nothing to protect


@pytest.mark.parametrize(
    ("host", "port", "tls", "expected"),
    [
        ("https://db.example", None, False, ("db.example", 443, True)),
        ("http://db.example", None, True, ("db.example", 80, False)),
        ("https://db.example:8443", None, False, ("db.example", 8443, True)),
        ("http://db.example:9000", 1234, False, ("db.example", 9000, False)),
        ("db.example", 443, False, ("db.example", 443, True)),
        ("db.example", None, False, ("db.example", 9000, False)),
    ],
)
def test_hostname_as_url(host: str, port: int | None, tls: bool, expected: tuple) -> None:
    e = _make(db_type="questdb", questdb_hostname=host, questdb_port=port, questdb_tls_enabled=tls).export_endpoint()
    assert (e.host, e.port, e.tls) == expected


@pytest.mark.parametrize("host", ["https://db.example/path", "ftp://db.example", "db.example/x", "http://"])
def test_invalid_hostname(host: str) -> None:
    assert "QUESTDB_HOSTNAME" in _error(db_type="questdb", questdb_hostname=host)


def test_bounds_and_secret_hygiene() -> None:
    assert _error(metrics_export_interval_seconds=1)
    assert _error(**QDB, questdb_downsampling="extreme")
    assert _error(**QDB, questdb_retention_days=-1)
    s = _make(**V2)
    assert "secret-token" not in repr(s.effective()) + repr(s)
    assert "hunter2" not in _error(**QDB, questdb_password="hunter2")


@pytest.mark.parametrize(
    ("value", "port", "tls"),
    [("http://h:443", 443, False), ("https://h", 443, True), ("h:443", None, None)],
)
def test_resolve_endpoint_scheme_wins_over_port_443(value, port, tls):
    from app.export.endpoint import resolve_endpoint

    if tls is None:  # no scheme: port 443 implies TLS
        endpoint = resolve_endpoint("h", 443, False, 80, "host")
        assert endpoint.tls is True
    else:
        endpoint = resolve_endpoint(value, None, False, 80, "host")
        assert (endpoint.port, endpoint.tls) == (port, tls)


@pytest.mark.parametrize("value", ["host:8086", "http://user:pw@host", "http://user@host"])
def test_resolve_endpoint_rejects_scheme_less_port_and_userinfo(value):
    from app.export.endpoint import resolve_endpoint

    with pytest.raises(ValueError):
        resolve_endpoint(value, None, False, 80, "host")
