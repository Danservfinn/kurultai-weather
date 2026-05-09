#!/usr/bin/env python3
"""Read-only weather data source adapters for paper research.

The adapters in this module are intentionally bounded and optional. They only
build or fetch public weather metadata/observations, return provider provenance,
and degrade to status records on failure. No adapter places orders, loads
secrets, signs requests, or requires credentials by default.
"""

from __future__ import annotations

import datetime as dt
import csv
import importlib.util
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


DEFAULT_USER_AGENT = "polymarket-weather-edge/0.1 (+paper research; read-only)"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CACHE_TTL_SECONDS = 300
MAX_CACHE_ENTRIES = 128


FetchText = Callable[[str, dict[str, str], float], str]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def c_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def bounded_excerpt(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return str(value)[:limit]


@dataclass(frozen=True)
class SourceConfig:
    provider: str
    enabled: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    max_cache_entries: int = MAX_CACHE_ENTRIES


@dataclass
class SourceRecord:
    provider: str
    family: str
    status: str
    fetched_at: str
    source_url: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "family": self.family,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "source_url": self.source_url,
            "data": dict(self.data),
            "provenance": dict(self.provenance),
            "error": self.error,
        }


class TTLCache:
    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max_entries
        self._values: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        row = self._values.get(key)
        if row is None:
            return None
        expires_at, value = row
        if time.time() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if len(self._values) >= self.max_entries:
            oldest_key = min(self._values, key=lambda item: self._values[item][0])
            self._values.pop(oldest_key, None)
        self._values[key] = (time.time() + max(1, int(ttl_seconds)), value)


def default_fetch_text(url: str, headers: dict[str, str], timeout_seconds: float) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        return resp.read().decode("utf-8", "replace")


