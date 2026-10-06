#!/usr/bin/env python3
#
# tests/test_config_validation.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup aborts of the configuration contract (Requirement 22)."""

import re

import pytest
from pydantic import ValidationError

from app.config import (
    _REMOVED_ENV_NAMES,
    _SECURITY_RELEVANT_ENV_NAMES,
    OPERATOR_FIELDS,
    Settings,
    format_validation_error,
    load_settings,
    warn_ignored_settings,
)
from app.errors import ConfigError

TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray settings.env; conftest already cleared the process env


def _fail(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as info:
        load_settings()
    return str(info.value.context["detail"])


def _fail_init(**kwargs: object) -> str:
    """Tuning values are not readable from the environment, so cross-checks are driven by kwargs."""
    with pytest.raises(ValidationError) as info:
        Settings(devices="wr1=10.0.0.5:8899", **kwargs)
    return format_validation_error(info.value)


def test_tuning_variables_are_not_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "1")
    monkeypatch.setenv("READ_RETRIES", "9")
    settings = load_settings()
    assert settings.cache_ttl_seconds == 10 and settings.read_retries == 4


def test_valid_defaults_load() -> None:
    settings = load_settings()
    assert settings.bind_port == 8000
    assert settings.auth_required is True and settings.devices == [] and settings.enable_write_support is False
    assert settings.behind_reverse_proxy is False
    assert settings.allow_non_loopback_bind is False
    assert settings.docs_public is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_docs_public_is_read_from_dotenv_and_environment(monkeypatch, tmp_path, caplog, raw, expected) -> None:
    env_file = tmp_path / "docs.env"
    env_file.write_text(f"DOCS_PUBLIC={raw}\n")
    with caplog.at_level("WARNING", logger="app.config"):
        assert load_settings(env_file).docs_public is expected
        monkeypatch.setenv("DOCS_PUBLIC", "false" if expected else "true")
        assert load_settings(env_file).docs_public is not expected
    assert "DOCS_PUBLIC" not in " ".join(r.getMessage() for r in caplog.records)


def test_out_of_range_names_setting_and_value(monkeypatch) -> None:
    detail = _fail(monkeypatch, BIND_PORT="80")
    assert "BIND_PORT" in detail and "1024" in detail and "80" in detail


def test_non_loopback_bind_requires_explicit_consent(monkeypatch) -> None:
    detail = _fail(monkeypatch, BIND_ADDRESS="0.0.0.0")
    assert "ALLOW_NON_LOOPBACK_BIND" in detail and "BIND_ADDRESS" in detail
    with pytest.raises(ValidationError, match="ALLOW_NON_LOOPBACK_BIND"):
        Settings(bind_address="0.0.0.0", behind_reverse_proxy=True)


