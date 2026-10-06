#!/usr/bin/env python3
#
# app/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Settings following the configuration contract (Requirement 22)."""

import logging
import os
import re
from collections.abc import Mapping
from enum import StrEnum
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    IPvAnyAddress,
    IPvAnyNetwork,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.export.endpoint import Endpoint, resolve_endpoint

log = logging.getLogger(__name__)

MAX_DEVICES = 32
MAX_PERIODIC_METRICS = 64
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SECRET_FIELDS = frozenset({"influxdb_token", "questdb_password"})
_MEASUREMENT = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
_PROJECT_DIR = Path(__file__).resolve().parent.parent


class TokenRole(StrEnum):
    READ = "read"
    READ_WRITE = "read/write"


class LogFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


class FreshPeriodicMode(StrEnum):
    OBSERVE = "observe"
    REJECT = "reject"


class DbType(StrEnum):
    INFLUXDB_V2 = "influxdb_v2"
    QUESTDB = "questdb"


class QuestDbDownsampling(StrEnum):
    OFF = "off"
    MANUAL = "manual"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StringEncoding(StrEnum):
    UTF8 = "utf-8"
    LATIN1 = "latin-1"


@dataclass(frozen=True, slots=True)
class EndpointKey:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class DeviceKey:
    """Identifies one device: its transport endpoint plus optional network id."""

    endpoint: EndpointKey
    network_id: int | None = None