class ReadOnlyWeatherAdapter:
    family = "weather"

    def __init__(
        self,
        config: SourceConfig,
        fetch_text: FetchText | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.config = config
        self.fetch_text = fetch_text or default_fetch_text
        self.cache = cache or TTLCache(config.max_cache_entries)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def provenance(self, url: str | None, **extra: Any) -> dict[str, Any]:
        record = {
            "provider": self.config.provider,
            "family": self.family,
            "read_only": True,
            "requires_credentials": False,
            "source_url": url,
            "timeout_seconds": self.config.timeout_seconds,
            "cache_ttl_seconds": self.config.cache_ttl_seconds,
            "fetched_at": utc_now_iso(),
        }
        record.update(extra)
        return record

    def disabled_record(self, family: str | None = None, note: str = "disabled_by_runtime_flag") -> SourceRecord:
        return SourceRecord(
            provider=self.config.provider,
            family=family or self.family,
            status="disabled",
            fetched_at=utc_now_iso(),
            provenance=self.provenance(None, note=note),
        )

    def _fetch_json(self, url: str, accept: str = "application/json") -> SourceRecord:
        if not self.enabled:
            return self.disabled_record()
        headers = {"User-Agent": self.config.user_agent, "Accept": accept}
        cached = self.cache.get(url)
        fetched_at = utc_now_iso()
        provenance = self.provenance(url, cache_hit=bool(cached))
        try:
            text = cached
            if text is None:
                text = self.fetch_text(url, headers, self.config.timeout_seconds)
                self.cache.set(url, text, self.config.cache_ttl_seconds)
            data = json.loads(text) if text.lstrip().startswith(("{", "[")) else {"raw_text": text}
            return SourceRecord(
                provider=self.config.provider,
                family=self.family,
                status="ok",
                fetched_at=fetched_at,
                source_url=url,
                data=data if isinstance(data, dict) else {"items": data},
                provenance=provenance,
            )
        except Exception as exc:
            return SourceRecord(
                provider=self.config.provider,
                family=self.family,
                status="error",
                fetched_at=fetched_at,
                source_url=url,
                provenance=provenance,
                error=f"{type(exc).__name__}: {exc}",
            )


class NWSAdapter(ReadOnlyWeatherAdapter):
    """Adapter for NWS points, stations, observations, and forecast metadata."""

    family = "nws"

    def __init__(self, enabled: bool = False, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig("nws", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    @staticmethod
    def points_url(lat: float, lon: float) -> str:
        return f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"

    @staticmethod
    def latest_observation_url(station_id: str) -> str:
        return f"https://api.weather.gov/stations/{urllib.parse.quote(station_id.upper())}/observations/latest"

    @staticmethod
    def observations_url(station_id: str, start: dt.datetime, end: dt.datetime) -> str:
        params = {
            "start": start.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return f"https://api.weather.gov/stations/{urllib.parse.quote(station_id.upper())}/observations?{urllib.parse.urlencode(params)}"

    def fetch_points(self, lat: float, lon: float) -> SourceRecord:
        return self._fetch_json(self.points_url(lat, lon), accept="application/geo+json,application/json")

    def fetch_latest_observation(self, station_id: str) -> SourceRecord:
        record = self._fetch_json(self.latest_observation_url(station_id), accept="application/geo+json,application/json")
        if record.status != "ok":
            return record
        props = record.data.get("properties") if isinstance(record.data.get("properties"), dict) else {}
        temp_c = ((props.get("temperature") or {}).get("value") if isinstance(props.get("temperature"), dict) else None)
        record.data = {
            "station_id": station_id.upper(),
            "obs_time_utc": props.get("timestamp"),
            "temperature_c": temp_c,
            "temperature_f": c_to_f(float(temp_c)) if temp_c is not None else None,
            "raw": record.data,
        }
        return record

    def fetch_daily_high(self, station_id: str, date: str) -> SourceRecord:
        try:
            day = dt.date.fromisoformat(date[:10])
        except ValueError:
            return SourceRecord(
                provider=self.config.provider,
                family=self.family,
                status="error",
                fetched_at=utc_now_iso(),
                provenance=self.provenance(None, station_id=station_id, date=date),
                error="invalid_date",
            )
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
        record = self._fetch_json(self.observations_url(station_id, start, end), accept="application/geo+json,application/json")
        if record.status != "ok":
            return record
        features = record.data.get("features") if isinstance(record.data.get("features"), list) else []
        highs: list[float] = []
        for feature in features:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                continue
            temp = props.get("temperature") if isinstance(props.get("temperature"), dict) else {}
            value = temp.get("value")
            if value is None:
                continue
            try:
                highs.append(c_to_f(float(value)))
            except (TypeError, ValueError):
                continue
        high = max(highs) if highs else None
        record.data = {
            "station_id": station_id.upper(),
            "date": day.isoformat(),
            "daily_high_f": high,
            "observation_count": len(highs),
            "raw": record.data,
        }
        record.status = "ok" if high is not None else "missing_daily_high"
        return record

    def forecast_metadata(self, points_record: SourceRecord) -> dict[str, Any]:
        props = points_record.data.get("properties") if isinstance(points_record.data.get("properties"), dict) else {}
        return {
            "nws_grid_office": props.get("gridId"),
            "nws_grid_x": props.get("gridX"),
            "nws_grid_y": props.get("gridY"),
            "nws_forecast_url": props.get("forecast"),
            "nws_forecast_hourly_url": props.get("forecastHourly"),
            "nws_observation_stations_url": props.get("observationStations"),
            "nws_timezone": props.get("timeZone"),
        }


class AviationWeatherMetarAdapter(ReadOnlyWeatherAdapter):
    """Adapter for current AviationWeather METAR reports."""

    family = "metar_direct"

    def __init__(self, enabled: bool = False, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig("aviationweather_metar", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    @staticmethod
    def current_metar_url(station_ids: list[str] | tuple[str, ...] | str) -> str:
        ids = station_ids if isinstance(station_ids, str) else ",".join(s.upper() for s in station_ids)
        q = urllib.parse.urlencode({"ids": ids.upper(), "format": "json", "taf": "false"})
        return f"https://aviationweather.gov/api/data/metar?{q}"

    def fetch_current(self, station_ids: list[str] | tuple[str, ...] | str) -> SourceRecord:
        record = self._fetch_json(self.current_metar_url(station_ids))
        if record.status != "ok":
            return record
        items = record.data.get("items") if "items" in record.data else record.data
        reports = items if isinstance(items, list) else [items]
        parsed = [parse_metar_report(item) for item in reports if item]
        record.data = {"reports": parsed, "raw": record.data}
        return record


class IEMMetarAdapter(ReadOnlyWeatherAdapter):
    """URL-builder/parser skeleton for IEM ASOS/METAR pulls."""

    family = "iem_metar"

    def __init__(self, enabled: bool = False, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig("iem_metar", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    @staticmethod
    def asos_url(station_id: str, start: dt.datetime, end: dt.datetime, tz: str = "UTC") -> str:
        params = [
            ("station", station_id.upper()),
            ("data", "tmpf"),
            ("year1", start.year),
            ("month1", start.month),
            ("day1", start.day),
            ("hour1", start.hour),
            ("minute1", start.minute),
            ("year2", end.year),
            ("month2", end.month),
            ("day2", end.day),
            ("hour2", end.hour),
            ("minute2", end.minute),
            ("tz", tz),
            ("format", "onlycomma"),
            ("latlon", "yes"),
            ("missing", "M"),
            ("trace", "T"),
            ("direct", "yes"),
            ("report_type", "1"),
            ("report_type", "2"),
        ]
        return f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?{urllib.parse.urlencode(params)}"

    def fetch_daily_high(self, station_id: str, date: str, tz: str = "UTC") -> SourceRecord:
        try:
            day = dt.date.fromisoformat(date[:10])
        except ValueError:
            return SourceRecord(
                provider=self.config.provider,
                family=self.family,
                status="error",
                fetched_at=utc_now_iso(),
                provenance=self.provenance(None, station_id=station_id, date=date),
                error="invalid_date",
            )
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
        record = self._fetch_json(self.asos_url(station_id, start, end, tz=tz), accept="text/plain,text/csv")
        if record.status != "ok":
            return record
        raw = str(record.data.get("raw_text") or "")
        high = parse_iem_asos_daily_high_f(raw)
        record.data = {
            "station_id": station_id.upper(),
            "date": day.isoformat(),
            "daily_high_f": high,
            "raw_excerpt": bounded_excerpt(raw),
        }
        record.status = "ok" if high is not None else "missing_daily_high"
        return record


def parse_metar_report(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        raw = str(item.get("rawOb") or item.get("raw_text") or item.get("metar") or "")
        temp_c = item.get("temp")
        if temp_c is None:
            temp_c = item.get("temp_c")
        parsed_temp_c = float(temp_c) if temp_c not in (None, "") else parse_metar_temperature_c(raw)
        return {
            "station_id": item.get("icaoId") or item.get("station_id") or item.get("station"),
            "report_time_utc": item.get("obsTime") or item.get("reportTime") or item.get("time"),
            "temperature_c": parsed_temp_c,
            "temperature_f": c_to_f(parsed_temp_c) if parsed_temp_c is not None else None,
            "raw_metar": raw,
            "parse_quality": "json" if item else "empty",
        }
    raw = str(item)
    temp_c = parse_metar_temperature_c(raw)
    return {
        "station_id": raw.split()[0] if raw else None,
        "report_time_utc": None,
        "temperature_c": temp_c,
        "temperature_f": c_to_f(temp_c) if temp_c is not None else None,
        "raw_metar": raw,
        "parse_quality": "raw_metar_regex" if temp_c is not None else "unparsed",
    }


def parse_metar_temperature_c(raw_metar: str) -> float | None:
    """Parse the METAR temperature token, including negative M-prefixed values."""
    match = re.search(r"\b(M?\d{2})/(?:M?\d{2}|//)\b", raw_metar or "")
    if not match:
        return None
    token = match.group(1)
    sign = -1.0 if token.startswith("M") else 1.0
    digits = token[1:] if token.startswith("M") else token
    return sign * float(digits)


class NOAADelayedLabelAdapter(ReadOnlyWeatherAdapter):
    """NOAA/NCEI delayed daily label/backfill adapter.

    NCEI Access Data Service URLs are tokenless. NOAA CDO token usage is
    optional and never required by default; token values are not exposed in
    provenance.
    """

    family = "ncei_daily_labels"

    def __init__(
        self,
        enabled: bool = False,
        token_env: str = "NOAA_CDO_TOKEN",
        fetch_text: FetchText | None = None,
        cache: TTLCache | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(SourceConfig("noaa_ncei", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)
        self.token_env = token_env

    @staticmethod
    def ncei_daily_url(station_id: str, date: str) -> str:
        params = {
            "dataset": "daily-summaries",
            "stations": station_id,
            "startDate": date,
            "endDate": date,
            "format": "json",
            "units": "standard",
            "includeAttributes": "false",
        }
        return f"https://www.ncei.noaa.gov/access/services/data/v1?{urllib.parse.urlencode(params)}"

    def fetch_daily_summary(self, station_id: str, date: str) -> SourceRecord:
        record = self._fetch_json(self.ncei_daily_url(station_id, date))
        record.provenance["offline_or_daily"] = True
        record.provenance["optional_cdo_token_configured"] = bool(os.environ.get(self.token_env))
        if record.status == "ok":
            high = parse_ncei_daily_high_f(record.data)
            record.data = {
                "station_id": station_id,
                "date": date,
                "daily_high_f": high,
                "raw": record.data,
            }
            record.status = "ok" if high is not None else "missing_daily_high"
        return record


class MeteostatAdapter(ReadOnlyWeatherAdapter):
    """Dependency-free Meteostat adapter stub with graceful absence handling."""

    family = "meteostat"

    def __init__(self, enabled: bool = False, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig("meteostat", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    def dependency_record(self) -> SourceRecord:
        available = importlib.util.find_spec("meteostat") is not None
        status = "available" if available else "dependency_absent"
        return SourceRecord(
            provider=self.config.provider,
            family=self.family,
            status=status if self.enabled else "disabled",
            fetched_at=utc_now_iso(),
            provenance=self.provenance(None, optional_dependency=True, dependency_available=available),
            data={"dependency_available": available},
        )

    def historical_stub(self, station_id: str, start_date: str, end_date: str) -> SourceRecord:
        record = self.dependency_record()
        record.data.update({"station_id": station_id, "start_date": start_date, "end_date": end_date})
        if record.status == "available":
            record.status = "adapter_stub"
            record.error = "Meteostat integration is intentionally optional; wire dependency use in offline backfill only."
        return record


class OpenMeteoEnrichmentAdapter(ReadOnlyWeatherAdapter):
    """Open-Meteo source/model metadata enrichment for existing forecast calls."""

    family = "open_meteo"

    def __init__(self, enabled: bool = True, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig("open_meteo", enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    @staticmethod
    def forecast_url(lat: float, lon: float, start_date: str, end_date: str, models: str | None = None) -> str:
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": start_date,
            "end_date": end_date,
        }
        if models:
            params["models"] = models
        return f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"

    @staticmethod
    def enrich_response(data: dict[str, Any], source_url: str | None = None) -> SourceRecord:
        daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
        highs = [float(v) for v in daily.get("temperature_2m_max", []) if v is not None]
        spread = max(highs) - min(highs) if len(highs) >= 2 else 0.0
        return SourceRecord(
            provider="open_meteo",
            family="open_meteo",
            status="ok" if highs else "missing_forecast_high",
            fetched_at=utc_now_iso(),
            source_url=source_url,
            data={
                "forecast_daily_high_f": highs[0] if highs else None,
                "forecast_high_count": len(highs),
                "forecast_high_min_f": min(highs) if highs else None,
                "forecast_high_max_f": max(highs) if highs else None,
                "model_spread_f": spread,
                "timezone": data.get("timezone"),
                "utc_offset_seconds": data.get("utc_offset_seconds"),
                "elevation": data.get("elevation"),
                "generationtime_ms": data.get("generationtime_ms"),
            },
            provenance={
                "provider": "open_meteo",
                "family": "open_meteo",
                "read_only": True,
                "requires_credentials": False,
                "source_url": source_url,
                "model_metadata_enrichment": True,
            },
        )


class CommercialWeatherAdapter(ReadOnlyWeatherAdapter):
    """Disabled-by-default commercial provider interface stub."""

    family = "commercial_weather"

    def __init__(self, provider_name: str, enabled: bool = False, fetch_text: FetchText | None = None, cache: TTLCache | None = None, **kwargs: Any) -> None:
        super().__init__(SourceConfig(provider_name, enabled=enabled, **kwargs), fetch_text=fetch_text, cache=cache)

    def fetch_stub(self) -> SourceRecord:
        if not self.enabled:
            return self.disabled_record(note="commercial_provider_disabled_by_default")
        return SourceRecord(
            provider=self.config.provider,
            family=self.family,
            status="adapter_stub",
            fetched_at=utc_now_iso(),
            provenance=self.provenance(None, credentials_optional=True, credentials_loaded=False),
            error="Commercial providers are optional adapter stubs and are not used without explicit configuration or mandatory credentials.",
        )


def adapter_catalog(runtime: dict[str, Any] | None = None) -> list[SourceRecord]:
    """Return runtime-visible adapter statuses without making network calls."""
    runtime = runtime or {}
    def optional_record(provider: str, family: str, enabled: bool) -> SourceRecord:
        if not enabled:
            return SourceRecord(
                provider=provider,
                family=family,
                status="disabled",
                fetched_at=utc_now_iso(),
                provenance={"provider": provider, "family": family, "read_only": True},
            )
        return SourceRecord(
            provider=provider,
            family=family,
            status="optional",
            fetched_at=utc_now_iso(),
            provenance={"provider": provider, "family": family, "read_only": True},
        )

    return [
        optional_record("nws", "nws", boolish(runtime.get("enable_nws"))),
        optional_record("aviationweather_metar", "metar_direct", boolish(runtime.get("enable_metar_direct"))),
        optional_record("iem_metar", "iem_metar", boolish(runtime.get("enable_iem"))),
        optional_record("noaa_ncei", "ncei_daily_labels", boolish(runtime.get("enable_ncei_daily"))),
        MeteostatAdapter(enabled=boolish(runtime.get("enable_meteostat"))).dependency_record(),
        CommercialWeatherAdapter("commercial_weather", enabled=boolish(runtime.get("allow_paid_provider_features"))).fetch_stub(),
    ]


def parse_ncei_daily_high_f(data: Any) -> float | None:
    """Extract a Fahrenheit daily high from NOAA/NCEI daily summary payloads."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        rows = data["results"]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        rows = data["items"]
    elif isinstance(data, list):
        rows = data
    else:
        rows = [data] if isinstance(data, dict) else []

    field_names = (
        "TMAX",
        "tmax",
        "DailyMaximumDryBulbTemperature",
        "dailyMaximumDryBulbTemperature",
        "temperature_max",
        "temperatureMaximum",
        "MAX_TEMP",
    )
    values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in field_names:
            raw = row.get(field)
            if raw in (None, "", "M"):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if -100.0 <= value <= 170.0:
                values.append(value)
    return max(values) if values else None


def parse_iem_asos_daily_high_f(raw_csv: str) -> float | None:
    """Extract a daily high from IEM ASOS CSV text without assuming network."""
    text = (raw_csv or "").strip()
    if not text:
        return None
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        return None
    values: list[float] = []
    try:
        reader = csv.DictReader(lines)
        for row in reader:
            raw = row.get("tmpf") or row.get("temperature") or row.get("temp_f")
            if raw in (None, "", "M"):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if -100.0 <= value <= 170.0:
                values.append(value)
    except csv.Error:
        return None
    return max(values) if values else None