def test_non_loopback_bind_with_consent_still_warns_without_proxy_mode(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BIND_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("ALLOW_NON_LOOPBACK_BIND", "true")
    with caplog.at_level("WARNING", logger="app.config"):
        settings = load_settings()
    assert str(settings.bind_address) == "0.0.0.0"
    assert settings.allow_non_loopback_bind is True
    assert "behind reverse proxy' is off" in " ".join(r.getMessage() for r in caplog.records)


def test_fixed_settings_are_named_not_valued(monkeypatch, caplog) -> None:
    monkeypatch.setenv("CACHE_TTL_SECONDS", "777")
    with caplog.at_level("INFO", logger="app.config"):
        load_settings()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "CACHE_TTL_SECONDS" in text and "777" not in text


def test_trusted_proxies_are_operator_settable(monkeypatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("FORWARDED_HEADER", "X-Forwarded-For")
    settings = load_settings()
    assert [str(n) for n in settings.trusted_proxies] == ["10.0.0.0/8"]
    assert settings.forwarded_header == "X-Forwarded-For"


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_metrics_endpoint_switch_is_operator_settable(monkeypatch, caplog, raw, expected) -> None:
    monkeypatch.setenv("ENABLE_METRICS_ENDPOINT", raw)
    with caplog.at_level("WARNING", logger="app.config"):
        settings = load_settings()
    assert settings.enable_metrics_endpoint is expected
    assert "ENABLE_METRICS_ENDPOINT" not in " ".join(r.getMessage() for r in caplog.records)


def test_metrics_endpoint_is_enabled_by_default() -> None:
    assert load_settings().enable_metrics_endpoint is True


def test_operator_environment_precedes_dotenv(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "settings.env"
    env_file.write_text("BIND_PORT=9000\n")
    monkeypatch.setenv("BIND_PORT", "9100")
    assert load_settings(env_file).bind_port == 9100


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        ({"max_metrics_per_request": 4, "max_fresh_metrics_per_request": 9}, ("MAX_FRESH", "MAX_METRICS", "9", "4")),
        ({"read_retry_backoff_initial_ms": 900, "read_retry_backoff_max_ms": 100}, ("INITIAL", "MAX_MS", "900")),
        ({"shutdown_periodic_reserve_seconds": 20, "shutdown_grace_seconds": 20}, ("RESERVE", "GRACE")),
        ({"forwarded_header": "X-Forwarded-For"}, ("FORWARDED_HEADER", "TRUSTED_PROXIES")),
        ({"http_workers": 2}, ("HTTP_WORKERS",)),
        ({"periodic_metrics": [f"m{i}" for i in range(65)]}, ("PERIODIC_METRICS",)),
    ],
)
def test_cross_checks_on_code_level_tuning(kwargs, names) -> None:
    detail = _fail_init(**kwargs)
    for fragment in names:
        assert fragment in detail


@pytest.mark.parametrize(
    ("devices", "fragment"),
    [
        ("a=10.0.0.5:1,a=10.0.0.6:1", "duplicate device ids"),
        ("a=10.0.0.5:1,b=10.0.0.5:1", "duplicate physical"),
        ("a=10.0.0.5:1@2", "exactly one directly attached"),
    ],
)
def test_invalid_devices_are_rejected_in_gui_settings(devices, fragment) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(devices=devices)
    assert fragment in format_validation_error(info.value)


def _ignored_messages(caplog) -> dict[str, str]:
    """Join the ignored-settings records per level, so each group can be asserted on its own."""
    return {
        level: " ".join(r.getMessage() for r in caplog.records if r.levelname == level and "Ignored" in r.getMessage())
        for level in ("WARNING", "INFO")
    }


def test_gui_only_variables_are_ignored_with_a_warning_that_names_them_only(monkeypatch, tmp_path, caplog) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text("DEVICES=a=10.0.0.5:1\nENABLE_WRITE_SUPPORT=true\n")
    monkeypatch.setenv("API_TOKENS", f"{TOKEN}:read")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("BEHIND_REVERSE_PROXY", "true")
    with caplog.at_level("INFO", logger="app.config"):
        settings = load_settings(env_file)
    assert settings.devices == [] and settings.auth_required is True
    assert settings.enable_write_support is False and settings.behind_reverse_proxy is False
    messages = _ignored_messages(caplog)
    for name in ("API_TOKENS", "AUTH_REQUIRED", "BEHIND_REVERSE_PROXY", "ENABLE_WRITE_SUPPORT"):
        assert name in messages["WARNING"]
    assert "DEVICES" in messages["INFO"] and "DEVICES" not in messages["WARNING"]
    whole_log = " ".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in whole_log and "10.0.0.5" not in whole_log


@pytest.mark.parametrize(
    "name", ["API_TOKENS", "API_TOKENS_FILE", "AUTH_REQUIRED", "BEHIND_REVERSE_PROXY", "ENABLE_WRITE_SUPPORT"]
)
def test_security_relevant_ignored_settings_stay_a_warning(monkeypatch, caplog, name) -> None:
    monkeypatch.setenv(name, "false")
    with caplog.at_level("INFO", logger="app.config"):
        load_settings()
    messages = _ignored_messages(caplog)
    assert name in messages["WARNING"] and name not in messages["INFO"]
    assert "admin GUI" in messages["WARNING"] and "settings.env" in messages["WARNING"]
    assert not messages["INFO"]


def test_other_ignored_settings_are_info_only(monkeypatch, caplog) -> None:
    monkeypatch.setenv("DEVICES", "a=10.0.0.5:1")
    with caplog.at_level("INFO", logger="app.config"):
        load_settings()
    messages = _ignored_messages(caplog)
    assert "DEVICES" in messages["INFO"] and "settings.env" in messages["INFO"]
    assert not messages["WARNING"]


def test_nothing_is_logged_when_no_ignored_setting_is_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BIND_PORT", "9123")  # operator-settable, so it is not ignored
    with caplog.at_level("INFO", logger="app.config"):
        assert load_settings().bind_port == 9123
    assert _ignored_messages(caplog) == {"WARNING": "", "INFO": ""}


def _candidate_names() -> set[str]:
    """Derived at runtime, so a new non-operator field cannot silently escape both groups."""
    return {f.upper() for f in Settings.model_fields if f not in OPERATOR_FIELDS} | set(_REMOVED_ENV_NAMES)


def test_security_relevant_names_are_real_ignored_settings() -> None:
    assert _SECURITY_RELEVANT_ENV_NAMES <= _candidate_names()


def test_both_groups_together_cover_every_ignored_setting_exactly_once(tmp_path, caplog) -> None:
    candidates = _candidate_names()
    with caplog.at_level("INFO", logger="app.config"):
        warn_ignored_settings(tmp_path / "absent.env", environ=dict.fromkeys(candidates, "x"))
    groups = [
        {name.strip() for name in match.group(1).split(",")}
        for match in (re.search(r"settings: (.+?) - ", r.getMessage()) for r in caplog.records)
        if match
    ]
    assert len(groups) == 2
    assert groups[0] | groups[1] == candidates
    assert not groups[0] & groups[1]


def test_ignored_settings_never_log_their_values(monkeypatch, tmp_path, caplog) -> None:
    env_file = tmp_path / "v.env"
    env_file.write_text("DEVICES=a=10.0.0.5:1\n")
    monkeypatch.setenv("API_TOKENS", f"{TOKEN}:read")
    monkeypatch.setenv("BEHIND_REVERSE_PROXY", "unmistakable-dummy-value")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "4242")
    with caplog.at_level("INFO", logger="app.config"):
        load_settings(env_file)
    whole_log = " ".join(r.getMessage() for r in caplog.records)
    for value in (TOKEN, "unmistakable-dummy-value", "4242", "10.0.0.5"):
        assert value not in whole_log