class DeviceEntry(BaseModel):
    device_id: str  # path component of the REST contract
    host: str
    port: int = Field(8899, ge=1, le=65535)
    network_id: int | None = Field(None, ge=0, le=2**32 - 1)  # set -> addressed via the plant network
    display_name: str | None = None

    @field_validator("device_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _DEVICE_ID.fullmatch(value):
            raise ValueError("device id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        return value

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            # Brackets are URL syntax for an IPv6 literal, not part of the socket host (Finding
            # P3-2): stripping them here makes text config and structured/admin config agree on
            # the same canonical value instead of relying on each caller to do it separately.
            value = value[1:-1]
        if not value or any(c.isspace() for c in value):
            raise ValueError("host must not be empty or contain whitespace")
        return value

    @property
    def key(self) -> DeviceKey:
        return DeviceKey(EndpointKey(self.host, self.port), self.network_id)


def _split_list(value: object) -> object:
    """Parse comma-separated text; other types pass through for normal validation."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def parse_devices(raw: str) -> list[DeviceEntry]:
    """Parse ``id=host:port[@network_id]`` items."""
    entries: list[DeviceEntry] = []
    for index, item in enumerate(_split_list(raw), start=1):
        device_id, sep, address = item.partition("=")
        if not sep:
            raise ValueError(f"device entry {index} must have the form id=host:port[@network_id]")
        address, at, net = address.partition("@")
        network_id: int | None = None
        if at:
            if not (net.strip().isascii() and net.strip().isdigit()):
                raise ValueError(f"device entry {index} has a non-numeric network id")
            network_id = int(net)
        address = address.strip()
        port = "8899"
        if address.startswith("["):
            host, bracket, rest = address[1:].partition("]")
            if not bracket or (rest and not rest.startswith(":")):
                raise ValueError(f"device entry {index} has a malformed bracketed address")
            if rest:
                port = rest[1:].strip()
        elif address.count(":") > 1:
            raise ValueError(f"device entry {index}: an IPv6 address needs brackets, e.g. [::1]:8899")
        else:
            host, colon, given = address.partition(":")
            if colon:
                port = given.strip()
        if not (port.isascii() and port.isdigit()):
            raise ValueError(f"device entry {index} has a non-numeric port")
        entries.append(
            DeviceEntry(device_id=device_id.strip(), host=host.strip(), port=int(port), network_id=network_id)
        )
    return entries


def url_host(host: object) -> str:
    """Host part of a URL: IPv6 literals need brackets."""
    text = str(host)
    return f"[{text}]" if ":" in text else text


# Removed settings that old deployments may still export; they are ignored with a startup warning.
# The three DISPATCH_* names were hardware assumptions about one inverter model. They are now
# fields of a per-device, persisted capability record that is verified on the device itself, so a
# value from the environment can no longer release any hardware.
_REMOVED_ENV_NAMES = frozenset(
    {
        "API_TOKENS",
        "API_TOKENS_FILE",
        "DISPATCH_SOC_STRATEGY_EXTERNAL_CODE",
        "DISPATCH_BATTERY_DISCHARGE_POSITIVE",
        "DISPATCH_GRID_IMPORT_POSITIVE",
    }
)

# Ignoring one of these silently would let an operator keep a dangerous assumption about
# authentication, write access, proxy trust or verified battery hardware, so they stay at WARNING;
# everything else drops to INFO.
_SECURITY_RELEVANT_ENV_NAMES = frozenset(
    {
        "API_TOKENS",
        "API_TOKENS_FILE",
        "AUTH_REQUIRED",
        "BEHIND_REVERSE_PROXY",
        "ENABLE_WRITE_SUPPORT",
        "DISPATCH_SOC_STRATEGY_EXTERNAL_CODE",
        "DISPATCH_BATTERY_DISCHARGE_POSITIVE",
        "DISPATCH_GRID_IMPORT_POSITIVE",
    }
)

# Only these settings are operator-specific and readable from the environment or settings.env.
# Everything else (timeouts, retries, backoff, cache, limits, intervals, buffers) is a fixed
# default below and changes with the code, not with the deployment.
OPERATOR_FIELDS = frozenset(
    {
        "bind_address",
        "bind_port",
        "allow_non_loopback_bind",
        "docs_public",
        "log_level",
        "trusted_proxies",
        "forwarded_header",
        "enable_metrics_endpoint",
        "hmac_secret",
        "admin_secret",
        "admin_db_path",
        "dispatch_db_path",
        "dispatch_max_charge_power_w",
        "dispatch_max_discharge_power_w",
        "db_type",
        "metrics_export_enabled",
        "metrics_export_interval_seconds",
        "influxdb_hostname",
        "influxdb_port",
        "influxdb_tls_enabled",
        "influxdb_verify_tls",
        "influxdb_measurement_name",
        "influxdb_allow_plaintext_credentials",
        "influxdb_organization",
        "influxdb_bucket",
        "influxdb_token",
        "questdb_hostname",
        "questdb_port",
        "questdb_tls_enabled",
        "questdb_verify_tls",
        "questdb_measurement_name",
        "questdb_allow_plaintext_credentials",
        "questdb_username",
        "questdb_password",
        "questdb_downsampling",
        "questdb_raw_retention_days",
        "questdb_retention_days",
    }
)


class _OperatorOnly:
    """Wraps a settings source and drops every key that is not operator-specific."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._source().items() if k in OPERATOR_FIELDS}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="settings.env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings, _OperatorOnly(env_settings), _OperatorOnly(dotenv_settings))

    # HTTP server and operating environment
    bind_address: IPvAnyAddress = IPv4Address("127.0.0.1")
    bind_port: int = Field(8000, ge=1024, le=65535)
    allow_non_loopback_bind: bool = False
    http_workers: int = Field(1, ge=1, le=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: LogFormat = LogFormat.TEXT
    docs_public: bool = False
    behind_reverse_proxy: bool = False
    hmac_secret: SecretStr | None = None
    admin_secret: SecretStr | None = None  # deprecated name of hmac_secret
    admin_db_path: Path = _PROJECT_DIR / "data" / "rct.db"
    dispatch_db_path: Path = _PROJECT_DIR / "data" / "rct-dispatch.db"

    # Authentication is configured in the admin GUI; rate limits remain operator settings.
    auth_required: bool = True
    trusted_proxies: list[IPvAnyNetwork] = Field(default_factory=list, max_length=32)
    forwarded_header: str = ""
    rate_limit_requests: int = Field(60, ge=1, le=10_000)
    rate_limit_window_seconds: int = Field(60, ge=1, le=3600)
    auth_fail_limit: int = Field(5, ge=1, le=100)
    auth_fail_window_seconds: int = Field(300, ge=10, le=3600)
    auth_fail_block_seconds: int = Field(900, ge=10, le=86_400)

    # Devices, registry, decoding
    devices: list[DeviceEntry] = Field(default_factory=list)
    object_registry_path: Path = _PROJECT_DIR / "app" / "catalog" / "objects.json"
    string_encoding: StringEncoding = StringEncoding.UTF8

    # Timeouts, retries, pacing
    connect_timeout_seconds: float = Field(3.0, ge=0.5, le=30)
    response_timeout_seconds: float = Field(5.0, ge=0.5, le=60)
    write_response_timeout_ms: int = Field(300, ge=50, le=5000)
    read_total_timeout_seconds: float = Field(20.0, ge=1, le=300)
    read_retries: int = Field(4, ge=0, le=10)
    read_retry_backoff_initial_ms: int = Field(200, ge=10, le=10_000)
    read_retry_backoff_max_ms: int = Field(5000, ge=10, le=60_000)
    write_retries: int = Field(2, ge=0, le=5)
    min_request_interval_ms: int = Field(300, ge=0, le=10_000)

    # Queue, budget, shutdown
    queue_max_length: int = Field(32, ge=1, le=1000)
    queue_max_wait_seconds: float = Field(10.0, ge=0.5, le=120)
    device_budget_transactions: int = Field(60, ge=1, le=10_000)
    device_budget_window_seconds: int = Field(60, ge=1, le=3600)
    shutdown_grace_seconds: float = Field(20.0, ge=1, le=300)
    shutdown_periodic_reserve_seconds: float = Field(5.0, ge=0, le=60)

    # Stream robustness
    max_frame_bytes: int = Field(4096, ge=64, le=65_535)
    unexpected_frame_limit: int = Field(50, ge=1, le=10_000)
    unexpected_frame_window_seconds: int = Field(60, ge=1, le=3600)
    bootloader_cooldown_seconds: int = Field(300, ge=10, le=86_400)

    # Heartbeat and cache
    heartbeat_interval_seconds: int = Field(60, ge=10, le=3600)
    heartbeat_metric_name: str = "inverter_state"
    heartbeat_failure_threshold: int = Field(3, ge=1, le=100)
    cache_ttl_seconds: float = Field(10.0, ge=0, le=3600)
    cache_grace_seconds: float = Field(120.0, ge=0, le=86_400)
    slave_cache_ttl_seconds: int = Field(300, ge=0, le=86_400)
    slave_discovery_stable_reads: int = Field(3, ge=1, le=31)
    slave_discovery_max_reads: int = Field(40, ge=1, le=200)

    # REST contract
    max_metrics_per_request: int = Field(32, ge=1, le=256)
    max_fresh_metrics_per_request: int = Field(8, ge=1, le=64)
    fresh_periodic_mode: FreshPeriodicMode = FreshPeriodicMode.OBSERVE
    enable_vendor_diagnostics: bool = False
    problem_type_base_uri: str = "urn:device-api:problem"
    correlation_id_header: str = "X-Request-Id"
    foreign_access_frame_threshold: int = Field(10, ge=1, le=10_000)

    # Writes
    enable_write_support: bool = False
    write_allowlist_path: Path = _PROJECT_DIR / "app" / "catalog" / "default_write_allowlist.json"

    # Battery dispatch. Hardware-dependent values have no guessed default and fail closed: the
    # sign conventions and the external-control strategy code are no longer settings but fields of
    # a per-device capability record, verified on that very device (app/dispatch/capabilities.py).
    dispatch_max_charge_power_w: float | None = Field(None, gt=0, le=50_000)
    dispatch_max_discharge_power_w: float | None = Field(None, gt=0, le=50_000)
    dispatch_min_soc: float = Field(5.0, ge=0, le=100)
    dispatch_max_soc: float = Field(95.0, ge=0, le=100)
    dispatch_grid_import_reserve_w: float = Field(100.0, ge=0, le=500)
    dispatch_grid_control_deadband_w: float = Field(200.0, ge=0, le=2000)
    dispatch_power_write_deadband_w: float = Field(200.0, ge=0, le=2000)
    dispatch_min_write_interval_seconds: float = Field(5.0, ge=1, le=60)
    dispatch_cycle_interval_seconds: float = Field(5.0, ge=1, le=60)
    dispatch_telemetry_timeout_seconds: float = Field(15.0, ge=5, le=120)
    dispatch_control_telemetry_max_age_seconds: float = Field(5.0, ge=1, le=15)
    dispatch_soc_telemetry_max_age_seconds: float = Field(15.0, ge=5, le=60)
    dispatch_max_operation_duration_seconds: float = Field(21_600.0, ge=60, le=86_400)
    # The shorter cap, in force only while a device's engineering mode is *active*, i.e. while
    # dispatch runs on hardware that is not verified for the requested mode.
    dispatch_max_operation_duration_engineering_seconds: float = Field(1800.0, ge=60, le=7200)

    # Periodic reads
    enable_periodic_reads: bool = True  # code switch only; empty periodic_metrics falls back to preselected
    # Not in OPERATOR_FIELDS: only settable programmatically, never via environment.
    periodic_metrics: list[str] = Field(default_factory=list, max_length=MAX_PERIODIC_METRICS)
    periodic_interval_seconds: int = Field(30, ge=1, le=3600)

    # Metrics endpoint
    enable_metrics_endpoint: bool = True
    metrics_require_token: bool = True
    metrics_trusted_sources: list[IPvAnyNetwork] = Field(default_factory=list, max_length=32)
    metrics_exposed_names: list[str] = Field(default_factory=list, max_length=256)
    metrics_rate_limit_requests: int = Field(120, ge=1, le=10_000)
    metrics_rate_limit_window_seconds: int = Field(60, ge=1, le=3600)

    # Push export (InfluxDB 2 / QuestDB OSS); disabled while DB_TYPE is unset, or while this is False
    db_type: DbType | None = None
    metrics_export_enabled: bool = True
    metrics_export_interval_seconds: int = Field(30, ge=5, le=3600)
    influxdb_hostname: str | None = None  # host name or http(s) URL
    influxdb_port: int | None = Field(None, ge=1, le=65535)  # None: URL port or 8086
    influxdb_tls_enabled: bool = False
    influxdb_verify_tls: bool = True
    influxdb_measurement_name: str = Field("rct", pattern=_MEASUREMENT)
    influxdb_allow_plaintext_credentials: bool = False
    influxdb_organization: str | None = None
    influxdb_bucket: str | None = None
    influxdb_token: SecretStr | None = None
    questdb_hostname: str | None = None
    questdb_port: int | None = Field(None, ge=1, le=65535)  # None: URL port or 9000
    questdb_tls_enabled: bool = False
    questdb_verify_tls: bool = True
    questdb_measurement_name: str = Field("rct", pattern=_MEASUREMENT)
    questdb_allow_plaintext_credentials: bool = False
    questdb_username: str | None = None  # HTTP Basic Auth only (QuestDB OSS)
    questdb_password: SecretStr | None = None
    questdb_downsampling: QuestDbDownsampling = QuestDbDownsampling.OFF
    questdb_raw_retention_days: int | None = Field(None, ge=1, le=36_500)  # None: preset default
    questdb_retention_days: int = Field(365, ge=0, le=36_500)  # total retention; 0 keeps everything

    @field_validator(
        "db_type", "influxdb_hostname", "influxdb_port", "influxdb_organization", "influxdb_bucket", "influxdb_token",
        "questdb_hostname", "questdb_port", "questdb_username", "questdb_password", "questdb_downsampling",
        "questdb_raw_retention_days", "questdb_retention_days", mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object, info: ValidationInfo) -> object:
        """An empty variable (commented-out template line) means unset or the default."""
        if isinstance(value, str):
            value = value.strip()
            if info.field_name in {"db_type", "questdb_downsampling"}:
                value = value.lower()
            if not value:
                return {"questdb_downsampling": "off", "questdb_retention_days": 365}.get(info.field_name)
        return value

    @field_validator(
        "trusted_proxies", "metrics_trusted_sources", "periodic_metrics", "metrics_exposed_names", mode="before"
    )
    @classmethod
    def _comma_lists(cls, value: object) -> object:
        return _split_list(value)

    @field_validator("devices", mode="before")
    @classmethod
    def _devices(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_devices(value)
        return value

    @field_validator("devices")
    @classmethod
    def _check_device_count(cls, value: list[DeviceEntry]) -> list[DeviceEntry]:
        if len(value) > MAX_DEVICES:
            raise ValueError(f"must contain at most {MAX_DEVICES} entries")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        if self.hmac_secret is None and self.admin_secret is not None:
            self.hmac_secret = self.admin_secret  # keeps databases encrypted under the old name readable
        if self.max_fresh_metrics_per_request > self.max_metrics_per_request:
            raise ValueError(
                f"MAX_FRESH_METRICS_PER_REQUEST ({self.max_fresh_metrics_per_request}) must not exceed "
                f"MAX_METRICS_PER_REQUEST ({self.max_metrics_per_request})"
            )
        if self.read_retry_backoff_initial_ms > self.read_retry_backoff_max_ms:
            raise ValueError(
                f"READ_RETRY_BACKOFF_INITIAL_MS ({self.read_retry_backoff_initial_ms}) must not exceed "
                f"READ_RETRY_BACKOFF_MAX_MS ({self.read_retry_backoff_max_ms})"
            )
        if self.write_response_timeout_ms > self.response_timeout_seconds * 1000:
            raise ValueError(
                f"WRITE_RESPONSE_TIMEOUT_MS ({self.write_response_timeout_ms}) must not exceed "
                f"RESPONSE_TIMEOUT_SECONDS ({self.response_timeout_seconds}) in milliseconds"
            )
        if self.shutdown_periodic_reserve_seconds >= self.shutdown_grace_seconds:
            raise ValueError(
                f"SHUTDOWN_PERIODIC_RESERVE_SECONDS ({self.shutdown_periodic_reserve_seconds}) must be "
                f"smaller than SHUTDOWN_GRACE_SECONDS ({self.shutdown_grace_seconds})"
            )
        if self.dispatch_min_soc >= self.dispatch_max_soc:
            raise ValueError("DISPATCH_MIN_SOC must be smaller than DISPATCH_MAX_SOC")
        if self.dispatch_max_operation_duration_engineering_seconds > self.dispatch_max_operation_duration_seconds:
            raise ValueError(
                f"DISPATCH_MAX_OPERATION_DURATION_ENGINEERING_SECONDS "
                f"({self.dispatch_max_operation_duration_engineering_seconds}) must not exceed "
                f"DISPATCH_MAX_OPERATION_DURATION_SECONDS ({self.dispatch_max_operation_duration_seconds})"
            )
        if self.dispatch_power_write_deadband_w > self.dispatch_grid_control_deadband_w:
            raise ValueError(
                "DISPATCH_POWER_WRITE_DEADBAND_W must not exceed DISPATCH_GRID_CONTROL_DEADBAND_W"
            )
        if self.forwarded_header.strip().lower() == "forwarded":
            raise ValueError("FORWARDED_HEADER must name a single-address header such as X-Forwarded-For")
        if self.forwarded_header and not self.trusted_proxies:
            raise ValueError("FORWARDED_HEADER requires a non-empty TRUSTED_PROXIES")
        if not ip_address(str(self.bind_address)).is_loopback and not self.allow_non_loopback_bind:
            raise ValueError("ALLOW_NON_LOOPBACK_BIND=true is required for a non-loopback BIND_ADDRESS")
        if not ip_address(str(self.bind_address)).is_loopback and not self.behind_reverse_proxy:
            log.warning(
                "BIND_ADDRESS is not loopback and 'behind reverse proxy' is off: terminate TLS in front of "
                "the service and enable it in the admin GUI"
            )
        self._check_devices()
        self._check_export()
        return self

    def _check_export(self) -> None:
        if self.db_type is DbType.INFLUXDB_V2:
            missing = [
                name for name, value in (
                    ("INFLUXDB_HOSTNAME", self.influxdb_hostname), ("INFLUXDB_ORGANIZATION", self.influxdb_organization),
                    ("INFLUXDB_BUCKET", self.influxdb_bucket), ("INFLUXDB_TOKEN", self.influxdb_token),
                ) if not value
            ]
            if missing:
                raise ValueError(f"{', '.join(missing)} required for DB_TYPE=influxdb_v2")
            endpoint = self.export_endpoint()
            self._guard_plaintext(endpoint, True, self.influxdb_allow_plaintext_credentials, "INFLUXDB")
        elif self.db_type is DbType.QUESTDB:
            if not self.questdb_hostname:
                raise ValueError("QUESTDB_HOSTNAME required for DB_TYPE=questdb")
            if bool(self.questdb_username) != bool(self.questdb_password):
                raise ValueError("QUESTDB_USERNAME and QUESTDB_PASSWORD must be set together or not at all")
            endpoint = self.export_endpoint()
            self._guard_plaintext(
                endpoint, bool(self.questdb_username), self.questdb_allow_plaintext_credentials, "QUESTDB"
            )
            raw, total = self.questdb_raw_retention_days, self.questdb_retention_days
            if self.questdb_downsampling is QuestDbDownsampling.MANUAL and raw is None:
                raise ValueError("QUESTDB_RAW_RETENTION_DAYS is required for manual downsampling")
            if self.questdb_downsampling in {QuestDbDownsampling.LOW, QuestDbDownsampling.MEDIUM,
                                            QuestDbDownsampling.HIGH} and raw is not None and total and raw > total:
                raise ValueError(
                    f"QUESTDB_RAW_RETENTION_DAYS ({raw}) must not exceed QUESTDB_RETENTION_DAYS ({total})"
                )
        else:
            return
        verify = self.influxdb_verify_tls if self.db_type is DbType.INFLUXDB_V2 else self.questdb_verify_tls
        if endpoint.tls and not verify:
            log.warning("TLS certificate verification is disabled for the export target %s", endpoint.host)

    @staticmethod
    def _guard_plaintext(endpoint: Endpoint, has_credentials: bool, allowed: bool, prefix: str) -> None:
        if not has_credentials or endpoint.tls or endpoint.is_loopback:
            return
        if not allowed:
            raise ValueError(
                f"{prefix} credentials must not be sent over plain HTTP; enable TLS (https URL or "
                f"{prefix}_TLS_ENABLED=true) or set {prefix}_ALLOW_PLAINTEXT_CREDENTIALS=true for trusted networks"
            )
        log.warning("%s credentials are sent over plain HTTP to '%s'; use only on trusted networks", prefix, endpoint.host)

    def export_endpoint(self) -> Endpoint | None:
        """Resolved target of the active backend; None while the export is disabled."""
        if self.db_type is DbType.INFLUXDB_V2:
            return resolve_endpoint(
                self.influxdb_hostname or "", self.influxdb_port, self.influxdb_tls_enabled, 8086, "INFLUXDB_HOSTNAME"
            )
        if self.db_type is DbType.QUESTDB:
            return resolve_endpoint(
                self.questdb_hostname or "", self.questdb_port, self.questdb_tls_enabled, 9000, "QUESTDB_HOSTNAME"
            )
        return None

    def _check_devices(self) -> None:
        ids = [d.device_id for d in self.devices]
        if len(set(ids)) != len(ids):
            raise ValueError("device list contains duplicate device ids")
        keys = [d.key for d in self.devices]
        if len(set(keys)) != len(keys):
            raise ValueError("device list contains duplicate physical device keys (endpoint plus network id)")
        for endpoint in self.endpoints:
            direct = [d for d in self.devices if d.key.endpoint == endpoint and d.network_id is None]
            if len(direct) != 1:
                raise ValueError(
                    f"each transport endpoint needs exactly one directly attached device without network id "
                    f"(found {len(direct)} for endpoint {self.endpoints[endpoint]})"
                )

    @property
    def endpoints(self) -> dict[EndpointKey, str]:
        """Transport endpoints with address-free ids: the id of the directly attached device."""
        result: dict[EndpointKey, str] = {}
        for device in self.devices:
            if device.network_id is None:
                result.setdefault(device.key.endpoint, device.device_id)
        for device in self.devices:  # a missing direct device is rejected by _check_devices
            result.setdefault(device.key.endpoint, device.device_id)
        return result

    def effective(self) -> dict[str, Any]:
        """Loggable view of the configuration."""
        return self.model_dump(mode="json", exclude=_SECRET_FIELDS | {"hmac_secret", "admin_secret"})


def format_validation_error(exc: ValidationError) -> str:
    """Name each setting and its constraint; never echo secret inputs or raw parser text."""
    lines: list[str] = []
    for err in exc.errors(include_input=True, include_url=False, include_context=False):
        loc = err["loc"]
        name = str(loc[0]).upper() if loc else "CONFIGURATION"
        text = err["msg"].removeprefix("Value error, ")
        if err["type"] == "missing":
            text = "is required and has no default"
        line = f"{name}: {text}"
        if loc and str(loc[0]) not in _SECRET_FIELDS and err["type"] != "missing" and "input" in err:
            line += f" (rejected value: {err['input']!r})"
        lines.append(line)
    return "\n".join(lines)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple key=value format from a file; skip blank lines and comments."""
    result = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except (OSError, UnicodeError):
        pass
    return result


def warn_ignored_settings(env_file: Path | str | None = None, environ: Mapping[str, str] | None = None) -> None:
    """Name (never show values of) settings that are set but fixed in code or owned by the admin GUI."""
    env = os.environ if environ is None else environ
    names = {k.upper() for k in env}
    path = Path(env_file) if env_file else Path(Settings.model_config["env_file"])
    if path.is_file():
        names |= {k.upper() for k in parse_env_file(path)}
    fixed = {f.upper() for f in Settings.model_fields if f not in OPERATOR_FIELDS}
    fixed = (fixed | _REMOVED_ENV_NAMES) & names
    if security_relevant := sorted(fixed & _SECURITY_RELEVANT_ENV_NAMES):
        log.warning(
            "Ignored security-relevant settings: %s - the effective value comes from the admin GUI, not from here; "
            "remove them from the environment or settings.env",
            ", ".join(security_relevant),
        )
    if other := sorted(fixed - _SECURITY_RELEVANT_ENV_NAMES):
        log.info(
            "Ignored settings: %s - fixed in code or managed in the admin GUI; "
            "remove them from the environment or settings.env",
            ", ".join(other),
        )


def warn_auth_disabled(settings: Settings) -> None:
    """The opt-out must never go unnoticed: every start says so, louder when writes are possible."""
    if settings.auth_required:
        return
    log.warning(
        "SECURITY: authentication is DISABLED in admin settings; every client that reaches %s:%d is accepted "
        "without a token%s",
        settings.bind_address,
        settings.bind_port,
        " and can WRITE allowlisted device settings" if settings.enable_write_support else "",
    )


def _legacy_secret_in_use(settings: Settings) -> bool:
    return settings.admin_secret is not None and settings.admin_secret == settings.hmac_secret


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Load and validate settings; raises ConfigError with a sanitised message."""
    from app.errors import ConfigError

    warn_ignored_settings(env_file)
    try:
        settings = Settings(_env_file=env_file) if env_file else Settings()
    except ValidationError as exc:
        raise ConfigError("invalid_configuration", detail=format_validation_error(exc)) from None
    if os.environ.get("HMAC_SECRET") is None and _legacy_secret_in_use(settings):
        log.warning("ADMIN_SECRET is deprecated, rename it to HMAC_SECRET")
    warn_auth_disabled(settings)
    return settings