@pytest.mark.parametrize(
    "name",
    [
        "DISPATCH_SOC_STRATEGY_EXTERNAL_CODE",
        "DISPATCH_BATTERY_DISCHARGE_POSITIVE",
        "DISPATCH_GRID_IMPORT_POSITIVE",
    ],
)
def test_removed_dispatch_hardware_assumptions_warn_and_no_longer_exist(monkeypatch, caplog, name) -> None:
    """These three were assumptions about one inverter model. They are now per-device capability
    fields, verified on the device: a value from the environment must never release hardware again,
    and an operator who still exports one has to notice that he never verified anything."""
    monkeypatch.setenv(name, "true")
    with caplog.at_level("INFO", logger="app.config"):
        settings = load_settings()
    assert name.lower() not in Settings.model_fields
    assert not hasattr(settings, name.lower())
    messages = _ignored_messages(caplog)
    assert name in messages["WARNING"] and name not in messages["INFO"]
    assert "true" not in messages["WARNING"]  # the name is logged, never the value


def test_engineering_ttl_cap_must_not_exceed_the_normal_cap() -> None:
    detail = _fail_init(
        dispatch_max_operation_duration_seconds=600,
        dispatch_max_operation_duration_engineering_seconds=1800,
    )
    assert "DISPATCH_MAX_OPERATION_DURATION_ENGINEERING_SECONDS" in detail
    assert "DISPATCH_MAX_OPERATION_DURATION_SECONDS" in detail
    assert Settings(devices="a=h:1").dispatch_max_operation_duration_engineering_seconds == 1800.0


def test_cache_grace_shorter_than_ttl_is_allowed() -> None:
    settings = Settings(devices="a=h:1", cache_ttl_seconds=60, cache_grace_seconds=10)
    assert settings.cache_grace_seconds == 10


def test_effective_config_hides_token_values() -> None:
    assert TOKEN not in str(load_settings().effective())


def test_endpoint_id_is_the_direct_device_id_without_address() -> None:
    settings = Settings(devices="b=10.0.0.5:1@2,a=10.0.0.5:1")
    assert list(settings.endpoints.values()) == ["a"]


def test_structured_device_entry_strips_ipv6_brackets_like_the_text_parser() -> None:
    """Finding P3-2: a DeviceEntry built directly from structured data must canonicalize the host
    the same way the text parser already does, so the transport never receives bracket syntax."""
    from app.config import DeviceEntry

    entry = DeviceEntry(device_id="a", host="[2001:db8::1]", port=8899)
    assert entry.host == "2001:db8::1"
    settings = Settings(devices="a=[2001:db8::1]:8899")
    assert settings.devices[0].host == "2001:db8::1"
