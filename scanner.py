#!/usr/bin/env python3
"""Stdlib-only paper scanner for Polymarket weather markets.

No orders are placed. This is research tooling: fetch public pages/APIs, score
temperature buckets, persist observations, and render a local report.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import edge_validation
import features
import settlement_states
import touch_watchlist
import tuning_evaluator
import weather_sources
import truth_tiers

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "paper_weather.sqlite3")
REPORT_PATH = os.path.join(ROOT, "report.html")
TRAINING_EXPORT_PATH = os.path.join(ROOT, "training_rows.csv")
GOAL_PATH = os.path.join(ROOT, "goals", "paper_weather_edge_v1.yaml")
DEFAULT_URL = "https://polymarket.com/climate-science/weather"
UA = "polymarket-weather-edge/0.1 (+paper research; no trading)"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
WU_HOST_RE = re.compile(r"https?://(?:www\.)?wunderground\.com/[^\s\"'<>)]*", re.I)
WU_STATION_RE = re.compile(r"(?:stationId=|station=|/pws/|/history/[a-z]+/)([A-Z0-9]{3,12})", re.I)
DUST_PRICE = 0.001
DEFAULT_ACCOUNT_NAME = "default-paper"
DEFAULT_BANKROLL_USD = 1000.0
SQLITE_BUSY_TIMEOUT_MS = 5000
RAW_EXCERPT_LIMIT = 4000
RAW_JSON_LIMIT = 20000
FINAL_LABEL_STATUS = truth_tiers.FINAL_LABEL_STATUS
OFFICIAL_FINAL_LABEL_STATUS = truth_tiers.OFFICIAL_FINAL_LABEL_STATUS
PROXY_FINAL_LABEL_STATUS = truth_tiers.PROXY_FINAL_LABEL_STATUS
MULTI_PROVIDER_PROXY_CONSENSUS_LABEL_STATUS = truth_tiers.MULTI_PROVIDER_PROXY_CONSENSUS_LABEL_STATUS
SINGLE_PROVIDER_PROXY_LABEL_STATUS = truth_tiers.SINGLE_PROVIDER_PROXY_LABEL_STATUS
PROVISIONAL_LABEL_STATUS = truth_tiers.PROVISIONAL_LABEL_STATUS
PENDING_LABEL_STATUS = truth_tiers.PENDING_LABEL_STATUS
SKIPPED_LABEL_STATUS = truth_tiers.SKIPPED_LABEL_STATUS
ERROR_LABEL_STATUS = truth_tiers.ERROR_LABEL_STATUS
FINAL_LABEL_OUTCOME_STATUSES = truth_tiers.FINAL_LABEL_OUTCOME_STATUSES
LABEL_OUTCOME_STATUSES = truth_tiers.LABEL_OUTCOME_STATUSES


def runtime_tunable_env_value(key: str) -> str | None:
    """Read a simple KEY=value from repo-local runtime_tunables.env without mutating os.environ."""
    path = os.path.join(ROOT, "runtime_tunables.env")
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def active_paper_account_name() -> str:
    """Return the canonical official paper ledger account for this process."""
    configured = os.environ.get("PAPER_ACCOUNT_NAME") or runtime_tunable_env_value("PAPER_ACCOUNT_NAME")
    if configured and configured.strip():
        return configured.strip()
    return DEFAULT_ACCOUNT_NAME


TARGET_METRIC_DAILY_HIGH = "daily_high"
TARGET_METRIC_DAILY_LOW = "daily_low"
TARGET_METRIC_UNKNOWN = "unknown"
TARGET_METRICS = {TARGET_METRIC_DAILY_HIGH, TARGET_METRIC_DAILY_LOW, TARGET_METRIC_UNKNOWN}
TRAINING_EXPORT_COLUMNS = [
    "id",
    "created_at",
    "run_id",
    "signal_id",
    "market_id",
    "title",
    "outcome",
    "token_id",
    "city",
    "target_date",
    "station_id",
    "target_metric",
    "station_source",
    "source_url",
    "provider",
    "forecast_snapshot_id",
    "observation_id",
    "orderbook_snapshot_id",
    "market_family",
    "eligibility_class",
    "source_confidence",
    "bucket_lo_f",
    "bucket_hi_f",
    "bucket_kind",
    "bucket_state",
    "market_prob",
    "model_prob",
    "entry_price",
    "bid",
    "ask",
    "spread",
    "depth",
    "depth_sufficient",
    "edge",
    "required_edge",
    "uncertainty_margin",
    "ease_score",
    "signal_type",
    "strategy_family",
    "event_key",
    "contract_type",
    "settlement_state",
    "reason",
    "label_status",
    "label_value",
    "label_source",
    "labeled_at",
]
STRATEGY_FAMILIES = {
    "latency_absorbing_state",
    "complement_arb",
    "ladder_inconsistency",
    "settlement_source_edge",
    "settlement_source_delta",
    "diurnal_nowcast",
    "forecast_distribution_directional",
    "watch",
    "skip",
    "unknown",
}
PROHIBITED_LIVE_ARG_NAMES = (
    "live_trading",
    "live_trade",
    "place_order",
    "order_placement",
    "wallet",
    "wallet_address",
    "private_key",
    "api_key",
    "secret",
)


def connect_sqlite(path: str, *, readonly: bool = False, row_factory: bool = False) -> sqlite3.Connection:
    """Open SQLite with the engine's lock policy.

    Write connections run in autocommit mode so scanner network fetches cannot
    accidentally hold a transaction open between statements. Multi-statement
    ledger updates remain paper-only and bounded; each statement is committed
    before the next public HTTP request can run.
    """
    timeout_seconds = SQLITE_BUSY_TIMEOUT_MS / 1000.0
    if readonly and os.path.exists(path):
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        db = sqlite3.connect(uri, timeout=timeout_seconds, uri=True, isolation_level=None)
    else:
        db = sqlite3.connect(path, timeout=timeout_seconds, isolation_level=None)
        db.execute("pragma journal_mode=WAL")
    db.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    if row_factory:
        db.row_factory = sqlite3.Row
    return db
DEFAULT_GOAL_TEXT = """# Paper-only optimization goal scaffold.
# This file is intentionally declarative. The tune command only proposes
# candidate config values from historical paper data; it does not deploy,
# trade, place orders, sign transactions, or load wallets.
name: paper_weather_edge_v1
mode: paper_only
paper_only: true
primary_metric: calibration_brier_log_loss
secondary_metrics: realized_return_pct, max_drawdown, unresolved_rate
minimum_training_rows: 300
minimum_labeled_rows: 300
minimum_calendar_days: 14
validation_method: date_walk_forward
promotion: propose_only
guardrails:
  live_trading: false
  wallet_required: false
  order_placement: false
  displayed_price_fills: false
  require_executable_clob_depth: true
  max_drawdown: 0.20
  max_position_pct: 0.02
  max_city_date_pct: 0.10
  live_money_deployment: false
runtime_config: /Users/kublai/polymarket-weather-edge/runtime_tunables.env
cron_job_id: 4500a64f8da1
current_runtime:
  scan_interval_minutes: 15
  dashboard_poll_seconds: 30
  scan_pause_seconds: 0.01
  enable_wu: false
  enable_nws: false
  enable_iem: false
  enable_ncei_daily: false
  enable_meteostat: false
  enable_metar_direct: false
  enable_om_models: false
  allow_paid_provider_features: false
allowed_tunables:
  scan_interval_minutes: [5, 10, 15, 30, 60, 120]
  dashboard_poll_seconds: [15, 30, 60]
  scan_pause_seconds: [0.01, 0.05, 0.10, 0.20]
  market_metadata_cache_ttl_seconds: [300, 900, 1800, 3600]
  forecast_cache_ttl_seconds: [900, 1800, 3600, 7200]
  observation_cache_ttl_seconds: [120, 300, 600, 900]
  orderbook_cache_ttl_seconds: [15, 30, 60, 120]
  http_timeout_seconds: [4, 6, 8, 12]
  source_backoff_minutes: [5, 15, 30, 60]
  per_source_daily_call_budget: [100, 250, 500, 1000]
  sigma: [2.5, 3.5, 4.5]
  sigma_by_model_spread_multiplier: [0.5, 1.0, 1.5, 2.0]
  edge_threshold: [0.06, 0.08, 0.10, 0.12]
  edge_threshold_touched: [0.02, 0.04, 0.06, 0.08]
  edge_threshold_ambiguous_station: [0.10, 0.12, 0.15, 0.20]
  max_spread: [0.08, 0.10, 0.12]
  max_slippage: [0.01, 0.02, 0.03, 0.05]
  min_entry: [0.01, 0.02, 0.03, 0.05]
  max_entry: [0.90, 0.95]
  paper_size_shares: [2.0, 5.0, 10.0]
  min_fill_shares: [1.0, 2.0, 5.0]
  min_top_of_book_depth: [1.0, 2.0, 5.0, 10.0]
  stale_book_max_age_seconds: [30, 60, 120, 300]
  max_position_pct: [0.005, 0.01, 0.02]
  max_city_date_pct: [0.05, 0.10]
  max_station_date_pct: [0.05, 0.10]
  max_ladder_group_pct: [0.05, 0.10]
  max_open_exposure_pct: [0.20, 0.30, 0.50]
  min_station_confidence_for_buy: [0.50, 0.60, 0.75]
  station_distance_penalty_per_mile: [0.001, 0.002, 0.005]
  elevation_delta_penalty_per_100ft: [0.001, 0.002, 0.005]
  unknown_station_edge_haircut: [0.02, 0.04, 0.08]
  ambiguous_rule_edge_haircut: [0.02, 0.04, 0.08]
  missingness_penalty: [0.01, 0.03, 0.05]
  source_disagreement_threshold_f: [2.0, 4.0, 6.0]
  source_weight_open_meteo: [0.50, 0.75, 1.00]
  source_weight_nws: [0.00, 0.25, 0.50]
  source_weight_iem: [0.00, 0.25, 0.50]
  source_weight_wu: [0.00, 0.25, 0.50]
  source_weight_ncei: [0.00, 0.25, 0.50]
  paper_forward_min_labeled_rows: [300, 500, 1000]
  paper_forward_min_calendar_days: [14, 30, 60]
  paper_forward_max_drawdown: [0.10, 0.15, 0.20]
  paper_forward_min_feature_coverage: [0.70, 0.80, 0.90]
"""
STATION_REGISTRY = {
    "london": {"station_id": "EGLC", "station_name": "London City Airport", "timezone": "Europe/London", "source_reliability": "medium"},
    "singapore": {"station_id": "WSSS", "station_name": "Changi Airport", "timezone": "Asia/Singapore", "source_reliability": "medium"},
    "seoul": {"station_id": "RKSI", "station_name": "Incheon Airport", "timezone": "Asia/Seoul", "source_reliability": "medium"},
    "los angeles": {"station_id": "KLAX", "station_name": "Los Angeles International Airport", "timezone": "America/Los_Angeles", "source_reliability": "medium"},
    "new york": {"station_id": "KLGA", "station_name": "LaGuardia Airport", "timezone": "America/New_York", "source_reliability": "medium"},
    "new york city": {"station_id": "KLGA", "station_name": "LaGuardia Airport", "timezone": "America/New_York", "source_reliability": "medium"},
    "denver": {"station_id": "KDEN", "station_name": "Denver International Airport", "timezone": "America/Denver", "source_reliability": "medium"},
    "chicago": {"station_id": "KORD", "station_name": "Chicago O'Hare Airport", "timezone": "America/Chicago", "source_reliability": "medium"},
    "miami": {"station_id": "KMIA", "station_name": "Miami International Airport", "timezone": "America/New_York", "source_reliability": "medium"},
    "austin": {"station_id": "KAUS", "station_name": "Austin-Bergstrom Airport", "timezone": "America/Chicago", "source_reliability": "medium"},
    "paris": {"station_id": "LFPG", "station_name": "Paris Charles de Gaulle Airport", "timezone": "Europe/Paris", "source_reliability": "medium"},
    "tokyo": {"station_id": "RJTT", "station_name": "Tokyo Haneda Airport", "timezone": "Asia/Tokyo", "source_reliability": "medium"},
}
NCEI_STATION_ID_CROSSWALK = {
    "KAUS": "USW00013958",
    "KDEN": "USW00003017",
    "KLAX": "USW00023174",
    "KLGA": "USW00014732",
    "KMIA": "USW00012839",
    "KORD": "USW00094846",
}


@dataclass(frozen=True)
class BucketSpec:
    label: str
    lo: float
    hi: float
    kind: str
    unit: str = "F"
    integer_semantics: bool = True
    low_int: int | None = None
    high_int: int | None = None

    @property
    def bounds(self) -> tuple[float, float]:
        return self.lo, self.hi


@dataclass(frozen=True)
class StationObservation:
    station_id: str | None
    source: str
    observed_at: str
    local_date: str | None
    current_temp_f: float | None
    high_so_far_f: float | None
    raw_status: str
    excerpt: str = ""


def ensure_paper_only_guard(args: argparse.Namespace | None = None) -> bool:
    """Reject any accidental live-trading, wallet, or order-placement knobs."""
    if args is None:
        return True
    for name in PROHIBITED_LIVE_ARG_NAMES:
        if bool(getattr(args, name, False)):
            raise ValueError(f"paper-only guard blocked prohibited option: {name}")
    return True


def goal_template() -> str:
    return DEFAULT_GOAL_TEXT


def write_default_goal(path: str = GOAL_PATH, overwrite: bool = False) -> bool:
    if os.path.exists(path) and not overwrite:
        return False
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(goal_template())
    return True


def load_goal_text(path: str = GOAL_PATH) -> tuple[str, bool]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), True
    return goal_template(), False


def goal_guardrails_ok(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower())
    required = [
        "paper_only: true",
        "live_trading: false",
        "order_placement: false",
        "wallet_required: false",
        "promotion: propose_only",
    ]
    return all(item in normalized for item in required)


def fetch_json(url: str, timeout: int = 25) -> Any:
    raw, ctype = fetch_json_text(url, timeout), ""
    if raw.lstrip().startswith(("{", "[")):
        return json.loads(raw)
    return extract_next_json(raw)


def fetch_json_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_text_optional(url: str, timeout: int = 15) -> str | None:
    try:
        return fetch_json_text(url, timeout=timeout)
    except Exception:
        return None


def extract_next_json(page: str) -> Any:
    m = re.search(
        r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])(?=[^>]*\btype=["\']application/json["\'])[^>]*>(.*?)</script>',
        page,
        re.S,
    )
    if m:
        return json.loads(html.unescape(m.group(1)))
    b = re.search(r'"buildId":"([^"]+)"', page)
    if b:
        data_url = f"https://polymarket.com/_next/data/{b.group(1)}/en/climate-science/weather.json"
        return json.loads(fetch_json_text(data_url))
    flight_rows = extract_next_flight_json_rows(page)
    if flight_rows:
        return {"__next_f": flight_rows}
    raise ValueError("No Next.js JSON found in page")


def extract_next_flight_json_rows(page: str) -> list[Any]:
    """Extract JSON rows embedded in Next App Router React Flight pushes.

    Polymarket's weather page can ship data through ``self.__next_f.push``
    scripts instead of the legacy ``__NEXT_DATA__`` blob. Each push argument is
    JSON, and its string payload contains newline-delimited Flight rows like
    ``21:[...]``. Only rows whose payload is a JSON object/array are returned;
    module preload rows such as ``11:I[...]`` or ``:HL[...]`` are ignored.
    """
    rows: list[Any] = []
    for match in re.finditer(r"<script\b[^>]*>(.*?)</script>", page, re.S):
        body = match.group(1).strip()
        if "__next_f" not in body or ".push(" not in body:
            continue
        push_start = body.find(".push(")
        arg_start = push_start + len(".push(")
        arg_end = body.rfind(")")
        if push_start < 0 or arg_end <= arg_start:
            continue
        try:
            push_arg = json.loads(body[arg_start:arg_end].strip())
        except json.JSONDecodeError:
            continue
        rows.extend(parse_next_flight_json_rows(push_arg))
    return rows


def parse_next_flight_json_rows(push_arg: Any) -> list[Any]:
    payloads: list[str] = []
    if isinstance(push_arg, list):
        payloads.extend(item for item in push_arg[1:] if isinstance(item, str))
    elif isinstance(push_arg, str):
        payloads.append(push_arg)

    decoder = json.JSONDecoder()
    rows: list[Any] = []
    for payload in payloads:
        for line in payload.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            row_id, row_payload = line.split(":", 1)
            if not row_id or not re.fullmatch(r"[0-9A-Fa-f]+", row_id):
                continue
            row_payload = row_payload.lstrip()
            if not row_payload.startswith(("{", "[")):
                continue
            try:
                value, _ = decoder.raw_decode(row_payload)
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                rows.append(value)
    return rows


def walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("[", "{")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def compact_text(value: Any, max_len: int = 6000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=True, sort_keys=True)
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def bounded_json(value: Any, max_len: int = RAW_JSON_LIMIT) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return text[:max_len]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        value = runtime_tunable_env_value(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def parse_iso_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def station_local_date(timezone_name: str | None, when_utc: dt.datetime | None = None) -> str | None:
    if not timezone_name:
        return None
    when = when_utc or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    return when.astimezone(zone).date().isoformat()


def station_local_hour(timezone_name: str | None, when_utc: dt.datetime | None = None) -> int | None:
    if not timezone_name:
        return None
    when = when_utc or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    return int(when.astimezone(zone).hour)


def first_present(d: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, "", [], {}):
            return d[key]
    return None


def all_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(all_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(all_strings(v))
    elif isinstance(obj, str):
        found.append(obj)
    return found


def extract_links(text: str) -> list[str]:
    links = re.findall(r"https?://[^\s\"'<>)]*", text)
    cleaned: list[str] = []
    for link in links:
        link = link.rstrip(".,;]")
        if link not in cleaned:
            cleaned.append(link)
    return cleaned


def extract_wu_source(*values: Any) -> tuple[str | None, str | None, str | None]:
    joined = "\n".join(compact_text(v, 20000) for v in values if v is not None)
    wu_links = [m.group(0).rstrip(".,;]") for m in WU_HOST_RE.finditer(joined)]
    station = None
    for text in wu_links + [joined]:
        m = WU_STATION_RE.search(text)
        if m:
            station = m.group(1).upper()
            break
    source_url = wu_links[0] if wu_links else None
    host = urllib.parse.urlparse(source_url).netloc.lower().removeprefix("www.") if source_url else None
    return host, source_url, station


def source_confidence(rules_text: str, resolution_text: str, source_url: str | None, station_id: str | None) -> str:
    score = 0
    blob = f"{rules_text}\n{resolution_text}".lower()
    if rules_text or resolution_text:
        score += 1
    if any(term in blob for term in ("resolution", "source", "weather underground", "wunderground", "station")):
        score += 1
    if source_url:
        score += 1
    if station_id:
        score += 1
    if score >= 3:
        return "high"
    if score >= 1:
        return "medium"
    return "low"


def token_ids_from_market(d: dict[str, Any], outcomes: Any) -> list[str | None]:
    raw = first_present(d, ("clobTokenIds", "clobTokenIDs", "tokenIds", "token_ids"))
    parsed = parse_maybe_json(raw)
    ids: list[str | None] = []
    if isinstance(parsed, list):
        ids = [str(x) if x not in (None, "") else None for x in parsed]
    elif isinstance(outcomes, list):
        for outcome in outcomes:
            if isinstance(outcome, dict):
                token_id = first_present(outcome, ("clobTokenId", "clobTokenID", "token_id", "tokenId", "id"))
                ids.append(str(token_id) if token_id not in (None, "") else None)
    return ids


def extract_market_context(d: dict[str, Any]) -> dict[str, Any]:
    rules = first_present(d, ("rules", "resolutionRules", "resolution_criteria", "resolutionCriteria", "gameRules"))
    resolution = first_present(
        d,
        (
            "resolutionSource",
            "resolution_source",
            "resolutionDetails",
            "resolutionDetail",
            "resolution",
            "description",
        ),
    )
    source = first_present(d, ("source", "sourceUrl", "source_url", "settlementSource", "settlement_source"))
    rules_text = compact_text(rules)
    resolution_text = compact_text(resolution)
    source_text = compact_text(source)
    blob = "\n".join(all_strings(d))
    links = extract_links("\n".join([rules_text, resolution_text, source_text, blob]))
    source_host, source_url, station_id = extract_wu_source(rules_text, resolution_text, source_text, blob)
    if not source_url:
        source_url = next((link for link in links if "wunderground.com" in link.lower()), None)
        source_host = urllib.parse.urlparse(source_url).netloc.lower().removeprefix("www.") if source_url else None
    return {
        "rules_text": rules_text,
        "resolution_text": resolution_text,
        "source_text": source_text,
        "source_links": json.dumps(links[:20], ensure_ascii=True),
        "source_host": source_host,
        "source_url": source_url,
        "station_id": station_id,
        "source_confidence": source_confidence(rules_text, resolution_text, source_url, station_id),
    }


def extract_markets(data: Any) -> list[dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}
    for d in walk(data):
        title = d.get("question") or d.get("title") or d.get("name") or d.get("description")
        if not isinstance(title, str) or not re.search(r"\b(weather|temperature|temp|high)\b", title, re.I):
            continue
        outcomes = parse_maybe_json(d.get("outcomes") or d.get("tokens") or d.get("shortOutcomes"))
        prices = parse_maybe_json(d.get("outcomePrices") or d.get("prices") or d.get("lastTradePrices"))
        if not isinstance(outcomes, list) or len(outcomes) < 2:
            continue
        labels = [str(x.get("outcome") if isinstance(x, dict) else x) for x in outcomes]
        probs = normalize_prices(prices, len(labels))
        token_ids = token_ids_from_market(d, outcomes)
        while len(token_ids) < len(labels):
            token_ids.append(None)
        mid = str(d.get("conditionId") or d.get("id") or d.get("slug") or title)
        markets[mid] = {
            "market_id": mid,
            "title": title.strip(),
            "slug": d.get("slug") or "",
            "url": market_url(d),
            "outcomes": labels,
            "prices": probs,
            "token_ids": token_ids[: len(labels)],
            **extract_market_context(d),
        }
    return list(markets.values())


def normalize_prices(prices: Any, n: int) -> list[float | None]:
    vals: list[float | None] = []
    if isinstance(prices, list):
        for p in prices[:n]:
            if isinstance(p, dict):
                p = p.get("price") or p.get("lastPrice") or p.get("midpoint")
            try:
                vals.append(float(p))
            except (TypeError, ValueError):
                vals.append(None)
    while len(vals) < n:
        vals.append(None)
    return vals


def market_url(d: dict[str, Any]) -> str:
    url = d.get("url") or d.get("link")
    if isinstance(url, str) and url.startswith("http"):
        return url
    slug = d.get("slug")
    return f"https://polymarket.com/event/{slug}" if slug else ""


def infer_city_date(title: str, fallback_city: str | None = None) -> tuple[str | None, str | None]:
    city = fallback_city
    m = re.search(r"temperature\s+in\s+(.+?)\s+be\b", title, re.I)
    if not m:
        m = re.search(r"\bin\s+([A-Z][A-Za-z .'-]+?)(?:\s+on\b|\s+\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)|[?,-]|$)", title)
    if m:
        city = clean_city(m.group(1))
    if not city:
        m = re.search(r"\b([A-Z][A-Za-z .'-]+?)\s+(?:high|temperature|weather)\b", title)
        if m:
            city = clean_city(m.group(1))

    today = dt.date.today()
    date = None
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", title)
    if m:
        date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    else:
        m = re.search(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:,\s*(20\d{2}))?\b", title, re.I)
        if m:
            month = "jan feb mar apr may jun jul aug sep oct nov dec".split().index(m.group(1)[:3].lower()) + 1
            year = int(m.group(3) or today.year)
            cand = dt.date(year, month, int(m.group(2)))
            if not m.group(3) and cand < today - dt.timedelta(days=30):
                cand = dt.date(year + 1, month, int(m.group(2)))
            date = cand.isoformat()
    return city, date


def clean_city(s: str) -> str:
    s = re.sub(r"\b(will|be|the|daily|temperature|high|low|weather)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" .,-")


def geocode(city: str) -> dict[str, Any] | None:
    q = urllib.parse.urlencode({"name": city, "count": 1, "language": "en", "format": "json"})
    data = fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}", timeout=5)
    results = data.get("results") if isinstance(data, dict) else None
    return results[0] if results else None


def forecast_high_f(lat: float, lon: float, date: str) -> float | None:
    q = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": date,
            "end_date": date,
        }
    )
    data = fetch_json(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=5)
    vals = data.get("daily", {}).get("temperature_2m_max", []) if isinstance(data, dict) else []
    return float(vals[0]) if vals and vals[0] is not None else None


def fetch_wu_station_observation(
    station_id: str | None,
    date: str | None,
    source_url: str | None = None,
    timezone_name: str | None = None,
) -> StationObservation:
    """Best-effort Weather Underground read.

    WU often blocks public scraping and its JSON API requires credentials. This
    helper intentionally avoids credentials and returns a raw status when
    unavailable.
    """
    observed_at = utc_now_iso()
    local_date = station_local_date(timezone_name) or date
    if not station_id or not date:
        return StationObservation(station_id, "weather_underground", observed_at, local_date, None, None, "missing_station_or_date")
    urls = []
    if source_url:
        urls.append(source_url)
    urls.extend(
        [
            f"https://www.wunderground.com/dashboard/pws/{urllib.parse.quote(station_id)}",
            f"https://www.wunderground.com/dashboard/pws/{urllib.parse.quote(station_id)}/table/{date}/{date}/daily",
            f"https://www.wunderground.com/history/daily/{urllib.parse.quote(station_id)}/date/{date}",
        ]
    )
    for url in urls:
        text = fetch_text_optional(url, timeout=12)
        if not text:
            continue
        high = parse_wu_high_from_text(text)
        current = parse_wu_current_temp_from_text(text)
        if high is not None:
            return StationObservation(
                station_id,
                "weather_underground",
                observed_at,
                local_date,
                current,
                high,
                "ok",
                compact_text(text, RAW_EXCERPT_LIMIT),
            )
        time.sleep(0.1)
    return StationObservation(station_id, "weather_underground", observed_at, local_date, None, None, "no_public_high")


def fetch_wu_observed_high_f(station_id: str | None, date: str | None, source_url: str | None = None) -> float | None:
    return fetch_wu_station_observation(station_id, date, source_url).high_so_far_f


def parse_wu_high_from_text(text: str) -> float | None:
    candidates: list[float] = []
    patterns = [
        r'"tempHigh"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"temperatureHigh"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"max_temp"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"tempAvg"\s*:\s*\{[^}]*"max"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'\bHigh\b[^-+\d]{0,80}(-?\d+(?:\.\d+)?)\s*(?:&deg;|°)?\s*F\b',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I | re.S):
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if -80.0 <= value <= 140.0:
                candidates.append(value)
    return max(candidates) if candidates else None


def parse_wu_current_temp_from_text(text: str) -> float | None:
    patterns = [
        r'"temp"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'"temperature"\s*:\s*(-?\d+(?:\.\d+)?)',
        r'\bCurrent\b[^-+\d]{0,80}(-?\d+(?:\.\d+)?)\s*(?:&deg;|°)?\s*F\b',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I | re.S):
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if -80.0 <= value <= 140.0:
                return value
    return None


def c_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def detect_temperature_unit(label: str) -> str:
    text = label.lower()
    if "°c" in text or "℃" in text or "celsius" in text or re.search(r"(?<=\d)\s*c\b", text):
        return "C"
    return "F"


def bucket_number_token_strings(label: str) -> list[str]:
    normalized = str(label).replace("\u2212", "-").replace("\ufe63", "-").replace("\uff0d", "-")
    normalized = re.sub(r"(?<=\d)\s*[-–—]\s*(?=-?\d)", " to ", normalized)
    return re.findall(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?", normalized)


def integerish_tokens(tokens: list[str]) -> bool:
    return bool(tokens) and all("." not in t and float(t).is_integer() for t in tokens)


def convert_temp(value: float, unit: str) -> float:
    return c_to_f(value) if unit == "C" else value


def integer_lower(value: float, unit: str) -> float:
    return convert_temp(value - 0.5, unit)


def integer_upper(value: float, unit: str) -> float:
    return convert_temp(value + 0.5, unit)


def numeric_tokens(label: str) -> list[float]:
    return [float(x) for x in bucket_number_token_strings(label)]


def parse_bucket(label: str) -> BucketSpec | None:
    """Parse a temperature outcome into integer-bucket Fahrenheit bounds.

    Polymarket weather ladders usually settle on integer station highs. For
    labels such as ``81-82F`` this means the bucket is inclusive of both
    integer highs, so its continuous model interval is ``80.5 <= high < 82.5``.
    Strict inequalities keep strict integer semantics: ``above 80`` starts at
    80.5, while ``80 or above`` starts at 79.5.
    """
    text = html.unescape(str(label)).strip()
    lower = text.lower()
    unit = detect_temperature_unit(text)
    tokens = bucket_number_token_strings(text)
    nums = [float(t) for t in tokens]
    if not nums:
        m = re.search(r"\b(-?\d{1,3})s\b", lower)
        if m:
            decade = float(m.group(1))
            lo_int = int(decade)
            hi_int = lo_int + 9
            return BucketSpec(text, integer_lower(lo_int, unit), integer_upper(hi_int, unit), "closed_range", unit, True, lo_int, hi_int)
        return None

    int_semantics = integerish_tokens(tokens)
    has_strict_below = bool(re.search(r"(?<![<>=])<(?!=)|\b(?:below|under|less than|fewer than)\b", lower))
    has_inclusive_below = bool(re.search(r"<=|≤|\bor\s+(?:below|lower|less)\b|\bat most\b|\bno more than\b|\bless than or equal", lower))
    has_strict_above = bool(re.search(r"(?<![<>=])>(?!=)|\b(?:above|over|greater than|more than)\b", lower))
    has_inclusive_above = bool(re.search(r">=|≥|\+|\bor\s+(?:above|higher|more)\b|\bat least\b|\bno less than\b|\bgreater than or equal", lower))

    if len(nums) >= 2:
        a, b = sorted(nums[:2])
        if int_semantics:
            lo_int = int(a)
            hi_int = int(b)
            return BucketSpec(text, integer_lower(lo_int, unit), integer_upper(hi_int, unit), "closed_range", unit, True, lo_int, hi_int)
        return BucketSpec(text, convert_temp(a, unit), convert_temp(b, unit), "closed_range", unit, False)

    n = nums[0]
    n_int = int(n) if int_semantics else None
    if has_inclusive_below or has_strict_below:
        if int_semantics:
            hi = integer_upper(n, unit) if has_inclusive_below else integer_lower(n, unit)
        else:
            hi = convert_temp(n, unit)
        kind = "open_below_inclusive" if has_inclusive_below else "open_below_strict"
        return BucketSpec(text, -math.inf, hi, kind, unit, int_semantics, None, n_int)
    if has_inclusive_above or has_strict_above:
        if int_semantics:
            lo = integer_lower(n, unit) if has_inclusive_above else integer_upper(n, unit)
        else:
            lo = convert_temp(n, unit)
        kind = "open_above_inclusive" if has_inclusive_above else "open_above_strict"
        return BucketSpec(text, lo, math.inf, kind, unit, int_semantics, n_int, None)
    if int_semantics:
        return BucketSpec(text, integer_lower(n, unit), integer_upper(n, unit), "single_integer", unit, True, n_int, n_int)
    return BucketSpec(text, convert_temp(n, unit), convert_temp(n, unit), "point", unit, False)


def bucket_bounds(label: str) -> tuple[float, float] | None:
    """Backward-compatible displayed bucket bounds used by old tests/callers.

    parse_bucket() keeps the production integer-settlement support interval
    (e.g. 81-82F -> 80.5..82.5). This helper preserves the legacy displayed
    bounds for closed ranges while still returning widened bounds for single
    integer buckets.
    """
    spec = parse_bucket(label)
    if not spec:
        return None
    if spec.kind == "closed_range" and spec.low_int is not None and spec.high_int is not None:
        return (convert_temp(float(spec.low_int), spec.unit), convert_temp(float(spec.high_int), spec.unit))
    return spec.bounds


def normal_prob(mean: float, sigma: float, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    cdf = lambda x: 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))
    a = 0.0 if lo == -math.inf else cdf(lo)
    b = 1.0 if hi == math.inf else cdf(hi)
    return max(0.0, min(1.0, b - a))


def high_within_bucket(observed_high_f: float, bucket: BucketSpec) -> bool:
    return observed_high_f >= bucket.lo and observed_high_f <= bucket.hi


def bucket_state_from_observation(
    observed_high_f: float | None,
    bucket: BucketSpec | None,
    target_date: str | None = None,
    local_date: str | None = None,
) -> str:
    state = settlement_state_for_bucket(bucket, observed_high_f, "Yes", target_date, local_date)
    if state.state == settlement_states.STATE_STILL_POSSIBLE and bucket is not None and observed_high_f is not None and bucket.lo != -math.inf and observed_high_f < bucket.lo:
        gap = bucket.lo - observed_high_f
        return "unlikely_but_possible" if gap >= 10.0 else "still_possible"
    return settlement_states.legacy_bucket_state(state)


def threshold_status_from_bucket_state(state: str) -> str:
    if state == "already_won":
        return "already_touched"
    if state == "already_lost":
        return "impossible_now"
    return "unknown"


def intraday_status(observed_high_f: float | None, bounds: tuple[float, float] | None) -> str:
    if observed_high_f is None or bounds is None:
        return "unknown"
    lo, hi = bounds
    if observed_high_f > hi:
        return "impossible_now"
    if hi == math.inf and observed_high_f >= lo:
        return "already_touched"
    return "unknown"


def apply_intraday_probability(base_prob: float, status: str) -> float:
    if status in ("already_touched", "already_won"):
        return 1.0
    if status in ("impossible_now", "already_lost"):
        return 0.0
    return base_prob


def outcome_side(outcome: str | None) -> str:
    return "no" if str(outcome or "").strip().lower() in {"no", "n"} else "yes"


def contract_spec_for_bucket(bucket: BucketSpec | None, outcome: str | None = None) -> settlement_states.ContractSpec:
    if bucket is None:
        return settlement_states.ContractSpec(settlement_states.CONTRACT_UNKNOWN, outcome_side(outcome), label=str(outcome or ""))
    return settlement_states.classify_contract(
        bucket.kind,
        finite_or_none(bucket.lo),
        finite_or_none(bucket.hi),
        side=outcome_side(outcome),
        label=bucket.label,
    )


def settlement_state_for_bucket(
    bucket: BucketSpec | None,
    observed_high_f: float | None,
    outcome: str | None = None,
    target_date: str | None = None,
    local_date: str | None = None,
) -> settlement_states.SettlementState:
    target = parse_iso_date(target_date)
    local = parse_iso_date(local_date)
    local_day_complete = bool(target and local and local > target)
    spec = contract_spec_for_bucket(bucket, outcome)
    return settlement_states.settlement_state(spec, observed_high_f, local_day_complete=local_day_complete)


def payout_mapping_for_contract(spec: settlement_states.ContractSpec) -> dict[str, Any]:
    if spec.contract_type == settlement_states.CONTRACT_THRESHOLD:
        if spec.threshold_direction == "gte":
            condition = f"final_high_f >= {spec.threshold_f:g}" if spec.threshold_f is not None else "threshold_missing"
        elif spec.threshold_direction == "lte":
            condition = f"final_high_f <= {spec.threshold_f:g}" if spec.threshold_f is not None else "threshold_missing"
        else:
            condition = "threshold_unknown"
    elif spec.contract_type == settlement_states.CONTRACT_EXACT:
        condition = f"{spec.low_f:g} <= final_high_f <= {spec.high_f:g}" if spec.low_f is not None and spec.high_f is not None else "exact_bounds_missing"
    elif spec.contract_type == settlement_states.CONTRACT_RANGE:
        condition = f"{spec.low_f:g} <= final_high_f <= {spec.high_f:g}" if spec.low_f is not None and spec.high_f is not None else "range_bounds_missing"
    else:
        condition = "unclassified_contract"
    return {
        "contract_type": spec.contract_type,
        "side": spec.side,
        "yes_condition": condition,
        "payout_if_condition_true": 0.0 if spec.side == "no" else 1.0,
        "payout_if_condition_false": 1.0 if spec.side == "no" else 0.0,
        "threshold_f": spec.threshold_f,
        "low_f": spec.low_f,
        "high_f": spec.high_f,
        "threshold_direction": spec.threshold_direction,
    }


def normalized_key_part(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


def event_key_for(
    city: str | None,
    target_date: str | None,
    source: str | None,
    station_id: str | None,
    rule_text: str | None,
) -> str:
    """Stable event key by city/date/source/station/rule material."""
    city_part = normalized_key_part(city, "unknown-city")
    date_part = normalized_key_part(target_date, "unknown-date")
    source_part = normalized_key_part(source, "unknown-source")
    station_part = normalized_key_part(str(station_id or "").upper(), "unknown-station")
    rule_hash = features.stable_hash(rule_text or "") or "no-rule"
    return f"{city_part}|{date_part}|{source_part}|{station_part}|{rule_hash}"


def event_rule_text(market: dict[str, Any], bucket: BucketSpec | None = None) -> str:
    pieces = [
        market.get("rules_text"),
        market.get("resolution_text"),
        market.get("source_text"),
        bucket.kind if bucket else None,
        bucket.label if bucket else None,
    ]
    return " ".join(str(piece) for piece in pieces if piece)


def candidate_key_for(event_key: str | None, market_id: str | None, outcome: str | None, token_id: str | None, created_at: str | None) -> str:
    raw = "|".join(str(value or "") for value in (event_key, market_id, outcome, token_id, created_at))
    return features.stable_hash(raw) or normalized_key_part(raw, "candidate")


def fetch_clob_book(token_id: str | None) -> dict[str, Any] | None:
    if not token_id:
        return None
    url = f"{CLOB_BOOK_URL}?{urllib.parse.urlencode({'token_id': token_id})}"
    try:
        data = fetch_json(url, timeout=12)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def parse_book_side(values: Any, reverse: bool) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    if not isinstance(values, list):
        return rows
    for row in values:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price"))
            size = float(row.get("size"))
        except (TypeError, ValueError):
            continue
        if 0.0 <= price <= 1.0 and size > 0.0:
            rows.append((price, size))
    rows.sort(key=lambda x: x[0], reverse=reverse)
    return rows


def executable_price(levels: list[tuple[float, float]], paper_size: float) -> tuple[float | None, float]:
    remaining = paper_size
    notional = 0.0
    filled = 0.0
    for price, size in levels:
        take = min(size, remaining)
        notional += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0
    return notional / filled, filled


def depth_within_price(levels: list[tuple[float, float]], max_price: float | None) -> float:
    if max_price is None:
        return 0.0
    return sum(size for price, size in levels if price <= max_price)


def quote_age_seconds_from_book(book: dict[str, Any] | None) -> float | None:
    if not isinstance(book, dict):
        return None
    timestamp = book.get("timestamp") or book.get("updated_at") or book.get("created_at")
    if isinstance(book.get("book"), dict):
        nested = book["book"]
        timestamp = timestamp or nested.get("timestamp") or nested.get("updated_at") or nested.get("created_at")
    parsed = features.parse_time(timestamp)
    if parsed is None:
        return 0.0
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def quote_from_book(book: dict[str, Any] | None, displayed_price: float | None, paper_size: float) -> dict[str, Any]:
    if not book:
        return {
            "execution_source": "displayed_price",
            "entry_price": displayed_price,
            "bid": None,
            "ask": None,
            "spread": None,
            "depth": None,
            "depth_near_ask": None,
            "depth_sufficient": False,
            "midpoint": None,
            "quote_age_seconds": None,
            "stale_book_flag": True,
            "raw_status": "book_missing",
        }
    quote_age = quote_age_seconds_from_book(book)
    if isinstance(book.get("book"), dict):
        book = book["book"]
    bids = parse_book_side(book.get("bids"), reverse=True)
    asks = parse_book_side(book.get("asks"), reverse=False)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    spread = ask - bid if bid is not None and ask is not None else None
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    avg_ask, filled = executable_price(asks, paper_size)
    depth_sufficient = bool(avg_ask is not None and filled + 1e-9 >= paper_size)
    near_ask_limit = ask + 0.03 if ask is not None else None
    return {
        "execution_source": "clob_book" if depth_sufficient else ("clob_book_partial" if avg_ask is not None else "clob_book_no_ask"),
        "entry_price": avg_ask if avg_ask is not None else displayed_price,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "depth": filled if avg_ask is not None else 0.0,
        "depth_near_ask": depth_within_price(asks, near_ask_limit),
        "depth_sufficient": depth_sufficient,
        "midpoint": midpoint,
        "quote_age_seconds": quote_age,
        "stale_book_flag": bool(quote_age is not None and quote_age > float(os.environ.get("STALE_BOOK_MAX_AGE_SECONDS", "60"))),
        "raw_status": "ok" if depth_sufficient else ("partial_depth" if avg_ask is not None else "no_asks"),
    }


def execution_quote(
    token_id: str | None,
    displayed_price: float | None,
    paper_size: float,
    db: sqlite3.Connection | None = None,
    run_id: int | None = None,
    market_id: str | None = None,
    outcome: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    book = fetch_clob_book(token_id)
    quote = quote_from_book(book, displayed_price, paper_size)
    quote["orderbook_snapshot_id"] = None
    if db is not None:
        quote["orderbook_snapshot_id"] = record_orderbook_snapshot(
            db,
            run_id,
            market_id,
            token_id,
            outcome,
            created_at or utc_now_iso(),
            quote,
            book,
        )
    return quote


def city_key(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[^a-z ]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", key) or None


def station_registry_lookup(city: str | None) -> dict[str, Any] | None:
    if not city:
        return None
    key = city_key(city)
    if not key:
        return None
    if key in STATION_REGISTRY:
        return STATION_REGISTRY[key]
    for name, meta in STATION_REGISTRY.items():
        if name in key or key in name:
            return meta
    return None


def station_registry_lookup_db(db: sqlite3.Connection, city: str | None, market_id: str | None = None, title: str | None = None) -> dict[str, Any] | None:
    key = city_key(city)
    title_l = (title or "").lower()
    if market_id or title_l:
        rows = db.execute(
            """
            select city_key, station_id, station_name, source_url, timezone, reliability, note
            from station_overrides
            where active=1
            order by id desc
            """
        ).fetchall()
        for row in rows:
            o_city, station_id, station_name, source_url, timezone_name, reliability, note = row
            patterns = [p.strip().lower() for p in str(o_city or "").split(",") if p.strip()]
            if market_id and any(p == market_id.lower() for p in patterns):
                return {
                    "city_key": key or o_city,
                    "station_id": station_id,
                    "station_name": station_name,
                    "source_url": source_url,
                    "timezone": timezone_name,
                    "source_reliability": reliability,
                    "override_note": note,
                }
            if title_l and any(p and p in title_l for p in patterns):
                return {
                    "city_key": key or o_city,
                    "station_id": station_id,
                    "station_name": station_name,
                    "source_url": source_url,
                    "timezone": timezone_name,
                    "source_reliability": reliability,
                    "override_note": note,
                }
    if key:
        row = db.execute(
            """
            select city_key, city_name, station_id, station_name, source_url, timezone, reliability
            from station_registry
            where active=1 and city_key=?
            """,
            (key,),
        ).fetchone()
        if row:
            return {
                "city_key": row[0],
                "city_name": row[1],
                "station_id": row[2],
                "station_name": row[3],
                "source_url": row[4],
                "timezone": row[5],
                "source_reliability": row[6],
            }
        for row in db.execute(
            """
            select city_key, city_name, station_id, station_name, source_url, timezone, reliability
            from station_registry
            where active=1
            """
        ):
            if row[0] in key or key in row[0]:
                return {
                    "city_key": row[0],
                    "city_name": row[1],
                    "station_id": row[2],
                    "station_name": row[3],
                    "source_url": row[4],
                    "timezone": row[5],
                    "source_reliability": row[6],
                }
    return station_registry_lookup(city)


def market_family(title: str, resolution_text: str | None = None) -> str:
    text = f"{title} {resolution_text or ''}".lower()
    if any(word in text for word in ("temperature", "highest temp", "daily high", "weather")):
        return "daily_temperature"
    if any(word in text for word in ("tweet", "posts", "count", "number of")):
        return "count_market"
    if any(word in text for word in ("fed", "cpi", "rate cut", "inflation")):
        return "macro_release"
    if any(word in text for word in ("bitcoin", "btc", "ethereum", "eth", "wti", "oil")):
        return "barrier_hit"
    if any(word in text for word in ("election", "president", "war", "ceasefire")):
        return "politics_or_geopolitics"
    return "other"


def ease_score(family: str, source_conf: str | None, spread: float | None, execution_source: str | None) -> float:
    if family == "daily_temperature":
        score = 8.6
    elif family == "count_market":
        score = 8.0
    elif family in ("macro_release", "barrier_hit"):
        score = 6.2
    elif family == "politics_or_geopolitics":
        score = 3.5
    else:
        score = 5.0
    if source_conf == "high":
        score += 0.5
    elif source_conf == "low":
        score -= 1.0
    if spread is not None:
        if spread <= 0.03:
            score += 0.4
        elif spread >= 0.10:
            score -= 1.2
    if execution_source == "displayed_price":
        score -= 0.5
    elif execution_source in ("clob_book_partial", "clob_book_no_ask"):
        score -= 0.8
    return max(0.0, min(10.0, score))


def market_eligibility(
    family: str,
    source_conf: str | None,
    station_id: str | None,
    target_date: str | None,
    bucket: BucketSpec | None,
    timezone_name: str | None,
) -> str:
    if family != "daily_temperature":
        return "non_temperature"
    if bucket is None:
        return "ambiguous_resolution"
    if not target_date:
        return "ambiguous_resolution"
    if source_conf == "low":
        return "unclear_source"
    if not station_id:
        return "unclear_station"
    if not timezone_name:
        return "unclear_timezone"
    return "clean_station"


def uncertainty_margin(source_conf: str | None, spread: float | None, threshold_status: str, family: str) -> float:
    margin = 0.025 if family == "daily_temperature" else 0.06
    if source_conf == "low":
        margin += 0.04
    elif source_conf == "medium":
        margin += 0.015
    if threshold_status in ("already_touched", "impossible_now"):
        margin = min(margin, 0.02)
    if spread is None:
        margin += 0.015
    return max(0.015, min(0.15, margin))


def required_edge(edge_threshold: float, spread: float | None, uncertainty: float) -> float:
    spread_component = 0.0 if spread is None else 1.5 * spread
    return max(edge_threshold, spread_component, uncertainty)


def required_edge_v2(
    edge_threshold: float,
    spread: float | None,
    uncertainty: float,
    source_conf: str | None,
    depth_sufficient: bool,
) -> float:
    base = required_edge(edge_threshold, spread, uncertainty)
    source_risk = 0.04 if source_conf == "medium" else (0.08 if source_conf == "low" else 0.0)
    liquidity_haircut = 0.0 if depth_sufficient else 0.06
    return max(base, source_risk, liquidity_haircut)


def detect_complement_arbitrage(
    rows: list[dict[str, Any]],
    *,
    payout: float = 1.0,
    margin: float = 0.01,
    min_depth: float = 1.0,
    max_quote_age_seconds: float = 60.0,
) -> dict[str, Any]:
    """Detect same-market YES/NO underround from executable asks only."""
    by_side: dict[str, dict[str, Any]] = {}
    for row in rows:
        side = outcome_side(row.get("outcome"))
        if str(row.get("outcome") or "").strip().lower() in {"yes", "y", "no", "n"}:
            by_side[side] = row
    yes = by_side.get("yes")
    no = by_side.get("no")
    if not yes or not no:
        return {"status": "not_binary_pair", "is_arb": False, "strategy_family": "unknown"}
    yes_ask = yes.get("ask")
    no_ask = no.get("ask")
    if yes_ask is None or no_ask is None:
        return {"status": "missing_executable_ask", "is_arb": False, "strategy_family": "unknown"}
    yes_depth = float(yes.get("depth") or 0.0)
    no_depth = float(no.get("depth") or 0.0)
    yes_age = yes.get("quote_age_seconds")
    no_age = no.get("quote_age_seconds")
    stale = any(age is not None and float(age) > max_quote_age_seconds for age in (yes_age, no_age))
    thin = yes_depth + 1e-9 < min_depth or no_depth + 1e-9 < min_depth
    ask_sum = float(yes_ask) + float(no_ask)
    edge = payout - ask_sum - margin
    if stale:
        status = "stale_quote"
    elif thin:
        status = "insufficient_depth"
    elif edge > 0:
        status = "complement_arb"
    else:
        status = "no_arb"
    return {
        "status": status,
        "is_arb": status == "complement_arb",
        "strategy_family": "complement_arb" if status == "complement_arb" else "unknown",
        "yes_ask": float(yes_ask),
        "no_ask": float(no_ask),
        "ask_sum": ask_sum,
        "payout": payout,
        "margin": margin,
        "edge_after_margin": edge,
        "min_depth": min(yes_depth, no_depth),
        "max_quote_age_seconds": max(float(age or 0.0) for age in (yes_age, no_age)),
        "candidate_trade": "buy_yes_and_no" if status == "complement_arb" else None,
    }


def bucket_midpoint_for_row(row: dict[str, Any]) -> float:
    spec = parse_bucket(str(row.get("outcome", "")))
    if spec and math.isfinite(spec.lo) and math.isfinite(spec.hi):
        return (spec.lo + spec.hi) / 2.0
    lo = row.get("bucket_lo_f")
    hi = row.get("bucket_hi_f")
    if lo is not None and hi is not None and math.isfinite(float(lo)) and math.isfinite(float(hi)):
        return (float(lo) + float(hi)) / 2.0
    return float("inf")


def ladder_monitor(
    rows: list[dict[str, Any]],
    *,
    min_depth: float = 1.0,
    max_quote_age_seconds: float = 60.0,
) -> dict[str, Any]:
    usable = [r for r in rows if r.get("entry_price") is not None or r.get("ask") is not None or r.get("bid") is not None]
    if not usable:
        return {
            "status": "ladder_missing",
            "violations": ["ladder_missing"],
            "implied_distribution": [],
            "candidate_correction_trade": None,
        }
    ask_sum = sum(float(r["ask"]) for r in usable if r.get("ask") is not None)
    bid_sum = sum(float(r["bid"]) for r in usable if r.get("bid") is not None)
    market_sum = sum(float(r["entry_price"]) for r in usable if r.get("entry_price") is not None)
    model_sum = sum(float(r["model_prob"]) for r in usable if r.get("model_prob") is not None)
    violations: list[str] = []
    if ask_sum and ask_sum < 0.98:
        violations.append("ask_sum_under_one")
    if bid_sum and bid_sum > 1.02:
        violations.append("bid_sum_over_one")
    if market_sum > 1.08:
        violations.append("probability_sum_high")
    elif market_sum and market_sum < 0.92:
        violations.append("probability_sum_low")
    if any(float(r.get("quote_age_seconds") or 0.0) > max_quote_age_seconds for r in usable):
        violations.append("stale_linked_outcome")
    if any(float(r.get("depth") or 0.0) + 1e-9 < min_depth for r in usable if r.get("ask") is not None):
        violations.append("thin_linked_outcome")

    ordered = sorted(usable, key=bucket_midpoint_for_row)
    for a, b in zip(ordered, ordered[1:]):
        if a.get("entry_price") is not None and b.get("entry_price") is not None and abs(float(a["entry_price"]) - float(b["entry_price"])) >= 0.30:
            violations.append("adjacent_discontinuity")
            break
    for row in ordered:
        state = str(row.get("bucket_state") or "")
        entry = row.get("entry_price")
        if entry is None:
            continue
        if state == "already_lost" and float(entry) >= 0.05:
            violations.append("impossible_bucket_priced")
            break
        if state == "already_won" and float(entry) <= 0.95:
            violations.append("absorbing_bucket_discount")
            break

    correction = None
    best = None
    for row in ordered:
        if row.get("model_prob") is None or row.get("entry_price") is None:
            continue
        edge = float(row["model_prob"]) - float(row["entry_price"])
        if best is None or edge > best[0]:
            best = (edge, row)
    if best and best[0] >= 0.12:
        correction = {
            "action": "buy_underpriced_bucket",
            "outcome": best[1].get("outcome"),
            "edge": best[0],
            "entry_price": best[1].get("entry_price"),
            "model_prob": best[1].get("model_prob"),
        }
        if "underpriced_bucket" not in violations:
            violations.append("underpriced_bucket")
    if ask_sum and ask_sum < 0.98:
        correction = correction or {"action": "buy_full_ladder_underround", "ask_sum": ask_sum, "edge": 1.0 - ask_sum}

    distribution = [
        {
            "outcome": row.get("outcome"),
            "implied_prob": row.get("entry_price"),
            "model_prob": row.get("model_prob"),
            "ask": row.get("ask"),
            "bid": row.get("bid"),
            "depth": row.get("depth"),
            "quote_age_seconds": row.get("quote_age_seconds"),
        }
        for row in ordered
    ]
    return {
        "status": "ladder_violation" if violations else "ladder_ok",
        "violations": sorted(set(violations)) or ["ladder_ok"],
        "ask_sum": ask_sum or None,
        "bid_sum": bid_sum or None,
        "market_sum": market_sum,
        "model_sum": model_sum,
        "implied_distribution": distribution,
        "candidate_correction_trade": correction,
    }


def ladder_diagnostics(rows: list[dict[str, Any]]) -> str:
    structured = ladder_monitor(rows)
    if structured.get("status") == "ladder_missing":
        return "ladder_missing"
    notes = [v for v in structured.get("violations", []) if v != "ladder_ok"]
    if not notes:
        notes.append("ladder_ok")
    if structured.get("market_sum") is not None:
        notes.append(f"market_sum={float(structured['market_sum']):.3f}")
    if structured.get("model_sum") is not None:
        notes.append(f"model_sum={float(structured['model_sum']):.3f}")
    if structured.get("ask_sum"):
        notes.append(f"ask_sum={float(structured['ask_sum']):.3f}")
    if structured.get("bid_sum"):
        notes.append(f"bid_sum={float(structured['bid_sum']):.3f}")
    return "; ".join(notes)


def legacy_ladder_diagnostics(rows: list[dict[str, Any]]) -> str:
    usable = [r for r in rows if r.get("entry_price") is not None and r.get("model_prob") is not None]
    if not usable:
        return "ladder_missing"
    ask_sum = sum(float(r["ask"]) for r in usable if r.get("ask") is not None)
    bid_sum = sum(float(r["bid"]) for r in usable if r.get("bid") is not None)
    market_sum = sum(float(r["entry_price"]) for r in usable)
    model_sum = sum(float(r["model_prob"]) for r in usable)
    notes: list[str] = []
    if ask_sum and ask_sum < 0.98:
        notes.append("ladder_underround")
    if bid_sum and bid_sum > 1.02:
        notes.append("bid_sum_overround")
    if market_sum > 1.08:
        notes.append("overround_high")
    elif market_sum < 0.92:
        notes.append("probability_sum_low")
    ordered = []
    for r in usable:
        spec = parse_bucket(str(r.get("outcome", "")))
        b = spec.bounds if spec else None
        mid = None if b is None or math.isinf(b[0]) or math.isinf(b[1]) else (b[0] + b[1]) / 2.0
        ordered.append((float("inf") if mid is None else mid, r))
    ordered.sort(key=lambda item: item[0])
    for _, r in ordered:
        gap = float(r["model_prob"]) - float(r["entry_price"])
        if gap >= 0.12:
            notes.append("underpriced_bucket")
            break
    for a, b in zip(ordered, ordered[1:]):
        if abs(float(a[1]["entry_price"]) - float(b[1]["entry_price"])) >= 0.30:
            notes.append("adjacent_discontinuity")
            break
    for _, r in ordered:
        state = str(r.get("bucket_state") or "")
        entry = float(r["entry_price"])
        if state == "already_lost" and entry >= 0.05:
            notes.append("impossible_bucket_priced")
            break
        if state == "already_won" and entry <= 0.95:
            notes.append("touched_bucket_discount")
            break
    if not notes:
        notes.append("ladder_ok")
    notes.append(f"market_sum={market_sum:.3f}")
    notes.append(f"model_sum={model_sum:.3f}")
    if ask_sum:
        notes.append(f"ask_sum={ask_sum:.3f}")
    if bid_sum:
        notes.append(f"bid_sum={bid_sum:.3f}")
    return "; ".join(notes)


def classify_signal(
    edge: float | None,
    entry_price: float | None,
    source_conf: str,
    spread: float | None,
    threshold_status: str,
    args: argparse.Namespace,
    uncertainty_margin: float = 0.025,
    market_family_name: str = "daily_temperature",
    ease: float | None = None,
    eligibility_class: str = "clean_station",
    execution_source: str | None = None,
    depth_sufficient: bool = True,
    bucket_state: str = "unknown",
) -> tuple[str, str]:
    reasons: list[str] = []
    if edge is None:
        return "skip", "missing edge"
    if eligibility_class != "clean_station":
        reasons.append(f"eligibility {eligibility_class} not eligible for paper_buy")
    if ease is not None and ease < 8.0:
        reasons.append(f"ease_score {ease:.1f} < 8.0")
    if source_conf == "low":
        reasons.append("low source/rules confidence")
    if execution_source is not None and execution_source != "clob_book":
        reasons.append(f"execution {execution_source} is not executable CLOB ask/depth")
    if not depth_sufficient:
        reasons.append("insufficient executable depth for paper size")
    if spread is not None and spread > args.max_spread:
        reasons.append(f"spread {spread:.1%} > {args.max_spread:.1%}")
    min_entry = getattr(args, "min_entry", 0.02)
    if entry_price is not None and (entry_price < min_entry or entry_price >= args.max_entry):
        reasons.append("effectively settled/dust entry")
    if threshold_status == "impossible_now" or bucket_state == "already_lost":
        reasons.append("station high already makes outcome impossible")
    needed = required_edge_v2(args.edge_threshold, spread, uncertainty_margin, source_conf, depth_sufficient)
    if edge < needed:
        reasons.append(f"edge {edge:.1%} < required_edge {needed:.1%}")
    if reasons:
        if bucket_state == "source_missing" and edge >= 0.0:
            return "watch_source_missing", "; ".join(reasons)
        signal_type = "watch" if edge >= 0.0 and threshold_status != "impossible_now" else "skip"
        return signal_type, "; ".join(reasons)
    if threshold_status == "already_touched" or bucket_state == "already_won":
        return "paper_buy_touched_threshold", "station high already touched threshold; passes paper filters"
    return "paper_buy_forecast_distribution", "passes paper filters"


def classify_strategy_family(
    signal_type: str | None,
    *,
    bucket_state: str | None = None,
    settlement_state: str | None = None,
    ladder_status: str | None = None,
    complement_status: str | None = None,
    source_confidence: str | None = None,
    eligibility_class: str | None = None,
    observed_high_f: float | None = None,
) -> str:
    raw_signal = str(signal_type or "").strip().lower()
    if raw_signal.startswith("skip"):
        return "skip"
    if raw_signal.startswith("watch"):
        return "watch"
    if "source_delta" in raw_signal or settlement_state == "source_delta":
        return "settlement_source_delta"
    if complement_status == "complement_arb" or "complement_arb" in raw_signal:
        return "complement_arb"
    if ladder_status == "ladder_violation" or "ladder" in raw_signal:
        return "ladder_inconsistency"
    if bucket_state in {"already_won", "already_lost"} or settlement_state in {
        settlement_states.STATE_YES_CERTAIN,
        settlement_states.STATE_NO_CERTAIN,
        settlement_states.STATE_YES_IMPOSSIBLE,
        settlement_states.STATE_NO_IMPOSSIBLE,
    }:
        return "latency_absorbing_state"
    if source_confidence == "medium" or eligibility_class in {"unclear_source", "unclear_station", "ambiguous_resolution"}:
        return "settlement_source_edge"
    if observed_high_f is not None:
        return "diurnal_nowcast"
    if raw_signal.startswith("paper_buy"):
        return "forecast_distribution_directional"
    return "unknown"


def init_db(path: str) -> sqlite3.Connection:
    db = connect_sqlite(path)
    db.executescript(
        """
        create table if not exists runs (
          id integer primary key, started_at text not null, source_url text not null,
          markets_seen integer not null, signals_seen integer not null
        );
        create table if not exists markets (
          market_id text primary key, title text not null, url text, first_seen text not null, last_seen text not null
        );
        create table if not exists signals (
          id integer primary key, run_id integer not null, market_id text not null, title text not null,
          city text, target_date text, forecast_high_f real, outcome text not null,
          market_prob real, model_prob real, edge real, created_at text not null
        );
        create table if not exists station_registry (
          city_key text primary key, city_name text, station_id text not null,
          station_name text, source_url text, latitude real, longitude real,
          elevation_m real, timezone text, reliability text, active integer not null default 1,
          updated_at text not null
        );
        create table if not exists station_overrides (
          id integer primary key, city_key text not null, station_id text not null,
          station_name text, source_url text, timezone text, reliability text,
          active integer not null default 1, note text, updated_at text not null
        );
        create table if not exists settlement_source_registry (
          source_key text primary key, canonical_name text not null, family text not null,
          provider text not null, station_id text, source_url text, priority integer not null default 100,
          active integer not null default 1, created_at text not null, updated_at text not null
        );
        create table if not exists source_observation_snapshots (
          id integer primary key, run_id integer, market_id text, event_key text,
          source_key text, provider text not null, source_provider text,
          family text not null, status text not null,
          station_id text, observed_at text, fetched_at text not null, local_date text,
          target_metric text, observed_f real, observed_high_f real, current_temp_f real, source_url text, provenance_json text,
          raw_excerpt text, error text, created_at text not null
        );
        create table if not exists station_residuals (
          id integer primary key, station_id text not null, source_key text, strategy_family text,
          sample_count integer not null, mean_residual_f real not null, mae_f real not null,
          rmse_f real not null, last_label_at text, updated_at text not null
        );
        create table if not exists forecast_snapshots (
          id integer primary key, run_id integer, market_id text, city text,
          target_date text, provider text not null, fetched_at text not null,
          forecast_high_f real, raw_status text, raw_excerpt text
        );
        create table if not exists station_observations (
          id integer primary key, run_id integer, market_id text, station_id text,
          source text not null, observed_at text not null, local_date text,
          current_temp_f real, high_so_far_f real, raw_status text, excerpt text
        );
        create table if not exists orderbook_snapshots (
          id integer primary key, run_id integer, market_id text, token_id text,
          outcome text, captured_at text not null, best_bid real, best_ask real,
          spread real, depth_at_ask real, depth_near_ask real,
          depth_sufficient integer, raw_status text, raw_json text
        );
        create table if not exists training_rows (
          id integer primary key, run_id integer, signal_id integer, created_at text not null,
          market_id text, title text, outcome text, token_id text, city text,
          target_date text, station_id text, target_metric text, station_source text, source_url text,
          provider text, forecast_snapshot_id integer, observation_id integer,
          orderbook_snapshot_id integer, market_family text, eligibility_class text,
          source_confidence text, bucket_lo_f real, bucket_hi_f real, bucket_kind text,
          bucket_state text, market_prob real, model_prob real, entry_price real,
          bid real, ask real, spread real, depth real, depth_sufficient integer,
          edge real, required_edge real, uncertainty_margin real, ease_score real,
          signal_type text, reason text, features_json text, label_status text,
          label_value real, label_source text, labeled_at text
        );
        create table if not exists label_attempts (
          id integer primary key, attempted_at text not null, training_row_id integer,
          position_id integer, market_id text, title text, outcome text,
          target_date text, station_id text, source_provider text not null,
          source_family text, source_url text, source_status text,
          station_confidence text, target_metric text, final_observed_f real, final_high_f real, threshold_low_f real,
          threshold_high_f real, label_value real, outcome_status text not null,
          reason text, provenance_json text, raw_excerpt text
        );
        create table if not exists paper_accounts (
          id integer primary key, name text not null unique, starting_cash real not null,
          cash real not null, realized_pnl real not null default 0,
          created_at text not null, updated_at text not null
        );
        create table if not exists paper_orders (
          id integer primary key, run_id integer, signal_id integer, account_id integer,
          market_id text, token_id text, outcome text, side text not null,
          signal_type text, status text not null, requested_shares real,
          limit_price real, estimated_cost real, reason text, created_at text not null
        );
        create table if not exists paper_fills (
          id integer primary key, order_id integer not null, filled_at text not null,
          shares real not null, price real not null, cost real not null,
          slippage real, source text, raw_status text
        );
        create table if not exists shadow_orders (
          id integer primary key, run_id integer, signal_id integer, market_id text,
          token_id text, outcome text, side text not null, signal_type text,
          requested_shares real, limit_price real, estimated_cost real,
          shadow_reason text not null, created_at text not null, event_key text,
          candidate_key text, strategy_family text
        );
        create table if not exists shadow_fills (
          id integer primary key, shadow_order_id integer not null, filled_at text not null,
          shares real not null, price real not null, cost real not null,
          slippage real, source text, raw_status text, event_key text,
          candidate_key text, strategy_family text
        );
        create table if not exists paper_positions (
          id integer primary key, account_id integer not null, market_id text not null,
          token_id text, title text, outcome text not null, city text, target_date text,
          shares real not null, avg_price real not null, cost_basis real not null,
          realized_pnl real not null default 0, latest_mark real, status text not null,
          updated_at text not null,
          unique(account_id, market_id, outcome)
        );
        create table if not exists paper_settlements (
          id integer primary key, position_id integer not null, settled_at text not null,
          outcome_status text not null, payout real not null, realized_pnl real not null,
          source_signal_id integer
        );
        create table if not exists paper_account_snapshots (
          id integer primary key, account_id integer not null, run_id integer,
          captured_at text not null, cash real not null, open_exposure real not null,
          realized_pnl real not null, unrealized_pnl real not null, equity real not null,
          return_pct real not null, drawdown real not null, unresolved_positions integer not null
        );
        create table if not exists events (
          event_key text primary key, city text, target_date text, source text,
          station_id text, rule_hash text, first_seen text not null,
          last_seen text not null, latent_final_high_mean_f real,
          latent_final_high_sigma_f real, observed_high_f real,
          local_day_complete integer, contract_count integer,
          open_exposure real, open_position_count integer, source_disagreement_f real
        );
        create table if not exists contract_payouts (
          id integer primary key, event_key text not null, market_id text,
          outcome text, token_id text, contract_type text, side text,
          bucket_lo_f real, bucket_hi_f real, threshold_f real,
          payout_mapping_json text, created_at text not null
        );
        create table if not exists event_exposure_snapshots (
          id integer primary key, event_key text not null, captured_at text not null,
          open_exposure real, open_value real, open_position_count integer,
          latent_final_high_mean_f real, latent_final_high_sigma_f real,
          observed_high_f real, local_day_complete integer,
          source_disagreement_f real
        );
        create table if not exists lifecycle_attribution (
          id integer primary key, candidate_key text not null,
          event_key text, strategy_family text, market_id text, outcome text,
          signal_id integer, training_row_id integer, order_id integer,
          fill_id integer, position_id integer, label_attempt_id integer,
          label_status text, label_value real, paper_settlement_id integer,
          calibration_row_id integer, source_run_id integer,
          created_at text not null, updated_at text not null
        );
        create table if not exists calibration_rows (
          id integer primary key, training_row_id integer not null unique,
          event_key text, strategy_family text, contract_type text,
          market_family text, event_time_bucket text, prediction_prob real,
          label_value real, brier real, log_loss real, label_source text,
          label_attempt_id integer, target_metric text, final_observed_f real,
          label_confidence text, provider_set text, created_at text not null
        );
        create table if not exists touch_watchlist (
          event_key text not null,
          market_id text not null,
          token_id text,
          city text,
          station_id text,
          target_date text,
          strategy_family text,
          contract_type text,
          threshold_f real,
          side text,
          current_high_f real,
          distance_to_threshold_f real,
          local_hour integer,
          hotness_score real,
          hot_reason text,
          watch_started_at text,
          last_source_poll_at text,
          last_book_update_at text,
          last_seen_ask real,
          last_seen_bid real,
          last_seen_depth real,
          active integer default 1,
          primary key(event_key, market_id, token_id)
        );
        create table if not exists threshold_touch_events (
          id integer primary key,
          event_key text not null,
          market_id text not null,
          token_id text,
          city text,
          station_id text,
          target_date text,
          contract_type text,
          side text,
          threshold_f real not null,
          observed_high_f real not null,
          source_provider text,
          source_observed_at text,
          source_fetched_at text not null,
          scanner_detected_at text not null,
          source_age_seconds real,
          detection_delay_seconds real,
          settlement_source_match integer,
          confidence_class text,
          raw_status text,
          created_at text not null
        );
        create table if not exists post_touch_repricing (
          id integer primary key,
          touch_event_id integer,
          event_key text,
          market_id text,
          token_id text,
          observed_at text not null,
          seconds_after_touch real,
          best_bid real,
          best_ask real,
          spread real,
          ask_depth real,
          midpoint real,
          last_trade_price real,
          book_age_seconds real,
          source text
        );
        create index if not exists idx_touch_watchlist_active on touch_watchlist(active, target_date, station_id);
        create index if not exists idx_touch_watchlist_token on touch_watchlist(token_id, active);
        create index if not exists idx_threshold_touch_events_event on threshold_touch_events(event_key, market_id, token_id, created_at);
        create index if not exists idx_threshold_touch_events_detected on threshold_touch_events(scanner_detected_at);
        create index if not exists idx_post_touch_repricing_touch on post_touch_repricing(touch_event_id, observed_at);
        create index if not exists idx_post_touch_repricing_token on post_touch_repricing(token_id, observed_at);
        create index if not exists idx_source_observation_snapshots_event on source_observation_snapshots(event_key, market_id, provider, created_at);
        create index if not exists idx_source_observation_snapshots_status on source_observation_snapshots(status, provider, created_at);
        create unique index if not exists idx_station_residuals_key on station_residuals(station_id, coalesce(source_key,''), coalesce(strategy_family,''));
        """
    )
    ensure_columns(
        db,
        "markets",
        {
            "rules_text": "text",
            "resolution_text": "text",
            "source_text": "text",
            "source_links": "text",
            "source_host": "text",
            "source_url": "text",
            "station_id": "text",
            "source_confidence": "text",
            "clob_token_ids": "text",
            "market_family": "text",
            "eligibility_class": "text",
            "event_key": "text",
            "rule_hash": "text",
        },
    )
    ensure_columns(
        db,
        "signals",
        {
            "observed_high_f": "real",
            "threshold_status": "text",
            "execution_source": "text",
            "entry_price": "real",
            "bid": "real",
            "ask": "real",
            "spread": "real",
            "depth": "real",
            "signal_type": "text",
            "reason": "text",
            "source_confidence": "text",
            "source_host": "text",
            "source_url": "text",
            "station_id": "text",
            "market_family": "text",
            "ease_score": "real",
            "uncertainty_margin": "real",
            "required_edge": "real",
            "ladder_diagnostic": "text",
            "token_id": "text",
            "bucket_lo_f": "real",
            "bucket_hi_f": "real",
            "bucket_kind": "text",
            "bucket_state": "text",
            "eligibility_class": "text",
            "forecast_snapshot_id": "integer",
            "observation_id": "integer",
            "orderbook_snapshot_id": "integer",
            "depth_near_ask": "real",
            "depth_sufficient": "integer",
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
            "contract_type": "text",
            "settlement_state": "text",
            "early_state": "text",
            "final_state": "text",
            "payout_mapping_json": "text",
            "quote_age_seconds": "real",
            "stale_book_flag": "integer",
            "complement_arb_edge": "real",
            "complement_arb_status": "text",
            "ladder_violation_type": "text",
            "correction_trade": "text",
            "latent_final_high_mean_f": "real",
            "latent_final_high_sigma_f": "real",
            "local_day_complete": "integer",
        },
    )
    ensure_columns(
        db,
        "training_rows",
        {
            "label_status": "text",
            "label_value": "real",
            "label_source": "text",
            "labeled_at": "text",
            "city": "text",
            "station_id": "text",
            "station_source": "text",
            "source_confidence": "text",
            "market_family": "text",
            "target_metric": "text",
            "target_date": "text",
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
            "signal_type": "text",
            "edge": "real",
            "contract_type": "text",
            "settlement_state": "text",
            "early_state": "text",
            "final_state": "text",
            "payout_mapping_json": "text",
            "quote_age_seconds": "real",
            "stale_book_flag": "integer",
            "complement_arb_edge": "real",
            "complement_arb_status": "text",
            "ladder_violation_type": "text",
            "correction_trade": "text",
            "latent_final_high_mean_f": "real",
            "latent_final_high_sigma_f": "real",
            "local_day_complete": "integer",
        },
    )
    ensure_columns(
        db,
        "label_attempts",
        {
            "position_id": "integer",
            "source_family": "text",
            "source_url": "text",
            "source_status": "text",
            "station_confidence": "text",
            "target_metric": "text",
            "final_observed_f": "real",
            "final_high_f": "real",
            "threshold_low_f": "real",
            "threshold_high_f": "real",
            "label_value": "real",
            "outcome_status": "text",
            "reason": "text",
            "provenance_json": "text",
            "raw_excerpt": "text",
        },
    )
    ensure_columns(
        db,
        "paper_settlements",
        {
            "source_training_row_id": "integer",
            "label_attempt_id": "integer",
            "label_source": "text",
            "final_high_f": "real",
            "event_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "source_observation_snapshots",
        {
            "source_provider": "text",
            "target_metric": "text",
            "observed_f": "real",
        },
    )
    db.execute("update source_observation_snapshots set observed_f=observed_high_f where observed_f is null and observed_high_f is not null")
    db.execute("update source_observation_snapshots set target_metric=? where target_metric is null and observed_high_f is not null", (TARGET_METRIC_DAILY_HIGH,))
    db.execute(
        "update source_observation_snapshots set source_provider=provider where source_provider is null and provider is not null"
    )
    ensure_columns(
        db,
        "orderbook_snapshots",
        {
            "quote_age_seconds": "real",
            "stale_book_flag": "integer",
        },
    )
    ensure_columns(
        db,
        "paper_orders",
        {
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "paper_fills",
        {
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "shadow_orders",
        {
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "shadow_fills",
        {
            "event_key": "text",
            "candidate_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "paper_positions",
        {
            "event_key": "text",
            "strategy_family": "text",
        },
    )
    ensure_columns(
        db,
        "events",
        {
            "source_disagreement_f": "real",
        },
    )
    ensure_columns(
        db,
        "lifecycle_attribution",
        {
            "paper_settlement_id": "integer",
            "calibration_row_id": "integer",
        },
    )
    ensure_columns(
        db,
        "calibration_rows",
        {
            "label_attempt_id": "integer",
            "target_metric": "text",
            "final_observed_f": "real",
            "label_confidence": "text",
            "provider_set": "text",
        },
    )
    db.executescript(
        """
        create index if not exists idx_source_obs_provider_status on source_observation_snapshots(source_provider, provider, status);
        create index if not exists idx_signals_title_outcome_id on signals(title, outcome, id);
        create index if not exists idx_signals_signaltype_edge_market_entry on signals(signal_type, edge, market_prob, entry_price);
        create index if not exists idx_training_label_status_value on training_rows(label_status, label_value);
        create index if not exists idx_training_signal_type on training_rows(signal_type);
        create index if not exists idx_training_created_at on training_rows(created_at);
        create index if not exists idx_training_edge on training_rows(edge);
        create index if not exists idx_training_target_date_label on training_rows(target_date, label_status);
        create index if not exists idx_training_labeler_candidates
          on training_rows(target_date, label_status, market_family, market_id, outcome, station_id, target_metric, id);
        create index if not exists idx_training_strategy_family on training_rows(strategy_family);
        create index if not exists idx_calibration_conf_family on calibration_rows(label_confidence, label_source, strategy_family);
        create index if not exists idx_calibration_event on calibration_rows(event_key, training_row_id);
        create index if not exists idx_label_attempts_status_provider on label_attempts(outcome_status, source_provider);
        create index if not exists idx_label_attempts_candidate_metric
          on label_attempts(market_id, outcome, target_date, station_id, target_metric, attempted_at);
        """
    )
    seed_station_registry(db)
    ensure_paper_account(db, utc_now_iso())
    db.commit()
    return db


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"pragma table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            db.execute(f"alter table {table} add column {name} {decl}")


def seed_station_registry(db: sqlite3.Connection) -> None:
    now = utc_now_iso()
    for key, meta in STATION_REGISTRY.items():
        db.execute(
            """
            insert into station_registry(
              city_key, city_name, station_id, station_name, source_url,
              latitude, longitude, elevation_m, timezone, reliability, active, updated_at
            )
            values(?,?,?,?,?,?,?,?,?,?,1,?)
            on conflict(city_key) do update set
              station_id=excluded.station_id,
              station_name=excluded.station_name,
              timezone=excluded.timezone,
              reliability=excluded.reliability,
              updated_at=excluded.updated_at
            """,
            (
                key,
                key.title(),
                meta.get("station_id"),
                meta.get("station_name"),
                meta.get("source_url"),
                meta.get("latitude"),
                meta.get("longitude"),
                meta.get("elevation_m"),
                meta.get("timezone"),
                meta.get("source_reliability", "medium"),
                now,
            ),
        )


def normalize_target_metric(value: str | None) -> str:
    text = str(value or TARGET_METRIC_DAILY_HIGH).strip().lower().replace("-", "_")
    return text if text in TARGET_METRICS else TARGET_METRIC_UNKNOWN


def label_confidence_for_attempt(outcome_status: str | None, provider_set: str | None = "") -> str:
    return truth_tiers.tier_for_label(outcome_status, provider_set)


def provider_set_json(value: Any) -> str:
    return json.dumps(truth_tiers.normalized_provider_set(value), sort_keys=True)


def ensure_paper_account(
    db: sqlite3.Connection,
    now: str,
    starting_cash: float = DEFAULT_BANKROLL_USD,
    account_name: str | None = None,
) -> int:
    name = (account_name or active_paper_account_name()).strip() or DEFAULT_ACCOUNT_NAME
    row = db.execute("select id from paper_accounts where name=?", (name,)).fetchone()
    if row:
        return int(row[0])
    cur = db.execute(
        """
        insert into paper_accounts(name, starting_cash, cash, realized_pnl, created_at, updated_at)
        values(?,?,?,?,?,?)
        """,
        (name, starting_cash, starting_cash, 0.0, now, now),
    )
    return int(cur.lastrowid)


def record_forecast_snapshot(
    db: sqlite3.Connection,
    run_id: int,
    market_id: str,
    city: str | None,
    target_date: str | None,
    fetched_at: str,
    forecast_high_f: float | None,
    raw_status: str,
    raw_excerpt: str = "",
) -> int:
    cur = db.execute(
        """
        insert into forecast_snapshots(
          run_id, market_id, city, target_date, provider, fetched_at,
          forecast_high_f, raw_status, raw_excerpt
        )
        values(?,?,?,?,?,?,?,?,?)
        """,
        (run_id, market_id, city, target_date, "open_meteo", fetched_at, forecast_high_f, raw_status, raw_excerpt[:RAW_EXCERPT_LIMIT]),
    )
    return int(cur.lastrowid)


def record_station_observation(db: sqlite3.Connection, run_id: int, market_id: str, obs: StationObservation) -> int:
    cur = db.execute(
        """
        insert into station_observations(
          run_id, market_id, station_id, source, observed_at, local_date,
          current_temp_f, high_so_far_f, raw_status, excerpt
        )
        values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            market_id,
            obs.station_id,
            obs.source,
            obs.observed_at,
            obs.local_date,
            obs.current_temp_f,
            obs.high_so_far_f,
            obs.raw_status,
            obs.excerpt[:RAW_EXCERPT_LIMIT],
        ),
    )
    return int(cur.lastrowid)


def record_orderbook_snapshot(
    db: sqlite3.Connection,
    run_id: int | None,
    market_id: str | None,
    token_id: str | None,
    outcome: str | None,
    captured_at: str,
    quote: dict[str, Any],
    book: dict[str, Any] | None,
) -> int:
    cur = db.execute(
        """
        insert into orderbook_snapshots(
          run_id, market_id, token_id, outcome, captured_at, best_bid, best_ask,
          spread, depth_at_ask, depth_near_ask, depth_sufficient, raw_status, raw_json,
          quote_age_seconds, stale_book_flag
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            market_id,
            token_id,
            outcome,
            captured_at,
            quote.get("bid"),
            quote.get("ask"),
            quote.get("spread"),
            quote.get("depth"),
            quote.get("depth_near_ask"),
            1 if quote.get("depth_sufficient") else 0,
            quote.get("raw_status"),
            bounded_json(book or {}, RAW_JSON_LIMIT),
            quote.get("quote_age_seconds"),
            1 if quote.get("stale_book_flag") else 0,
        ),
    )
    return int(cur.lastrowid)


def event_exposure_summary(db: sqlite3.Connection, event_key: str | None) -> dict[str, Any]:
    if not event_key:
        return {"open_exposure": 0.0, "open_value": 0.0, "open_position_count": 0}
    row = db.execute(
        """
        select
          coalesce(sum(cost_basis), 0),
          coalesce(sum(shares * coalesce(latest_mark, 0)), 0),
          count(*)
        from paper_positions
        where status='open' and event_key=?
        """,
        (event_key,),
    ).fetchone()
    return {
        "open_exposure": float(row[0] or 0.0),
        "open_value": float(row[1] or 0.0),
        "open_position_count": int(row[2] or 0),
    }


def upsert_event(
    db: sqlite3.Connection,
    *,
    event_key: str,
    city: str | None,
    target_date: str | None,
    source: str | None,
    station_id: str | None,
    rule_hash: str | None,
    seen_at: str,
    latent_mean: float | None,
    latent_sigma: float | None,
    observed_high: float | None,
    local_day_complete: bool,
    contract_count: int,
    source_disagreement_f: float | None = None,
) -> None:
    exposure = event_exposure_summary(db, event_key)
    db.execute(
        """
        insert into events(
          event_key, city, target_date, source, station_id, rule_hash,
          first_seen, last_seen, latent_final_high_mean_f,
          latent_final_high_sigma_f, observed_high_f, local_day_complete,
          contract_count, open_exposure, open_position_count, source_disagreement_f
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(event_key) do update set
          city=excluded.city,
          target_date=excluded.target_date,
          source=excluded.source,
          station_id=excluded.station_id,
          rule_hash=excluded.rule_hash,
          last_seen=excluded.last_seen,
          latent_final_high_mean_f=excluded.latent_final_high_mean_f,
          latent_final_high_sigma_f=excluded.latent_final_high_sigma_f,
          observed_high_f=excluded.observed_high_f,
          local_day_complete=excluded.local_day_complete,
          contract_count=excluded.contract_count,
          open_exposure=excluded.open_exposure,
          open_position_count=excluded.open_position_count,
          source_disagreement_f=excluded.source_disagreement_f
        """,
        (
            event_key,
            city,
            target_date,
            source,
            station_id,
            rule_hash,
            seen_at,
            seen_at,
            latent_mean,
            latent_sigma,
            observed_high,
            1 if local_day_complete else 0,
            contract_count,
            exposure["open_exposure"],
            exposure["open_position_count"],
            source_disagreement_f,
        ),
    )
    db.execute(
        """
        insert into event_exposure_snapshots(
          event_key, captured_at, open_exposure, open_value, open_position_count,
          latent_final_high_mean_f, latent_final_high_sigma_f, observed_high_f,
          local_day_complete, source_disagreement_f
        )
        values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_key,
            seen_at,
            exposure["open_exposure"],
            exposure["open_value"],
            exposure["open_position_count"],
            latent_mean,
            latent_sigma,
            observed_high,
            1 if local_day_complete else 0,
            source_disagreement_f,
        ),
    )


def record_contract_payout(
    db: sqlite3.Connection,
    *,
    event_key: str,
    market_id: str | None,
    outcome: str | None,
    token_id: str | None,
    bucket: BucketSpec | None,
    spec: settlement_states.ContractSpec,
    payout_mapping: dict[str, Any],
    created_at: str,
) -> None:
    db.execute(
        """
        insert into contract_payouts(
          event_key, market_id, outcome, token_id, contract_type, side,
          bucket_lo_f, bucket_hi_f, threshold_f, payout_mapping_json, created_at
        )
        values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_key,
            market_id,
            outcome,
            token_id,
            spec.contract_type,
            spec.side,
            finite_or_none(bucket.lo if bucket else None),
            finite_or_none(bucket.hi if bucket else None),
            spec.threshold_f,
            bounded_json(payout_mapping, RAW_JSON_LIMIT),
            created_at,
        ),
    )


def upsert_lifecycle_candidate(
    db: sqlite3.Connection,
    *,
    candidate_key: str,
    event_key: str | None,
    strategy_family: str,
    market_id: str | None,
    outcome: str | None,
    signal_id: int | None,
    training_row_id: int | None,
    source_run_id: int | None,
    now: str,
) -> None:
    row = db.execute("select id from lifecycle_attribution where candidate_key=?", (candidate_key,)).fetchone()
    if row:
        db.execute(
            """
            update lifecycle_attribution
            set event_key=?, strategy_family=?, market_id=?, outcome=?,
                signal_id=coalesce(?, signal_id), training_row_id=coalesce(?, training_row_id),
                source_run_id=coalesce(?, source_run_id), updated_at=?
            where candidate_key=?
            """,
            (event_key, strategy_family, market_id, outcome, signal_id, training_row_id, source_run_id, now, candidate_key),
        )
        return
    db.execute(
        """
        insert into lifecycle_attribution(
          candidate_key, event_key, strategy_family, market_id, outcome,
          signal_id, training_row_id, source_run_id, created_at, updated_at
        )
        values(?,?,?,?,?,?,?,?,?,?)
        """,
        (candidate_key, event_key, strategy_family, market_id, outcome, signal_id, training_row_id, source_run_id, now, now),
    )


def update_lifecycle_links(db: sqlite3.Connection, candidate_key: str | None = None, signal_id: int | None = None, **links: Any) -> None:
    allowed = {
        "order_id",
        "fill_id",
        "position_id",
        "label_attempt_id",
        "label_status",
        "label_value",
        "paper_settlement_id",
        "calibration_row_id",
        "strategy_family",
        "event_key",
    }
    updates = {key: value for key, value in links.items() if key in allowed and value is not None}
    if not updates:
        return
    updates["updated_at"] = utc_now_iso()
    set_clause = ", ".join(f"{key}=?" for key in updates)
    params = list(updates.values())
    if candidate_key:
        db.execute(f"update lifecycle_attribution set {set_clause} where candidate_key=?", (*params, candidate_key))
    elif signal_id is not None:
        db.execute(f"update lifecycle_attribution set {set_clause} where signal_id=?", (*params, signal_id))


def event_time_bucket_from_features(features_json: str | None) -> str:
    try:
        payload = json.loads(features_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    minutes = payload.get("minutes_until_local_end_of_day")
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        return "unknown"
    if value <= 60:
        return "0-1h_to_close"
    if value <= 360:
        return "1-6h_to_close"
    if value <= 720:
        return "6-12h_to_close"
    return "12h+_to_close"


def record_calibration_rows_for_training_ids(
    db: sqlite3.Connection,
    row_ids: list[int],
    *,
    label_source: str | None,
    now: str,
    label_attempt_id: int | None = None,
) -> int:
    if not row_ids:
        return 0
    placeholders = ",".join("?" for _ in row_ids)
    attempt_meta = None
    if label_attempt_id is not None:
        attempt_meta = db.execute(
            """
            select outcome_status, source_provider, target_metric, final_observed_f, final_high_f
            from label_attempts where id=?
            """,
            (label_attempt_id,),
        ).fetchone()
    confidence = truth_tiers.TruthTier.OFFICIAL_NCEI.value
    provider_set = ""
    target_metric = TARGET_METRIC_DAILY_HIGH
    final_observed_f = None
    if attempt_meta is not None:
        confidence = label_confidence_for_attempt(attempt_meta[0], attempt_meta[1])
        providers = truth_tiers.normalized_provider_set(attempt_meta[1])
        provider_set = "+".join(providers)
        target_metric = normalize_target_metric(attempt_meta[2])
        final_observed_f = attempt_meta[3] if attempt_meta[3] is not None else attempt_meta[4]
        if confidence not in {truth_tiers.TruthTier.OFFICIAL_NCEI.value, truth_tiers.TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value}:
            return 0
    rows = db.execute(
        f"""
        select id, event_key, strategy_family, contract_type, market_family,
               model_prob, label_value, features_json, candidate_key,
               coalesce(target_metric, ?)
        from training_rows
        where id in ({placeholders})
          and model_prob is not null
          and label_value in (0, 1)
        """,
        [TARGET_METRIC_DAILY_HIGH, *row_ids],
    ).fetchall()
    count = 0
    for row in rows:
        prob = max(1e-6, min(1.0 - 1e-6, float(row[5])))
        label_value = float(row[6])
        row_target_metric = normalize_target_metric(target_metric or row[9])
        brier = (prob - label_value) * (prob - label_value)
        log_loss = -(label_value * math.log(prob) + (1.0 - label_value) * math.log(1.0 - prob))
        cur = db.execute(
            """
            insert into calibration_rows(
              training_row_id, event_key, strategy_family, contract_type,
              market_family, event_time_bucket, prediction_prob, label_value,
              brier, log_loss, label_source, created_at, label_attempt_id,
              target_metric, final_observed_f, label_confidence, provider_set
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(training_row_id) do update set
              event_key=excluded.event_key,
              strategy_family=excluded.strategy_family,
              contract_type=excluded.contract_type,
              market_family=excluded.market_family,
              event_time_bucket=excluded.event_time_bucket,
              prediction_prob=excluded.prediction_prob,
              label_value=excluded.label_value,
              brier=excluded.brier,
              log_loss=excluded.log_loss,
              label_source=excluded.label_source,
              created_at=excluded.created_at,
              label_attempt_id=excluded.label_attempt_id,
              target_metric=excluded.target_metric,
              final_observed_f=excluded.final_observed_f,
              label_confidence=excluded.label_confidence,
              provider_set=excluded.provider_set
            """,
            (
                row[0],
                row[1],
                row[2] or "unknown",
                row[3] or "unknown",
                row[4],
                event_time_bucket_from_features(row[7]),
                prob,
                label_value,
                brier,
                log_loss,
                label_source,
                now,
                label_attempt_id,
                row_target_metric,
                final_observed_f,
                confidence,
                provider_set,
            ),
        )
        calibration_id = int(cur.lastrowid or 0)
        existing = db.execute("select id from calibration_rows where training_row_id=?", (row[0],)).fetchone()
        if existing:
            calibration_id = int(existing[0])
        update_lifecycle_links(
            db,
            candidate_key=row[8],
            calibration_row_id=calibration_id,
            label_attempt_id=label_attempt_id,
            label_status=FINAL_LABEL_STATUS,
            label_value=label_value,
        )
        count += 1
    return count


def record_training_row(
    db: sqlite3.Connection,
    run_id: int,
    signal_id: int,
    created_at: str,
    m: dict[str, Any],
    outcome: str,
    token_id: str | None,
    city: str | None,
    target_date: str | None,
    station_id: str | None,
    station_source: str | None,
    forecast_snapshot_id: int | None,
    observation_id: int | None,
    orderbook_snapshot_id: int | None,
    bucket: BucketSpec | None,
    bucket_state: str,
    quote: dict[str, Any],
    market_prob: float | None,
    model_prob: float | None,
    edge: float | None,
    signal_type: str,
    reason: str,
    source_conf: str,
    family: str,
    eligibility: str,
    ease: float,
    uncertainty: float,
    req_edge: float,
    event_key: str | None = None,
    candidate_key: str | None = None,
    strategy_family: str = "unknown",
    contract_type: str | None = None,
    settlement_state: str | None = None,
    early_state: str | None = None,
    final_state: str | None = None,
    payout_mapping: dict[str, Any] | None = None,
    complement_arb_edge: float | None = None,
    complement_arb_status: str | None = None,
    ladder_violation_type: str | None = None,
    correction_trade: str | None = None,
    latent_final_high_mean_f: float | None = None,
    latent_final_high_sigma_f: float | None = None,
    local_day_complete: bool | None = None,
) -> int:
    event_key = event_key or m.get("event_key")
    candidate_key = candidate_key or candidate_key_for(event_key, m.get("market_id"), outcome, token_id, created_at)
    strategy_family = strategy_family if strategy_family in STRATEGY_FAMILIES else "unknown"
    payout_mapping = payout_mapping or {}
    feature_payload = features.build_decision_features(
        decision_time=created_at,
        market={
            "created_at": created_at,
            "market_id": m.get("market_id"),
            "title": m.get("title"),
            "rules_text": m.get("rules_text"),
            "resolution_text": m.get("resolution_text"),
            "source_text": m.get("source_text"),
            "source_host": m.get("source_host"),
            "source_url": m.get("source_url"),
            "station_id": station_id,
            "station_source": station_source,
            "source_confidence": source_conf,
            "bucket_lo_f": bucket.lo if bucket else None,
            "bucket_hi_f": bucket.hi if bucket else None,
            "bucket_kind": bucket.kind if bucket else None,
            "bucket_state": bucket_state,
            "city": city,
            "target_date": target_date,
            "eligibility_class": eligibility,
            "edge": edge,
            "event_key": event_key,
            "strategy_family": strategy_family,
            "contract_type": contract_type,
            "settlement_state": settlement_state,
            "payout_mapping_json": bounded_json(payout_mapping, RAW_JSON_LIMIT),
        },
        forecast={
            "provider": "open_meteo",
            "fetched_at": created_at,
            "forecast_high_f": m.get("forecast_high_f"),
            "raw_status": "stored_snapshot",
        },
        observation={
            "provider": station_source or "weather_underground",
            "source": station_source or "weather_underground",
            "observed_at": created_at,
            "station_id": station_id,
            "current_temp_f": None,
            "high_so_far_f": m.get("observed_high_f"),
            "raw_status": "stored_snapshot",
        },
        orderbook={
            "captured_at": created_at,
            "execution_source": quote.get("execution_source"),
            "entry_price": quote.get("entry_price"),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "spread": quote.get("spread"),
            "depth": quote.get("depth"),
            "depth_near_ask": quote.get("depth_near_ask"),
            "depth_sufficient": quote.get("depth_sufficient"),
            "quote_age_seconds": quote.get("quote_age_seconds"),
            "stale_book_flag": quote.get("stale_book_flag"),
            "raw_status": quote.get("raw_status"),
        },
        source_records=[
            {"provider": "open_meteo", "family": "forecast", "status": "stored_snapshot", "fetched_at": created_at},
            {"provider": station_source or "weather_underground", "family": "observation", "status": "stored_snapshot", "observed_at": created_at},
            {"provider": quote.get("execution_source"), "family": "orderbook", "status": quote.get("raw_status"), "captured_at": created_at},
        ],
        ladder={"captured_at": created_at, "ladder_group_key": "|".join(str(x or "") for x in (city, target_date, station_id))},
        tunables={
            "paper_size_shares": quote.get("paper_size_shares"),
            "max_spread": quote.get("max_spread"),
            "min_fill_shares": quote.get("min_fill_shares"),
        },
    )
    feature_payload.update(
        {
            "market_text": {
                "title": m.get("title"),
                "rules_text_hash": features.stable_hash(m.get("rules_text")),
                "resolution_text_hash": features.stable_hash(m.get("resolution_text")),
                "source_text_hash": features.stable_hash(m.get("source_text")),
            },
            "event_model": {
                "event_key": event_key,
                "latent_final_high_mean_f": latent_final_high_mean_f,
                "latent_final_high_sigma_f": latent_final_high_sigma_f,
                "contract_payout_mapping": payout_mapping,
                "local_day_complete": bool(local_day_complete),
            },
            "gates": {"required_edge": req_edge, "uncertainty_margin": uncertainty, "eligibility": eligibility, "reason": reason},
            "strategy": {"family": strategy_family, "candidate_key": candidate_key},
        }
    )
    cur = db.execute(
        """
        insert into training_rows(
          run_id, signal_id, created_at, market_id, title, outcome, token_id,
          city, target_date, station_id, station_source, source_url, provider,
          forecast_snapshot_id, observation_id, orderbook_snapshot_id, market_family,
          eligibility_class, source_confidence, bucket_lo_f, bucket_hi_f, bucket_kind,
          bucket_state, market_prob, model_prob, entry_price, bid, ask, spread, depth,
          depth_sufficient, edge, required_edge, uncertainty_margin, ease_score,
          signal_type, strategy_family, event_key, candidate_key, contract_type,
          settlement_state, early_state, final_state, payout_mapping_json,
          quote_age_seconds, stale_book_flag, complement_arb_edge,
          complement_arb_status, ladder_violation_type, correction_trade,
          latent_final_high_mean_f, latent_final_high_sigma_f, local_day_complete,
          reason, features_json
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            signal_id,
            created_at,
            m.get("market_id"),
            m.get("title"),
            outcome,
            token_id,
            city,
            target_date,
            station_id,
            station_source,
            m.get("source_url"),
            "open_meteo",
            forecast_snapshot_id,
            observation_id,
            orderbook_snapshot_id,
            family,
            eligibility,
            source_conf,
            bucket.lo if bucket else None,
            bucket.hi if bucket else None,
            bucket.kind if bucket else None,
            bucket_state,
            market_prob,
            model_prob,
            quote.get("entry_price"),
            quote.get("bid"),
            quote.get("ask"),
            quote.get("spread"),
            quote.get("depth"),
            1 if quote.get("depth_sufficient") else 0,
            edge,
            req_edge,
            uncertainty,
            ease,
            signal_type,
            strategy_family,
            event_key,
            candidate_key,
            contract_type,
            settlement_state,
            early_state,
            final_state,
            bounded_json(payout_mapping, RAW_JSON_LIMIT),
            quote.get("quote_age_seconds"),
            1 if quote.get("stale_book_flag") else 0,
            complement_arb_edge,
            complement_arb_status,
            ladder_violation_type,
            correction_trade,
            latent_final_high_mean_f,
            latent_final_high_sigma_f,
            1 if local_day_complete else 0,
            reason,
            bounded_json(feature_payload, RAW_JSON_LIMIT),
        ),
    )
    training_row_id = int(cur.lastrowid)
    upsert_lifecycle_candidate(
        db,
        candidate_key=candidate_key,
        event_key=event_key,
        strategy_family=strategy_family,
        market_id=m.get("market_id"),
        outcome=outcome,
        signal_id=signal_id,
        training_row_id=training_row_id,
        source_run_id=run_id,
        now=created_at,
    )
    return training_row_id


def account_row(db: sqlite3.Connection) -> sqlite3.Row | tuple[Any, ...]:
    name = active_paper_account_name()
    row = db.execute("select id, starting_cash, cash, realized_pnl from paper_accounts where name=?", (name,)).fetchone()
    if not row:
        ensure_paper_account(db, utc_now_iso(), account_name=name)
        row = db.execute("select id, starting_cash, cash, realized_pnl from paper_accounts where name=?", (name,)).fetchone()
    return row


def open_exposure(db: sqlite3.Connection, account_id: int) -> float:
    row = db.execute(
        "select coalesce(sum(cost_basis),0) from paper_positions where account_id=? and status='open'",
        (account_id,),
    ).fetchone()
    return float(row[0] or 0.0)


def city_date_exposure(db: sqlite3.Connection, account_id: int, city: str | None, target_date: str | None) -> float:
    row = db.execute(
        """
        select coalesce(sum(cost_basis),0)
        from paper_positions
        where account_id=? and status='open' and coalesce(city,'')=coalesce(?, '') and coalesce(target_date,'')=coalesce(?, '')
        """,
        (account_id, city, target_date),
    ).fetchone()
    return float(row[0] or 0.0)


def settlement_source_key(provider: str | None, station_id: str | None, source_url: str | None = None) -> str:
    provider_part = re.sub(r"[^a-z0-9]+", "_", str(provider or "unknown").strip().lower()).strip("_") or "unknown"
    station_part = re.sub(r"[^a-z0-9]+", "_", str(station_id or "unknown").strip().lower()).strip("_") or "unknown"
    if source_url and station_part == "unknown":
        parsed = urllib.parse.urlparse(str(source_url))
        station_part = re.sub(r"[^a-z0-9]+", "_", (parsed.netloc or parsed.path or "source").lower()).strip("_") or "source"
    return f"{provider_part}:{station_part}"


def upsert_settlement_source_registry(
    db: sqlite3.Connection,
    *,
    provider: str | None,
    family: str | None,
    station_id: str | None,
    source_url: str | None,
    canonical_name: str | None = None,
    priority: int = 100,
    now: str | None = None,
) -> str:
    now = now or utc_now_iso()
    key = settlement_source_key(provider, station_id, source_url)
    db.execute(
        """
        insert into settlement_source_registry(source_key, canonical_name, family, provider, station_id, source_url, priority, active, created_at, updated_at)
        values(?,?,?,?,?,?,?,?,?,?)
        on conflict(source_key) do update set
          canonical_name=excluded.canonical_name,
          family=excluded.family,
          provider=excluded.provider,
          station_id=excluded.station_id,
          source_url=coalesce(excluded.source_url, settlement_source_registry.source_url),
          priority=excluded.priority,
          active=1,
          updated_at=excluded.updated_at
        """,
        (key, canonical_name or str(provider or "unknown"), family or "observation", provider or "unknown", station_id, source_url, priority, 1, now, now),
    )
    return key


def record_source_observation_snapshot(
    db: sqlite3.Connection,
    *,
    run_id: int | None,
    market_id: str | None,
    event_key: str | None,
    source_record: Any | None = None,
    provider: str | None = None,
    family: str | None = None,
    status: str | None = None,
    station_id: str | None = None,
    observed_at: str | None = None,
    fetched_at: str | None = None,
    local_date: str | None = None,
    target_metric: str | None = None,
    observed_f: float | None = None,
    observed_high_f: float | None = None,
    current_temp_f: float | None = None,
    source_url: str | None = None,
    provenance: dict[str, Any] | None = None,
    raw_excerpt: str | None = None,
    error: str | None = None,
    now: str | None = None,
) -> int:
    now = now or utc_now_iso()
    data = getattr(source_record, "data", None) if source_record is not None else None
    data = data if isinstance(data, dict) else {}
    provider = provider or getattr(source_record, "provider", None) or "unknown"
    family = family or getattr(source_record, "family", None) or "observation"
    status = status or getattr(source_record, "status", None) or "skipped"
    fetched_at = fetched_at or getattr(source_record, "fetched_at", None) or now
    source_url = source_url or getattr(source_record, "source_url", None)
    error = error or getattr(source_record, "error", None)
    provenance = provenance or getattr(source_record, "provenance", None) or {}
    station_id = station_id or data.get("station_id")
    observed_at = observed_at or data.get("observed_at") or data.get("obs_time_utc")
    local_date = local_date or data.get("local_date")
    target_metric = normalize_target_metric(target_metric or data.get("target_metric") or TARGET_METRIC_DAILY_HIGH)
    if observed_f is None:
        observed_f = data.get("observed_f")
    if observed_f is None and target_metric == TARGET_METRIC_DAILY_LOW:
        observed_f = data.get("daily_low_f", data.get("low_so_far_f"))
    observed_high_f = observed_high_f if observed_high_f is not None else data.get("daily_high_f", data.get("high_so_far_f"))
    if observed_f is None:
        observed_f = observed_high_f
    current_temp_f = current_temp_f if current_temp_f is not None else data.get("current_temp_f")
    source_key = upsert_settlement_source_registry(db, provider=provider, family=family, station_id=station_id, source_url=source_url, now=now)
    raw_excerpt = None if raw_excerpt is None else str(raw_excerpt)[:RAW_EXCERPT_LIMIT]
    cur = db.execute(
        """
        insert into source_observation_snapshots(
          run_id, market_id, event_key, source_key, provider, source_provider, family, status, station_id,
          observed_at, fetched_at, local_date, target_metric, observed_f, observed_high_f, current_temp_f, source_url,
          provenance_json, raw_excerpt, error, created_at
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, market_id, event_key, source_key, provider, provider, family, status, station_id,
            observed_at, fetched_at, local_date, target_metric,
            float(observed_f) if observed_f is not None else None,
            float(observed_high_f) if observed_high_f is not None else None,
            float(current_temp_f) if current_temp_f is not None else None,
            source_url, bounded_json(provenance, RAW_JSON_LIMIT), raw_excerpt, error, now,
        ),
    )
    return int(cur.lastrowid)


def source_delta_features(db: sqlite3.Connection, event_key: str | None, market_id: str | None = None) -> dict[str, Any]:
    if not event_key and not market_id:
        return {"source_delta_snapshot_count": 0, "source_delta_status": "missing_event"}
    rows = db.execute(
        """
        select provider, status, observed_high_f, station_id, source_key, fetched_at
        from source_observation_snapshots
        where (? is null or event_key=?) and (? is null or market_id=?)
        order by fetched_at desc, id desc
        limit 10
        """,
        (event_key, event_key, market_id, market_id),
    ).fetchall()
    highs = [float(r[2]) for r in rows if r[2] is not None]
    providers = sorted({str(r[0]) for r in rows if r[0]})
    delta = max(highs) - min(highs) if len(highs) >= 2 else None
    return {
        "source_delta_snapshot_count": len(rows),
        "source_delta_provider_count": len(providers),
        "source_delta_providers": providers,
        "source_delta_high_range_f": delta,
        "source_delta_abs_max_f": max((abs(highs[i] - highs[j]) for i in range(len(highs)) for j in range(i)), default=None),
        "source_delta_status": "ready" if len(highs) >= 2 else "insufficient_sources",
        "source_delta_primary_station_id": rows[0][3] if rows else None,
        "source_delta_primary_source_key": rows[0][4] if rows else None,
    }


def source_delta_probability(delta_f: float | None, *, calibration_count: int = 0) -> tuple[float | None, str]:
    if calibration_count <= 0:
        return None, "source_delta_shadow_no_calibration"
    if delta_f is None:
        return None, "source_delta_insufficient_sources"
    return max(0.05, min(0.95, 0.5 + min(delta_f, 10.0) / 40.0)), "source_delta_deterministic_calibrated"


def source_delta_official_guard(db: sqlite3.Connection, *, station_id: str | None, source_key: str | None = None) -> tuple[bool, str]:
    if not station_id or not source_key:
        return False, "source_delta_ambiguous_station_or_source"
    return False, "settlement_source_delta_official_locked"


def refresh_station_residuals(db: sqlite3.Connection, *, now: str | None = None) -> int:
    now = now or utc_now_iso()
    rows = db.execute(
        """
        select coalesce(tr.station_id, la.station_id) as station_id,
               coalesce(sos.source_key, la.source_provider) as source_key,
               coalesce(tr.strategy_family, 'unknown') as strategy_family,
               (coalesce(cr.final_observed_f, la.final_observed_f, la.final_high_f) - sos.observed_f) as residual_f,
               coalesce(la.attempted_at, cr.created_at) as label_at
        from calibration_rows cr
        join training_rows tr on tr.id=cr.training_row_id
        left join label_attempts la on la.id=cr.label_attempt_id
        join source_observation_snapshots sos
          on (sos.event_key=tr.event_key or sos.market_id=tr.market_id)
         and coalesce(nullif(sos.target_metric, ?), ?) = coalesce(nullif(cr.target_metric, ?), nullif(tr.target_metric, ?), nullif(la.target_metric, ?), ?)
         and sos.observed_f is not null
        where coalesce(cr.label_confidence, ?) in (?, ?)
          and coalesce(cr.final_observed_f, la.final_observed_f, la.final_high_f) is not null
          and coalesce(tr.station_id, la.station_id, '') <> ''
        """,
        (
            TARGET_METRIC_UNKNOWN,
            TARGET_METRIC_DAILY_HIGH,
            TARGET_METRIC_UNKNOWN,
            TARGET_METRIC_UNKNOWN,
            TARGET_METRIC_UNKNOWN,
            TARGET_METRIC_DAILY_HIGH,
            truth_tiers.TruthTier.UNKNOWN.value,
            truth_tiers.TruthTier.OFFICIAL_NCEI.value,
            truth_tiers.TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value,
        ),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[tuple[float, str | None]]] = {}
    for station_id, source_key, strategy_family, residual, label_at in rows:
        if residual is None:
            continue
        grouped.setdefault((str(station_id), str(source_key or "unknown"), str(strategy_family or "unknown")), []).append((float(residual), label_at))
    for (station_id, source_key, strategy_family), vals in grouped.items():
        residuals = [v[0] for v in vals]
        mean = sum(residuals) / len(residuals)
        mae = sum(abs(v) for v in residuals) / len(residuals)
        rmse = math.sqrt(sum(v * v for v in residuals) / len(residuals))
        last_label_at = max((v[1] for v in vals if v[1]), default=None)
        db.execute(
            """
            insert into station_residuals(station_id, source_key, strategy_family, sample_count, mean_residual_f, mae_f, rmse_f, last_label_at, updated_at)
            values(?,?,?,?,?,?,?,?,?)
            on conflict(station_id, coalesce(source_key,''), coalesce(strategy_family,'')) do update set
              sample_count=excluded.sample_count,
              mean_residual_f=excluded.mean_residual_f,
              mae_f=excluded.mae_f,
              rmse_f=excluded.rmse_f,
              last_label_at=excluded.last_label_at,
              updated_at=excluded.updated_at
            """,
            (station_id, source_key, strategy_family, len(residuals), mean, mae, rmse, last_label_at, now),
        )
    return len(grouped)


def paper_buy_survival_gate_disabled(args: argparse.Namespace, strategy_family: str) -> bool:
    """Return True when paper-buy orders for a strategy family should be skipped.

    The gate is paper-only: it does not affect signal/training-row collection.
    By default, only KILL_OR_DISABLE families are prevented from consuming the
    paper ledger; INCONCLUSIVE and CONTINUE_OBSERVING remain in bootstrap
    evidence collection. Use --strict-survival-gate for promote-only fills.
    """
    if getattr(args, "allow_weak_families", False):
        return False
    if not getattr(args, "disable_weak_families", False):
        return False
    family = (strategy_family or "unknown").strip() or "unknown"
    cache = getattr(args, "_paper_buy_disabled_families", None)
    if cache is None:
        try:
            cache = edge_validation.disabled_families(DB_PATH, strict=getattr(args, "strict_survival_gate", False))
        except Exception as exc:  # dashboard/reporting data must not break scan collection
            cache = set()
            setattr(args, "_paper_buy_survival_gate_error", str(exc))
        setattr(args, "_paper_buy_disabled_families", cache)
    return family in cache


def record_shadow_survival_gate_fill(
    db: sqlite3.Connection,
    run_id: int,
    signal_id: int,
    created_at: str,
    m: dict[str, Any],
    outcome: str,
    token_id: str | None,
    signal_type: str,
    quote: dict[str, Any],
    requested: float,
    event_key: str | None,
    candidate_key: str | None,
    strategy_family: str,
    shadow_reason: str = "survival_gate_disabled",
) -> tuple[int | None, int | None]:
    """Record a separate hypothetical fill for a shadow-only family.

    Shadow rows do not touch account cash, positions, or official paper PnL.
    They preserve counterfactual executable evidence so a blocked or capped
    family can keep producing evidence without dominating official fills.
    """
    entry = quote.get("entry_price")
    if (
        not token_id
        or quote.get("execution_source") != "clob_book"
        or not quote.get("depth_sufficient")
        or entry is None
        or float(entry) <= 0.0
    ):
        return None, None
    depth = float(quote.get("depth") or 0.0)
    shares = min(float(requested or 0.0), depth)
    if shares <= 0.0:
        return None, None
    price = float(entry)
    cost = shares * price
    cur = db.execute(
        """
        insert into shadow_orders(
          run_id, signal_id, market_id, token_id, outcome, side, signal_type,
          requested_shares, limit_price, estimated_cost, shadow_reason, created_at,
          event_key, candidate_key, strategy_family
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            signal_id,
            m.get("market_id"),
            token_id,
            outcome,
            "buy_yes",
            signal_type,
            requested,
            entry,
            cost,
            shadow_reason,
            created_at,
            event_key,
            candidate_key,
            strategy_family,
        ),
    )
    shadow_order_id = int(cur.lastrowid)
    db.execute(
        """
        insert into shadow_fills(
          shadow_order_id, filled_at, shares, price, cost, slippage, source,
          raw_status, event_key, candidate_key, strategy_family
        )
        values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            shadow_order_id,
            created_at,
            shares,
            price,
            cost,
            price - float(quote.get("ask") or price),
            quote.get("execution_source"),
            quote.get("raw_status"),
            event_key,
            candidate_key,
            strategy_family,
        ),
    )
    return shadow_order_id, int(db.execute("select last_insert_rowid()").fetchone()[0])


def paper_buy_shadow_only_family(
    args: argparse.Namespace,
    family: str,
    *,
    db: sqlite3.Connection | None = None,
) -> tuple[bool, str | None]:
    if family == "settlement_source_delta":
        return True, "settlement_source_delta_shadow_locked"
    if family == "ladder_inconsistency":
        return ladder_shadow_gate(db, args)
    configured = getattr(args, "shadow_strategy_families", None)
    if isinstance(configured, str):
        families = {item.strip() for item in configured.split(",") if item.strip()}
    elif configured:
        families = {str(item).strip() for item in configured if str(item).strip()}
    else:
        families = set()
    if family in families:
        return True, "strategy_family_shadow_only"
    return False, None


def seed_touch_watchlist_candidate(
    db: sqlite3.Connection,
    *,
    event_key: str | None,
    market: dict[str, Any],
    outcome: str,
    token_id: str | None,
    city: str | None,
    station_id: str | None,
    target_date: str | None,
    strategy_family: str,
    contract_spec: settlement_states.ContractSpec,
    observed_high_f: float | None,
    quote: dict[str, Any],
    source_confidence: str | None,
    timezone_name: str | None,
    now_iso: str,
) -> None:
    """Seed/update the hot touch watchlist from ordinary scanner evidence.

    This is paper-only observation plumbing. It does not increase polling cadence
    by itself and does not place orders; the separate watcher consumes active rows.
    """
    threshold = contract_spec.threshold_f
    if threshold is None or not token_id or not event_key:
        return
    candidate = {
        "event_key": event_key,
        "market_id": market.get("market_id"),
        "token_id": token_id,
        "city": city,
        "station_id": station_id,
        "target_date": target_date,
        "strategy_family": strategy_family,
        "contract_type": contract_spec.contract_type,
        "threshold_f": threshold,
        "side": contract_spec.side,
        "source_confidence": source_confidence,
    }
    hotness = touch_watchlist.compute_hotness_score(
        candidate,
        {"high_so_far_f": observed_high_f, "confidence_class": source_confidence},
        quote,
        {"local_hour": station_local_hour(timezone_name)},
    )
    touch_watchlist.upsert_touch_watchlist(db, candidate, hotness)


def official_paper_fill_gate(args: argparse.Namespace, quote: dict[str, Any]) -> tuple[bool, str]:
    entry = quote.get("entry_price")
    try:
        entry_float = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry_float = None
    if entry_float is None or entry_float <= 0.0:
        return False, "missing_entry_price"
    if entry_float < float(getattr(args, "min_entry", 0.02)):
        return False, "below_min_entry_shadow_only"
    if entry_float > float(getattr(args, "max_entry", 0.95)):
        return False, "above_max_entry"
    if quote.get("execution_source") != "clob_book":
        return False, "execution_not_clob_book"
    if not bool(quote.get("depth_sufficient")):
        return False, "insufficient_executable_depth"
    if quote.get("quote_age_seconds") is None:
        return False, "missing_quote_age"
    if bool(quote.get("stale_book_flag")):
        return False, "stale_book"
    spread = quote.get("spread")
    if spread is not None and float(spread) > float(getattr(args, "max_spread", 1.0)):
        return False, "spread_above_max_spread"
    return True, "paper_fill_executable_clob"


def ladder_shadow_gate(db: sqlite3.Connection | None, args: argparse.Namespace) -> tuple[bool, str | None]:
    if db is not None and table_exists(db, "calibration_rows") and table_count(db, "calibration_rows") < 300:
        return True, "ladder_shadow_until_labels"
    if getattr(args, "shadow_ladder_inconsistency", True):
        return True, "ladder_inconsistency_shadow_only"
    return False, None


def simulate_paper_order(
    db: sqlite3.Connection,
    args: argparse.Namespace,
    run_id: int,
    signal_id: int,
    created_at: str,
    m: dict[str, Any],
    outcome: str,
    token_id: str | None,
    city: str | None,
    target_date: str | None,
    signal_type: str,
    quote: dict[str, Any],
    event_key: str | None = None,
    candidate_key: str | None = None,
    strategy_family: str = "unknown",
) -> dict[str, int | None]:
    result: dict[str, int | None] = {"order_id": None, "fill_id": None, "position_id": None}
    if getattr(args, "disable_ledger", False):
        return result
    if not signal_type.startswith("paper_buy"):
        return result
    account = account_row(db)
    account_id, starting_cash, cash, _realized = int(account[0]), float(account[1]), float(account[2]), float(account[3])
    entry = quote.get("entry_price")
    entry_float: float | None = None
    try:
        entry_float = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry_float = None
    requested = float(getattr(args, "paper_size", 0.0) or 0.0)
    shadow_only, shadow_reason = paper_buy_shadow_only_family(args, strategy_family, db=db)
    official_allowed, official_reason = official_paper_fill_gate(args, quote)
    status = "skipped"
    reason = ""
    shares = 0.0
    if not token_id:
        reason = "missing_token_id"
    elif entry_float is None or entry_float <= 0.0:
        reason = "missing_entry_price"
    elif not official_allowed and official_reason == "below_min_entry_shadow_only":
        reason = official_reason
        record_shadow_survival_gate_fill(
            db,
            run_id,
            signal_id,
            created_at,
            m,
            outcome,
            token_id,
            signal_type,
            quote,
            requested,
            event_key,
            candidate_key,
            strategy_family,
            official_reason,
        )
    elif shadow_only:
        reason = "strategy_family_shadow_only"
        record_shadow_survival_gate_fill(
            db,
            run_id,
            signal_id,
            created_at,
            m,
            outcome,
            token_id,
            signal_type,
            quote,
            requested,
            event_key,
            candidate_key,
            strategy_family,
            shadow_reason or "strategy_family_shadow_only",
        )
    elif paper_buy_survival_gate_disabled(args, strategy_family):
        reason = "strategy_family_survival_gate_disabled"
        record_shadow_survival_gate_fill(
            db,
            run_id,
            signal_id,
            created_at,
            m,
            outcome,
            token_id,
            signal_type,
            quote,
            requested,
            event_key,
            candidate_key,
            strategy_family,
        )
    elif not official_allowed:
        reason = official_reason
    else:
        position_cap = starting_cash * float(getattr(args, "max_position_pct", 0.02))
        city_cap_remaining = starting_cash * float(getattr(args, "max_city_date_pct", 0.10)) - city_date_exposure(db, account_id, city, target_date)
        open_cap_remaining = starting_cash * float(getattr(args, "max_open_exposure_pct", 0.50)) - open_exposure(db, account_id)
        cash_cap = min(cash, position_cap, city_cap_remaining, open_cap_remaining)
        max_affordable_shares = max(0.0, cash_cap / entry_float)
        depth = float(quote.get("depth") or 0.0)
        shares = min(requested, depth, max_affordable_shares)
        min_fill = float(getattr(args, "min_fill_shares", 1.0))
        if shares + 1e-9 < min_fill:
            reason = "risk_cap_cash_or_depth_below_min_fill"
        else:
            status = "filled"
            reason = "paper_fill_executable_clob"
    estimated_cost = shares * float(entry or 0.0)
    cur = db.execute(
        """
        insert into paper_orders(
          run_id, signal_id, account_id, market_id, token_id, outcome, side,
          signal_type, status, requested_shares, limit_price, estimated_cost,
          reason, created_at, event_key, candidate_key, strategy_family
        )
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            signal_id,
            account_id,
            m.get("market_id"),
            token_id,
            outcome,
            "buy_yes",
            signal_type,
            status,
            requested,
            entry,
            estimated_cost,
            reason,
            created_at,
            event_key,
            candidate_key,
            strategy_family,
        ),
    )
    order_id = int(cur.lastrowid)
    result["order_id"] = order_id
    update_lifecycle_links(db, candidate_key=candidate_key, signal_id=signal_id, order_id=order_id)
    if status != "filled":
        return result
    price = float(entry)
    cost = shares * price
    if cost > cash + 1e-9:
        db.execute("update paper_orders set status='skipped', reason='cash_guard_triggered' where id=?", (order_id,))
        return result
    db.execute(
        """
        insert into paper_fills(order_id, filled_at, shares, price, cost, slippage, source, raw_status, event_key, candidate_key, strategy_family)
        values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            order_id,
            created_at,
            shares,
            price,
            cost,
            price - float(quote.get("ask") or price),
            quote.get("execution_source"),
            quote.get("raw_status"),
            event_key,
            candidate_key,
            strategy_family,
        ),
    )
    result["fill_id"] = int(db.execute("select last_insert_rowid()").fetchone()[0])
    db.execute("update paper_accounts set cash=cash-?, updated_at=? where id=?", (cost, created_at, account_id))
    existing = db.execute(
        "select id, shares, cost_basis from paper_positions where account_id=? and market_id=? and outcome=?",
        (account_id, m.get("market_id"), outcome),
    ).fetchone()
    if existing:
        pos_id, old_shares, old_cost = int(existing[0]), float(existing[1]), float(existing[2])
        new_shares = old_shares + shares
        new_cost = old_cost + cost
        db.execute(
            """
            update paper_positions
            set shares=?, avg_price=?, cost_basis=?, latest_mark=?, status='open',
                event_key=coalesce(?, event_key), strategy_family=coalesce(?, strategy_family),
                updated_at=?
            where id=?
            """,
            (new_shares, new_cost / new_shares, new_cost, price, event_key, strategy_family, created_at, pos_id),
        )
        result["position_id"] = pos_id
    else:
        cur = db.execute(
            """
            insert into paper_positions(
              account_id, market_id, token_id, title, outcome, city, target_date,
              shares, avg_price, cost_basis, realized_pnl, latest_mark, status, updated_at
              , event_key, strategy_family
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                account_id,
                m.get("market_id"),
                token_id,
                m.get("title"),
                outcome,
                city,
                target_date,
                shares,
                price,
                cost,
                0.0,
                price,
                "open",
                created_at,
                event_key,
                strategy_family,
            ),
        )
        result["position_id"] = int(cur.lastrowid)
    update_lifecycle_links(
        db,
        candidate_key=candidate_key,
        signal_id=signal_id,
        order_id=result["order_id"],
        fill_id=result["fill_id"],
        position_id=result["position_id"],
        strategy_family=strategy_family,
        event_key=event_key,
    )
    return result


def latest_signal_marks(db: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, float]]:
    rows = db.execute(
        """
        select s.market_id, s.outcome, s.id, coalesce(s.entry_price, s.market_prob)
        from signals s
        join (
          select market_id, outcome, max(id) latest_id
          from signals
          where coalesce(entry_price, market_prob) is not null
          group by market_id, outcome
        ) x on x.latest_id=s.id
        """
    ).fetchall()
    return {(str(r[0]), str(r[1])): (int(r[2]), float(r[3])) for r in rows if r[3] is not None}


def settle_paper_positions_from_latest_prices(db: sqlite3.Connection, now: str) -> None:
    account = account_row(db)
    account_id = int(account[0])
    marks = latest_signal_marks(db)
    positions = db.execute(
        """
        select id, market_id, outcome, shares, cost_basis, event_key, strategy_family
        from paper_positions
        where account_id=? and status='open'
        """,
        (account_id,),
    ).fetchall()
    for pos_id, market_id, outcome, shares, cost_basis, event_key, strategy_family in positions:
        latest = marks.get((str(market_id), str(outcome)))
        if not latest:
            continue
        source_signal_id, mark = latest
        status = token_resolution_for(mark)
        db.execute("update paper_positions set latest_mark=?, updated_at=? where id=?", (mark, now, pos_id))
        if status == "unresolved":
            continue
        payout = float(shares) if status == "resolved_win" else 0.0
        realized = payout - float(cost_basis)
        existing = db.execute(
            "select id, realized_pnl from paper_settlements where position_id=?", (pos_id,)
        ).fetchone()
        if existing:
            # Settlement already recorded — sync position status if it drifted to 'open'
            db.execute(
                "update paper_positions set status='settled', realized_pnl=?, updated_at=? where id=? and status='open'",
                (existing[1], now, pos_id),
            )
            continue
        db.execute(
            """
            insert into paper_settlements(position_id, settled_at, outcome_status, payout, realized_pnl, source_signal_id, event_key, strategy_family)
            values(?,?,?,?,?,?,?,?)
            """,
            (pos_id, now, status, payout, realized, source_signal_id, event_key, strategy_family),
        )
        db.execute(
            "update paper_positions set status='settled', realized_pnl=?, updated_at=? where id=?",
            (realized, now, pos_id),
        )
        db.execute(
            "update paper_accounts set cash=cash+?, realized_pnl=realized_pnl+?, updated_at=? where id=?",
            (payout, realized, now, account_id),
        )


def reconcile_zombie_positions(db: sqlite3.Connection, now: str) -> int:
    """Sync any position stuck in 'open' that already has a settlement record.

    This handles legacy/cutover scenarios where a settlement was recorded but
    the position status was never updated to 'settled'. Does NOT create new
    settlements — only syncs status to match existing settlement evidence.
    """
    zombies = db.execute(
        """
        select pp.id, ps.realized_pnl
        from paper_positions pp
        join paper_settlements ps on ps.position_id = pp.id
        where pp.status = 'open'
        """,
    ).fetchall()
    fixed = 0
    for pos_id, realized_pnl in zombies:
        db.execute(
            "update paper_positions set status='settled', realized_pnl=?, updated_at=? where id=?",
            (realized_pnl, now, pos_id),
        )
        fixed += 1
    return fixed


def portfolio_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    account = account_row(db)
    account_id, starting_cash, cash, realized = int(account[0]), float(account[1]), float(account[2]), float(account[3])
    positions = db.execute(
        """
        select shares, cost_basis, latest_mark, status
        from paper_positions
        where account_id=?
        """,
        (account_id,),
    ).fetchall()
    open_value = 0.0
    open_cost = 0.0
    unresolved = 0
    for shares, cost_basis, latest_mark, status in positions:
        if status == "open":
            unresolved += 1
            open_cost += float(cost_basis)
            mark = float(latest_mark) if latest_mark is not None else 0.0
            open_value += float(shares) * mark
    unrealized = open_value - open_cost
    equity = cash + open_value
    prior_high = db.execute(
        "select max(equity) from paper_account_snapshots where account_id=?",
        (account_id,),
    ).fetchone()[0]
    high_water = max(float(prior_high or starting_cash), equity, starting_cash)
    drawdown = 0.0 if high_water <= 0 else max(0.0, (high_water - equity) / high_water)
    return {
        "account_id": account_id,
        "account_name": active_paper_account_name(),
        "starting_cash": starting_cash,
        "cash": cash,
        "open_exposure": open_cost,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "equity": equity,
        "return_pct": (equity - starting_cash) / starting_cash if starting_cash else 0.0,
        "drawdown": drawdown,
        "unresolved_positions": unresolved,
    }


def record_account_snapshot(db: sqlite3.Connection, run_id: int | None, captured_at: str) -> None:
    metrics = portfolio_metrics(db)
    db.execute(
        """
        insert into paper_account_snapshots(
          account_id, run_id, captured_at, cash, open_exposure, realized_pnl,
          unrealized_pnl, equity, return_pct, drawdown, unresolved_positions
        )
        values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            metrics["account_id"],
            run_id,
            captured_at,
            metrics["cash"],
            metrics["open_exposure"],
            metrics["realized_pnl"],
            metrics["unrealized_pnl"],
            metrics["equity"],
            metrics["return_pct"],
            metrics["drawdown"],
            metrics["unresolved_positions"],
        ),
    )


def scan(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    data = fetch_json(args.url)
    markets = extract_markets(data)
    db = init_db(args.db)
    cur = db.execute("insert into runs(started_at, source_url, markets_seen, signals_seen) values (?, ?, 0, 0)", (now, args.url))
    run_id = int(cur.lastrowid)
    signal_count = 0
    for m in markets:
        m = dict(m)
        city, target_date = infer_city_date(m["title"], args.city)
        registry_station = station_registry_lookup_db(db, city, m.get("market_id"), m.get("title"))
        station_id = m.get("station_id") or (registry_station or {}).get("station_id")
        source_url = m.get("source_url") or (registry_station or {}).get("source_url")
        if source_url and not m.get("source_url"):
            m["source_url"] = source_url
            m["source_host"] = urllib.parse.urlparse(source_url).netloc.lower().removeprefix("www.")
        timezone_name = (registry_station or {}).get("timezone")
        station_source = "market_source" if m.get("station_id") else ("station_registry" if registry_station else None)
        source_conf_base = m.get("source_confidence") or "low"
        if registry_station and source_conf_base == "low":
            source_conf_base = "medium"
        family = market_family(m["title"], m.get("resolution_text"))
        title_bucket = parse_bucket(m["title"])
        market_bucket = title_bucket
        if market_bucket is None:
            for candidate_outcome in m.get("outcomes") or []:
                market_bucket = parse_bucket(str(candidate_outcome))
                if market_bucket is not None:
                    break
        rule_material = event_rule_text(m, market_bucket)
        rule_hash = features.stable_hash(rule_material) or "no-rule"
        event_key = event_key_for(city, target_date, m.get("source_host"), station_id, rule_material)
        m["event_key"] = event_key
        m["rule_hash"] = rule_hash
        market_level_eligibility = market_eligibility(family, source_conf_base, station_id, target_date, market_bucket, timezone_name)
        db.execute(
            "insert into markets(market_id,title,url,first_seen,last_seen) values(?,?,?,?,?) "
            "on conflict(market_id) do update set title=excluded.title,url=excluded.url,last_seen=excluded.last_seen",
            (m["market_id"], m["title"], m["url"], now, now),
        )
        db.execute(
            """
            update markets
            set rules_text=?, resolution_text=?, source_text=?, source_links=?,
                source_host=?, source_url=?, station_id=?, source_confidence=?,
                clob_token_ids=?, market_family=?, eligibility_class=?,
                event_key=?, rule_hash=?
            where market_id=?
            """,
            (
                m.get("rules_text"),
                m.get("resolution_text"),
                m.get("source_text"),
                m.get("source_links"),
                m.get("source_host"),
                m.get("source_url"),
                station_id,
                source_conf_base,
                json.dumps(m.get("token_ids") or [], ensure_ascii=True),
                family,
                market_level_eligibility,
                event_key,
                rule_hash,
                m["market_id"],
            ),
        )
        high = None
        forecast_status = "missing_city_or_date"
        forecast_excerpt = ""
        if city and target_date:
            try:
                geo = geocode(city)
                if geo:
                    city = ", ".join(x for x in [geo.get("name"), geo.get("admin1"), geo.get("country_code")] if x)
                    high = forecast_high_f(float(geo["latitude"]), float(geo["longitude"]), target_date)
                    forecast_status = "ok" if high is not None else "missing_forecast_high"
                    time.sleep(args.pause)
                else:
                    forecast_status = "geocode_missing"
            except Exception as exc:
                forecast_status = "weather_error"
                forecast_excerpt = str(exc)
                print(f"warn: weather baseline unavailable for {city}: {exc}", file=sys.stderr)
        forecast_snapshot_id = record_forecast_snapshot(
            db,
            run_id,
            m["market_id"],
            city,
            target_date,
            now,
            high,
            forecast_status,
            forecast_excerpt,
        )
        if getattr(args, "enable_wu", False) and (station_id or m.get("source_url")) and target_date:
            obs = fetch_wu_station_observation(station_id, target_date, m.get("source_url"), timezone_name)
        else:
            obs_status = "wu_disabled" if not getattr(args, "enable_wu", False) else "missing_station_or_date"
            obs = StationObservation(
                station_id,
                "weather_underground",
                now,
                station_local_date(timezone_name) or target_date,
                None,
                None,
                obs_status,
            )
        observation_id = record_station_observation(db, run_id, m["market_id"], obs)
        observed_high = obs.high_so_far_f
        target = parse_iso_date(target_date)
        local = parse_iso_date(obs.local_date)
        local_day_complete = bool(target and local and local > target)
        m["forecast_high_f"] = high
        m["observed_high_f"] = observed_high
        pending_rows: list[dict[str, Any]] = []
        ladder_rows: list[dict[str, Any]] = []
        for outcome, market_prob, token_id in zip(m["outcomes"], m["prices"], m.get("token_ids") or []):
            bucket = parse_bucket(str(outcome)) or title_bucket
            contract_spec = contract_spec_for_bucket(bucket, str(outcome))
            side_state = settlement_state_for_bucket(bucket, observed_high, str(outcome), target_date, obs.local_date)
            yes_state = settlement_state_for_bucket(bucket, observed_high, "Yes", target_date, obs.local_date)
            payout_mapping = payout_mapping_for_contract(contract_spec)
            baseline_high = observed_high if high is None else high
            bucket_state = settlement_states.legacy_bucket_state(yes_state)
            threshold_status = threshold_status_from_bucket_state(bucket_state)
            model_prob = None
            if bucket is not None and baseline_high is not None:
                base_prob = normal_prob(baseline_high, args.sigma, bucket.bounds)
                base_prob = apply_intraday_probability(base_prob, threshold_status)
                model_prob = base_prob
            if str(outcome).strip().lower() in ("no", "n"):
                model_prob = None if model_prob is None else 1.0 - model_prob
            quote = execution_quote(token_id, market_prob, args.paper_size, db, run_id, m["market_id"], str(outcome), now)
            entry_price = quote["entry_price"]
            edge = model_prob - entry_price if model_prob is not None and entry_price is not None else None
            source_conf = source_conf_base
            uncertainty = uncertainty_margin(source_conf, quote["spread"], threshold_status, family)
            req_edge = required_edge_v2(args.edge_threshold, quote["spread"], uncertainty, source_conf, bool(quote["depth_sufficient"]))
            ease = ease_score(family, source_conf, quote["spread"], quote["execution_source"])
            eligibility = market_eligibility(family, source_conf, station_id, target_date, bucket, timezone_name)
            signal_type, reason = classify_signal(
                edge,
                entry_price,
                source_conf,
                quote["spread"],
                threshold_status,
                args,
                uncertainty_margin=uncertainty,
                market_family_name=family,
                ease=ease,
                eligibility_class=eligibility,
                execution_source=quote["execution_source"],
                depth_sufficient=bool(quote["depth_sufficient"]),
                bucket_state=bucket_state,
            )
            signal_payload = {
                    "run_id": run_id,
                    "market_id": m["market_id"],
                    "title": m["title"],
                    "city": city,
                    "target_date": target_date,
                    "forecast_high_f": high,
                    "outcome": outcome,
                    "market_prob": market_prob,
                    "model_prob": model_prob,
                    "edge": edge,
                    "created_at": now,
                    "observed_high_f": observed_high,
                    "threshold_status": threshold_status,
                    "execution_source": quote["execution_source"],
                    "entry_price": entry_price,
                    "bid": quote["bid"],
                    "ask": quote["ask"],
                    "spread": quote["spread"],
                    "depth": quote["depth"],
                    "signal_type": signal_type,
                    "reason": reason,
                    "source_confidence": source_conf,
                    "source_host": m.get("source_host"),
                    "source_url": m.get("source_url"),
                    "station_id": station_id,
                    "market_family": family,
                    "ease_score": ease,
                    "uncertainty_margin": uncertainty,
                    "required_edge": req_edge,
                    "ladder_diagnostic": "",
                    "token_id": token_id,
                    "bucket_lo_f": bucket.lo if bucket else None,
                    "bucket_hi_f": bucket.hi if bucket else None,
                    "bucket_kind": bucket.kind if bucket else None,
                    "bucket_state": bucket_state,
                    "eligibility_class": eligibility,
                    "forecast_snapshot_id": forecast_snapshot_id,
                    "observation_id": observation_id,
                    "orderbook_snapshot_id": quote.get("orderbook_snapshot_id"),
                    "depth_near_ask": quote.get("depth_near_ask"),
                    "depth_sufficient": 1 if quote.get("depth_sufficient") else 0,
                    "event_key": event_key,
                    "candidate_key": candidate_key_for(event_key, m["market_id"], str(outcome), token_id, now),
                    "strategy_family": "unknown",
                    "contract_type": contract_spec.contract_type,
                    "settlement_state": side_state.state,
                    "early_state": side_state.early_state,
                    "final_state": side_state.final_state,
                    "payout_mapping_json": bounded_json(payout_mapping, RAW_JSON_LIMIT),
                    "quote_age_seconds": quote.get("quote_age_seconds"),
                    "stale_book_flag": 1 if quote.get("stale_book_flag") else 0,
                    "complement_arb_edge": None,
                    "complement_arb_status": None,
                    "ladder_violation_type": None,
                    "correction_trade": None,
                    "latent_final_high_mean_f": high,
                    "latent_final_high_sigma_f": args.sigma,
                    "local_day_complete": 1 if local_day_complete else 0,
            }
            pending_rows.append(
                {
                    "signal_payload": signal_payload,
                    "market": m,
                    "outcome": str(outcome),
                    "token_id": token_id,
                    "station_id": station_id,
                    "station_source": station_source,
                    "forecast_snapshot_id": forecast_snapshot_id,
                    "observation_id": observation_id,
                    "bucket": bucket,
                    "bucket_state": bucket_state,
                    "quote": quote,
                    "source_conf": source_conf,
                    "family": family,
                    "eligibility": eligibility,
                    "ease": ease,
                    "uncertainty": uncertainty,
                    "req_edge": req_edge,
                    "contract_spec": contract_spec,
                    "settlement_state": side_state,
                    "payout_mapping": payout_mapping,
                }
            )
            ladder_rows.append(
                {
                    "outcome": outcome,
                    "market_prob": market_prob,
                    "model_prob": model_prob,
                    "entry_price": entry_price,
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "depth": quote.get("depth"),
                    "quote_age_seconds": quote.get("quote_age_seconds"),
                    "bucket_state": bucket_state,
                    "bucket_lo_f": bucket.lo if bucket else None,
                    "bucket_hi_f": bucket.hi if bucket else None,
                }
            )
        max_quote_age = float(os.environ.get("STALE_BOOK_MAX_AGE_SECONDS", "60"))
        ladder_struct = ladder_monitor(ladder_rows, min_depth=float(getattr(args, "min_fill_shares", 1.0)), max_quote_age_seconds=max_quote_age)
        ladder_note = ladder_diagnostics(ladder_rows)
        ladder_violations = [v for v in ladder_struct.get("violations", []) if v != "ladder_ok"]
        ladder_violation_type = ",".join(ladder_violations) if ladder_violations else None
        correction_trade = ladder_struct.get("candidate_correction_trade")
        complement = detect_complement_arbitrage(
            ladder_rows,
            margin=float(os.environ.get("COMPLEMENT_ARB_MARGIN", "0.01")),
            min_depth=float(getattr(args, "min_fill_shares", 1.0)),
            max_quote_age_seconds=max_quote_age,
        )
        for row in pending_rows:
            row["signal_payload"]["ladder_diagnostic"] = ladder_note
            row["signal_payload"]["ladder_violation_type"] = ladder_violation_type
            row["signal_payload"]["correction_trade"] = bounded_json(correction_trade, RAW_JSON_LIMIT) if correction_trade else None
            row["signal_payload"]["complement_arb_status"] = complement.get("status")
            row["signal_payload"]["complement_arb_edge"] = complement.get("edge_after_margin")
            normalized_outcome = str(row["outcome"]).strip().lower()
            if complement.get("is_arb") and normalized_outcome in {"yes", "y", "no", "n"}:
                row["signal_payload"]["signal_type"] = "paper_buy_complement_arb"
                row["signal_payload"]["reason"] = (
                    f"complement YES+NO asks {float(complement['ask_sum']):.3f} below payout after margin; "
                    "separate from forecast edge"
                )
            elif correction_trade and correction_trade.get("outcome") == row["outcome"] and row["signal_payload"].get("signal_type") != "skip":
                row["signal_payload"]["signal_type"] = "paper_buy_ladder_inconsistency"
                row["signal_payload"]["reason"] = f"ladder correction candidate: {correction_trade.get('action')}"
            row["signal_payload"]["strategy_family"] = classify_strategy_family(
                row["signal_payload"].get("signal_type"),
                bucket_state=row["bucket_state"],
                settlement_state=row["signal_payload"].get("settlement_state"),
                ladder_status=ladder_struct.get("status"),
                complement_status=complement.get("status"),
                source_confidence=row["source_conf"],
                eligibility_class=row["eligibility"],
                observed_high_f=observed_high,
            )
            record_contract_payout(
                db,
                event_key=event_key,
                market_id=m["market_id"],
                outcome=row["outcome"],
                token_id=row["token_id"],
                bucket=row["bucket"],
                spec=row["contract_spec"],
                payout_mapping=row["payout_mapping"],
                created_at=now,
            )
            columns = list(row["signal_payload"].keys())
            cur = db.execute(
                f"insert into signals({','.join(columns)}) values ({','.join(['?'] * len(columns))})",
                tuple(row["signal_payload"][column] for column in columns),
            )
            signal_id = int(cur.lastrowid)
            record_training_row(
                db,
                run_id,
                signal_id,
                now,
                row["market"],
                row["outcome"],
                row["token_id"],
                row["signal_payload"]["city"],
                row["signal_payload"]["target_date"],
                row["station_id"],
                row["station_source"],
                row["forecast_snapshot_id"],
                row["observation_id"],
                row["quote"].get("orderbook_snapshot_id"),
                row["bucket"],
                row["bucket_state"],
                row["quote"],
                row["signal_payload"]["market_prob"],
                row["signal_payload"]["model_prob"],
                row["signal_payload"]["edge"],
                row["signal_payload"]["signal_type"],
                row["signal_payload"]["reason"],
                row["source_conf"],
                row["family"],
                row["eligibility"],
                row["ease"],
                row["uncertainty"],
                row["req_edge"],
                event_key=row["signal_payload"].get("event_key"),
                candidate_key=row["signal_payload"].get("candidate_key"),
                strategy_family=row["signal_payload"].get("strategy_family") or "unknown",
                contract_type=row["signal_payload"].get("contract_type"),
                settlement_state=row["signal_payload"].get("settlement_state"),
                early_state=row["signal_payload"].get("early_state"),
                final_state=row["signal_payload"].get("final_state"),
                payout_mapping=row["payout_mapping"],
                complement_arb_edge=row["signal_payload"].get("complement_arb_edge"),
                complement_arb_status=row["signal_payload"].get("complement_arb_status"),
                ladder_violation_type=row["signal_payload"].get("ladder_violation_type"),
                correction_trade=row["signal_payload"].get("correction_trade"),
                latent_final_high_mean_f=high,
                latent_final_high_sigma_f=args.sigma,
                local_day_complete=local_day_complete,
            )
            seed_touch_watchlist_candidate(
                db,
                event_key=row["signal_payload"].get("event_key"),
                market=row["market"],
                outcome=row["outcome"],
                token_id=row["token_id"],
                city=row["signal_payload"].get("city"),
                station_id=row["station_id"],
                target_date=row["signal_payload"].get("target_date"),
                strategy_family=row["signal_payload"].get("strategy_family") or "unknown",
                contract_spec=row["contract_spec"],
                observed_high_f=observed_high,
                quote=row["quote"],
                source_confidence=row["source_conf"],
                timezone_name=timezone_name,
                now_iso=now,
            )
            simulate_paper_order(
                db,
                args,
                run_id,
                signal_id,
                now,
                row["market"],
                row["outcome"],
                row["token_id"],
                row["signal_payload"]["city"],
                row["signal_payload"]["target_date"],
                row["signal_payload"]["signal_type"],
                row["quote"],
                event_key=row["signal_payload"].get("event_key"),
                candidate_key=row["signal_payload"].get("candidate_key"),
                strategy_family=row["signal_payload"].get("strategy_family") or "unknown",
            )
            signal_count += 1
        upsert_event(
            db,
            event_key=event_key,
            city=city,
            target_date=target_date,
            source=m.get("source_host"),
            station_id=station_id,
            rule_hash=rule_hash,
            seen_at=now,
            latent_mean=high,
            latent_sigma=args.sigma,
            observed_high=observed_high,
            local_day_complete=local_day_complete,
            contract_count=len(pending_rows),
        )
    db.execute("update runs set markets_seen=?, signals_seen=? where id=?", (len(markets), signal_count, run_id))
    reconcile_zombie_positions(db, now)
    settle_paper_positions_from_latest_prices(db, now)
    settle_paper_positions_from_labels(db, now)
    record_account_snapshot(db, run_id, now)
    db.commit()
    render_report(db, args.report)
    metrics = portfolio_metrics(db)
    print(
        f"run_id={run_id} markets={len(markets)} signals={signal_count} "
        f"paper_equity={metrics['equity']:.2f} report={args.report}"
    )


def render_report(db: sqlite3.Connection, path: str, limit: int = 200) -> None:
    rows = db.execute(
        """
        select created_at,title,city,target_date,forecast_high_f,observed_high_f,outcome,
               signal_type,source_confidence,coalesce(source_host,''),coalesce(source_url,''),coalesce(station_id,''),
               execution_source,entry_price,bid,ask,spread,depth,model_prob,edge,reason,
               coalesce(market_family,''),ease_score,uncertainty_margin,required_edge,coalesce(ladder_diagnostic,'')
        from signals order by created_at desc, abs(coalesce(edge,0)) desc limit ?
        """,
        (limit,),
    ).fetchall()
    body = []
    for r in rows:
        forecast = "" if r[4] is None else f"{r[4]:.1f}"
        observed = "" if r[5] is None else f"{r[5]:.1f}"
        entry = "" if r[13] is None else f"{r[13]:.1%}"
        spread = "" if r[16] is None else f"{r[16]:.1%}"
        model = "" if r[18] is None else f"{r[18]:.1%}"
        edge = "" if r[19] is None else f"{r[19]:+.1%}"
        family = str(r[21] or "")
        ease = "" if r[22] is None else f"{r[22]:.1f}"
        uncertainty = "" if r[23] is None else f"{r[23]:.1%}"
        required = "" if r[24] is None else f"{r[24]:.1%}"
        ladder = str(r[25] or "")
        source_url = str(r[10] or "")
        source_label = source_url or str(r[9] or "")
        source_cell = (
            f'<a href="{html.escape(source_url)}">{html.escape(str(r[9] or source_url))}</a>'
            if source_url.startswith("http")
            else html.escape(source_label)
        )
        station = html.escape(str(r[11] or ""))
        body.append(
            "<tr>"
            + f"<td>{html.escape(str(r[0]))}</td>"
            + f"<td>{html.escape(str(r[1]))}</td>"
            + f"<td>{html.escape('' if r[2] is None else str(r[2]))}</td>"
            + f"<td>{html.escape('' if r[3] is None else str(r[3]))}</td>"
            + f"<td>{html.escape(forecast)}</td>"
            + f"<td>{html.escape(observed)}</td>"
            + f"<td>{html.escape(str(r[6]))}</td>"
            + f"<td>{html.escape(str(r[7] or 'watch'))}</td>"
            + f"<td>{html.escape(str(r[8] or 'low'))}</td>"
            + f"<td>{source_cell}<br>{station}</td>"
            + f"<td>{html.escape(str(r[12] or ''))}</td>"
            + f"<td>{html.escape(entry)}</td>"
            + f"<td>{html.escape(spread)}</td>"
            + f"<td>{html.escape(model)}</td>"
            + f"<td>{html.escape(edge)}</td>"
            + f"<td>{html.escape(family)}</td>"
            + f"<td>{html.escape(ease)}</td>"
            + f"<td>{html.escape(uncertainty)}</td>"
            + f"<td>{html.escape(required)}</td>"
            + f"<td>{html.escape(ladder)}</td>"
            + f"<td>{html.escape(str(r[20] or ''))}</td>"
            + "</tr>"
        )
    doc = f"""<!doctype html>
<meta charset="utf-8">
<title>Polymarket Weather Paper Scan</title>
<style>
body{{font:14px system-ui,sans-serif;margin:32px;color:#1f2933}}table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ddd;padding:7px 9px;text-align:left;vertical-align:top}}th{{background:#f3f5f7}}
</style>
<h1>Polymarket Weather Paper Scan</h1>
<p>Paper research only. No trading integration or order placement.</p>
<table><thead><tr><th>Seen</th><th>Market</th><th>City</th><th>Date</th><th>Forecast F</th><th>Observed F</th><th>Outcome</th><th>Signal</th><th>Source Conf</th><th>Settlement Source / Station</th><th>Execution</th><th>Entry</th><th>Spread</th><th>Model</th><th>Edge</th><th>Family</th><th>Ease</th><th>Uncertainty</th><th>Req Edge</th><th>Ladder</th><th>Reason</th></tr></thead>
<tbody>{''.join(body) or '<tr><td colspan="21">No scored signals yet.</td></tr>'}</tbody></table>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def summary(args: argparse.Namespace) -> None:
    db = init_db(args.db)
    render_report(db, args.report)
    runs = db.execute("select count(*), coalesce(sum(markets_seen),0), coalesce(sum(signals_seen),0) from runs").fetchone()
    top = db.execute(
        "select title,outcome,coalesce(entry_price,market_prob),model_prob,edge,created_at,coalesce(signal_type,'watch') from signals "
        "where edge is not null order by abs(edge) desc limit ?",
        (args.limit,),
    ).fetchall()
    print(f"runs={runs[0]} markets_seen={runs[1]} signals={runs[2]} report={args.report}")
    for title, outcome, entry, pp, edge, seen, signal_type in top:
        print(f"{signal_type} {edge:+.1%} model={pp:.1%} entry={entry:.1%} {outcome} | {title[:90]} ({seen})")


def parse_date_prefix(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None


def token_resolution_for(latest_prob: float) -> str:
    if latest_prob >= 0.98:
        return "resolved_win"
    if latest_prob <= 0.02:
        return "resolved_loss"
    return "unresolved"


def reverse_resolution(status: str) -> str:
    if status == "resolved_win":
        return "resolved_loss"
    if status == "resolved_loss":
        return "resolved_win"
    return status


def mark_pnl(entry_prob: float, latest_prob: float, edge: float) -> tuple[float, str]:
    token_status = token_resolution_for(latest_prob)
    if token_status == "resolved_win":
        mark = 1.0
    elif token_status == "resolved_loss":
        mark = 0.0
    else:
        mark = latest_prob
    if edge >= 0.0:
        return mark - entry_prob, token_status
        return entry_prob - mark, reverse_resolution(token_status)


def finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return value if math.isfinite(value) else None


def outcome_bucket_for_label(title: str | None, outcome: str | None) -> tuple[BucketSpec | None, bool]:
    normalized = str(outcome or "").strip().lower()
    is_no_token = normalized in {"no", "n"}
    if normalized in {"yes", "y", "no", "n"}:
        return parse_bucket(str(title or "")), is_no_token
    return parse_bucket(str(outcome or "")) or parse_bucket(str(title or "")), False


def determine_label_value(final_high_f: float, title: str | None, outcome: str | None) -> tuple[float | None, BucketSpec | None, str]:
    bucket, is_no_token = outcome_bucket_for_label(title, outcome)
    if bucket is None:
        return None, None, "bucket_unparseable"
    in_bucket = high_within_bucket(final_high_f, bucket)
    value = 0.0 if (is_no_token and in_bucket) else 1.0 if (is_no_token or in_bucket) else 0.0
    return value, bucket, "ok"


def ncei_station_identifiers(station_id: str | None) -> list[str]:
    if not station_id:
        return []
    raw = str(station_id).strip()
    if not raw:
        return []
    upper = raw.upper()
    candidates = [NCEI_STATION_ID_CROSSWALK.get(upper), upper]
    if upper.startswith("GHCND:"):
        candidates.append(upper.removeprefix("GHCND:"))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def label_station_confidence(candidate: dict[str, Any], provider: str, outcome_status: str) -> str:
    if not candidate.get("station_id"):
        return "low"
    if provider == "noaa_ncei" and outcome_status == FINAL_LABEL_STATUS:
        return "high" if candidate.get("source_confidence") == "high" else "medium"
    if outcome_status == PROVISIONAL_LABEL_STATUS:
        return "medium"
    return str(candidate.get("source_confidence") or "low")


def build_label_attempt(
    candidate: dict[str, Any],
    *,
    attempted_at: str,
    source_record: weather_sources.SourceRecord | None,
    source_provider: str,
    source_family: str,
    source_status: str,
    outcome_status: str,
    reason: str,
    final_high_f: float | None = None,
    label_value: float | None = None,
    bucket: BucketSpec | None = None,
    station_id: str | None = None,
) -> dict[str, Any]:
    provider = source_record.provider if source_record else source_provider
    family = source_record.family if source_record else source_family
    target_metric = normalize_target_metric(candidate.get("target_metric"))
    provenance = source_record.to_dict() if source_record else {
        "provider": provider,
        "family": family,
        "read_only": True,
        "requires_credentials": False,
        "fetched_at": attempted_at,
    }
    return {
        "attempted_at": attempted_at,
        "training_row_id": candidate.get("training_row_id"),
        "position_id": candidate.get("position_id"),
        "market_id": candidate.get("market_id"),
        "title": candidate.get("title"),
        "outcome": candidate.get("outcome"),
        "target_date": candidate.get("target_date"),
        "station_id": station_id or candidate.get("station_id"),
        "source_provider": provider,
        "source_family": family,
        "source_url": source_record.source_url if source_record else None,
        "source_status": source_record.status if source_record else source_status,
        "station_confidence": label_station_confidence(candidate, provider, outcome_status),
        "target_metric": target_metric,
        "final_observed_f": final_high_f,
        "final_high_f": final_high_f,
        "threshold_low_f": finite_or_none(bucket.lo if bucket else None),
        "threshold_high_f": finite_or_none(bucket.hi if bucket else None),
        "label_value": label_value,
        "outcome_status": outcome_status,
        "reason": reason,
        "provenance_json": bounded_json(provenance, RAW_JSON_LIMIT),
        "raw_excerpt": compact_text((source_record.data if source_record else provenance), RAW_EXCERPT_LIMIT),
        "_source_record": source_record,
    }


def enabled_label_source_specs(args: argparse.Namespace) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    if getattr(args, "enable_ncei", False):
        specs.append(("noaa_ncei", "ncei_daily_labels"))
    if getattr(args, "enable_nws", False):
        specs.append(("nws", "nws"))
    if getattr(args, "enable_iem", False):
        specs.append(("iem_metar", "iem_metar"))
    if getattr(args, "enable_metar_direct", False):
        specs.append(("aviationweather_metar", "metar_direct"))
    return specs


def skipped_label_attempts_for_enabled_sources(
    candidate: dict[str, Any],
    args: argparse.Namespace,
    *,
    attempted_at: str,
    reason: str,
    bucket: BucketSpec | None = None,
) -> list[dict[str, Any]]:
    specs = enabled_label_source_specs(args) or [("labeler", "labeler")]
    return [
        build_label_attempt(
            candidate,
            attempted_at=attempted_at,
            source_record=None,
            source_provider=provider,
            source_family=family,
            source_status=reason,
            outcome_status=SKIPPED_LABEL_STATUS,
            reason=reason,
            bucket=bucket,
        )
        for provider, family in specs
    ]


def record_source_snapshot_from_label_attempt(
    db: sqlite3.Connection,
    *,
    candidate: dict[str, Any],
    attempt: dict[str, Any],
    run_id: int | None = None,
) -> int:
    """Persist one public-source observation snapshot for a label attempt.

    This is intentionally write-only evidence capture: success, error, pending,
    and skipped adapter outcomes all become snapshot rows so downstream source
    delta logic can distinguish missing data from silent adapter omission.
    """
    record = attempt.get("_source_record")
    provider = str(attempt.get("source_provider") or getattr(record, "provider", None) or "unknown")
    family = str(attempt.get("source_family") or getattr(record, "family", None) or "labeler")
    status = str(attempt.get("source_status") or getattr(record, "status", None) or "skipped")
    outcome_status = str(attempt.get("outcome_status") or "")
    data = getattr(record, "data", None) if record is not None else None
    data = data if isinstance(data, dict) else {}
    return record_source_observation_snapshot(
        db,
        run_id=run_id,
        market_id=attempt.get("market_id") or candidate.get("market_id"),
        event_key=candidate.get("event_key") or attempt.get("market_id") or candidate.get("market_id"),
        source_record=record,
        provider=provider,
        family=family,
        status=status,
        station_id=attempt.get("station_id") or candidate.get("station_id"),
        observed_at=data.get("observed_at") or data.get("obs_time_utc") or attempt.get("attempted_at"),
        fetched_at=getattr(record, "fetched_at", None) if record is not None else attempt.get("attempted_at"),
        local_date=data.get("local_date") or attempt.get("target_date") or candidate.get("target_date"),
        target_metric=attempt.get("target_metric") or candidate.get("target_metric"),
        observed_f=attempt.get("final_observed_f"),
        observed_high_f=attempt.get("final_high_f") if outcome_status in {FINAL_LABEL_STATUS, PROVISIONAL_LABEL_STATUS} else data.get("daily_high_f", data.get("high_so_far_f")),
        source_url=attempt.get("source_url"),
        raw_excerpt=attempt.get("raw_excerpt"),
        error=attempt.get("reason") if outcome_status in {ERROR_LABEL_STATUS, SKIPPED_LABEL_STATUS} or status not in {"ok", "success"} else None,
        now=str(attempt.get("attempted_at") or utc_now_iso()),
    )


def insert_label_attempt(db: sqlite3.Connection, attempt: dict[str, Any]) -> int:
    columns = [
        "attempted_at",
        "training_row_id",
        "position_id",
        "market_id",
        "title",
        "outcome",
        "target_date",
        "station_id",
        "source_provider",
        "source_family",
        "source_url",
        "source_status",
        "station_confidence",
        "target_metric",
        "final_observed_f",
        "final_high_f",
        "threshold_low_f",
        "threshold_high_f",
        "label_value",
        "outcome_status",
        "reason",
        "provenance_json",
        "raw_excerpt",
    ]
    cur = db.execute(
        f"insert into label_attempts({','.join(columns)}) values ({','.join(['?'] * len(columns))})",
        tuple(attempt.get(column) for column in columns),
    )
    return int(cur.lastrowid)


def matching_unlabeled_training_row_ids(db: sqlite3.Connection, candidate: dict[str, Any]) -> list[int]:
    rows = db.execute(
        """
        select id
        from training_rows
        where coalesce(market_id,'')=coalesce(?, '')
          and coalesce(outcome,'')=coalesce(?, '')
          and coalesce(target_date,'')=coalesce(?, '')
          and coalesce(station_id,'')=coalesce(?, '')
          and coalesce(target_metric, ?) = coalesce(?, coalesce(target_metric, ?))
          and coalesce(label_status,'') <> ?
        """,
        (
            candidate.get("market_id"),
            candidate.get("outcome"),
            candidate.get("target_date"),
            candidate.get("station_id"),
            TARGET_METRIC_DAILY_HIGH,
            candidate.get("target_metric"),
            TARGET_METRIC_DAILY_HIGH,
            FINAL_LABEL_STATUS,
        ),
    ).fetchall()
    return [int(row[0]) for row in rows]


def apply_final_label_to_training_rows(
    db: sqlite3.Connection,
    candidate: dict[str, Any],
    attempt: dict[str, Any],
    attempt_id: int | None,
) -> int:
    label_value = attempt.get("label_value")
    if label_value not in (0.0, 1.0, 0, 1):
        return 0
    row_ids = matching_unlabeled_training_row_ids(db, candidate)
    if not row_ids:
        return 0
    placeholders = ",".join("?" for _ in row_ids)
    label_source = str(attempt.get("source_provider") or "")
    if attempt_id is not None:
        label_source = f"{label_source}:attempt:{attempt_id}"
    db.execute(
        f"""
        update training_rows
        set label_status=?, label_value=?, label_source=?, labeled_at=?
        where id in ({placeholders})
        """,
        (
            FINAL_LABEL_STATUS,
            float(label_value),
            label_source,
            attempt.get("attempted_at"),
            *row_ids,
        ),
    )
    rows = db.execute(
        f"select candidate_key from training_rows where id in ({placeholders})",
        row_ids,
    ).fetchall()
    for row in rows:
        update_lifecycle_links(
            db,
            candidate_key=row[0],
            label_attempt_id=attempt_id,
            label_status=FINAL_LABEL_STATUS,
            label_value=float(label_value),
        )
    record_calibration_rows_for_training_ids(
        db,
        row_ids,
        label_source=label_source,
        now=str(attempt.get("attempted_at") or utc_now_iso()),
        label_attempt_id=attempt_id,
    )
    refresh_station_residuals(db, now=str(attempt.get("attempted_at") or utc_now_iso()))
    return len(row_ids)


def label_candidate_groups(
    db: sqlite3.Connection,
    cutoff_date: str,
    retry_before: str,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rows = db.execute(
        """
        with candidate_groups as (
          select
            min(id) as training_row_id,
            coalesce(market_id,'') as market_id_key,
            coalesce(outcome,'') as outcome_key,
            target_date as target_date_key,
            coalesce(station_id,'') as station_id_key,
            coalesce(target_metric, ?) as target_metric_key
          from training_rows indexed by idx_training_labeler_candidates
          where target_date is not null
            and target_date <> ''
            and target_date <= ?
            and (label_status is null or label_status <> ?)
            and (market_family is null or market_family='' or market_family='daily_temperature')
          group by
            coalesce(market_id,''),
            coalesce(outcome,''),
            target_date,
            coalesce(station_id,''),
            coalesce(target_metric, ?)
        ),
        latest_attempts as (
          select
            coalesce(market_id,'') as market_id_key,
            coalesce(outcome,'') as outcome_key,
            target_date as target_date_key,
            coalesce(station_id,'') as station_id_key,
            coalesce(target_metric, ?) as target_metric_key,
            min(attempted_at) as first_attempt,
            max(attempted_at) as latest_attempt
          from label_attempts indexed by idx_label_attempts_candidate_metric
          group by
            coalesce(market_id,''),
            coalesce(outcome,''),
            target_date,
            coalesce(station_id,''),
            coalesce(target_metric, ?)
        )
        select
          cg.training_row_id,
          tr.market_id,
          tr.title,
          tr.outcome,
          tr.city,
          tr.target_date,
          tr.station_id,
          tr.station_source,
          tr.source_confidence,
          tr.market_family,
          tr.event_key,
          la.first_attempt,
          la.latest_attempt,
          cg.target_metric_key
        from candidate_groups cg
        join training_rows tr on tr.id=cg.training_row_id
        left join latest_attempts la
          on la.market_id_key=cg.market_id_key
         and la.outcome_key=cg.outcome_key
         and la.target_date_key=cg.target_date_key
         and la.station_id_key=cg.station_id_key
         and la.target_metric_key=cg.target_metric_key
        where la.latest_attempt is null or la.latest_attempt < ?
        order by case when cg.station_id_key <> '' then 0 else 1 end, tr.target_date asc, cg.training_row_id asc
        limit ?
        """,
        (
            TARGET_METRIC_DAILY_HIGH,
            cutoff_date,
            FINAL_LABEL_STATUS,
            TARGET_METRIC_DAILY_HIGH,
            TARGET_METRIC_DAILY_HIGH,
            TARGET_METRIC_DAILY_HIGH,
            retry_before,
            limit,
        ),
    ).fetchall()
    return [
        {
            "training_row_id": int(row[0]),
            "market_id": row[1],
            "title": row[2],
            "outcome": row[3],
            "city": row[4],
            "target_date": row[5],
            "station_id": row[6],
            "station_source": row[7],
            "source_confidence": row[8],
            "market_family": row[9],
            "event_key": row[10],
            "first_attempt": row[11],
            "latest_attempt": row[12],
            "target_metric": row[13],
            "position_id": None,
        }
        for row in rows
    ]


def attempt_delayed_label(candidate: dict[str, Any], args: argparse.Namespace, attempted_at: str) -> list[dict[str, Any]]:
    label_value, bucket, bucket_reason = determine_label_value(0.0, candidate.get("title"), candidate.get("outcome"))
    if bucket is None:
        return skipped_label_attempts_for_enabled_sources(candidate, args, attempted_at=attempted_at, reason="parse_error")
    if not candidate.get("station_id"):
        return skipped_label_attempts_for_enabled_sources(candidate, args, attempted_at=attempted_at, reason="no_station_id", bucket=bucket)

    attempts: list[dict[str, Any]] = []
    timeout = float(getattr(args, "http_timeout", weather_sources.DEFAULT_TIMEOUT_SECONDS))
    ttl = int(getattr(args, "cache_ttl", weather_sources.DEFAULT_CACHE_TTL_SECONDS))
    if getattr(args, "enable_ncei", False):
        adapter = weather_sources.NOAADelayedLabelAdapter(enabled=True, timeout_seconds=timeout, cache_ttl_seconds=ttl)
        for station_identifier in ncei_station_identifiers(str(candidate.get("station_id"))):
            record = adapter.fetch_daily_summary(station_identifier, str(candidate.get("target_date")))
            high = record.data.get("daily_high_f") if isinstance(record.data, dict) else None
            if high is not None:
                final_value, final_bucket, reason = determine_label_value(float(high), candidate.get("title"), candidate.get("outcome"))
                if final_value is not None and final_bucket is not None:
                    attempts.append(
                        build_label_attempt(
                            candidate,
                            attempted_at=attempted_at,
                            source_record=record,
                            source_provider=record.provider,
                            source_family=record.family,
                            source_status=record.status,
                            outcome_status=FINAL_LABEL_STATUS,
                            reason=reason,
                            final_high_f=float(high),
                            label_value=final_value,
                            bucket=final_bucket,
                            station_id=station_identifier,
                        )
                    )
                    break
            attempts.append(
                build_label_attempt(
                    candidate,
                    attempted_at=attempted_at,
                    source_record=record,
                    source_provider=record.provider,
                    source_family=record.family,
                    source_status=record.status,
                    outcome_status=PENDING_LABEL_STATUS if record.status != "error" else ERROR_LABEL_STATUS,
                    reason=record.error or record.status or "ncei_daily_high_unavailable",
                    bucket=bucket,
                    station_id=station_identifier,
                )
            )
            time.sleep(float(getattr(args, "pause", 0.0) or 0.0))

    if getattr(args, "enable_nws", False):
        adapter = weather_sources.NWSAdapter(enabled=True, timeout_seconds=timeout, cache_ttl_seconds=ttl)
        record = adapter.fetch_daily_high(str(candidate.get("station_id")), str(candidate.get("target_date")))
        high = record.data.get("daily_high_f") if isinstance(record.data, dict) else None
        if high is not None:
            provisional_value, provisional_bucket, reason = determine_label_value(float(high), candidate.get("title"), candidate.get("outcome"))
            if provisional_value is not None and provisional_bucket is not None:
                attempts.append(
                    build_label_attempt(
                        candidate,
                        attempted_at=attempted_at,
                        source_record=record,
                        source_provider=record.provider,
                        source_family=record.family,
                        source_status=record.status,
                        outcome_status=PROVISIONAL_LABEL_STATUS,
                        reason=f"supporting_nws_observations_{reason}",
                        final_high_f=float(high),
                        label_value=provisional_value,
                        bucket=provisional_bucket,
                        )
                )
        else:
            attempts.append(
                build_label_attempt(
                    candidate,
                    attempted_at=attempted_at,
                    source_record=record,
                    source_provider=record.provider,
                    source_family=record.family,
                    source_status=record.status,
                    outcome_status=PENDING_LABEL_STATUS if record.status != "error" else ERROR_LABEL_STATUS,
                    reason=record.error or record.status or "nws_daily_high_unavailable",
                    bucket=bucket,
                )
            )

    if getattr(args, "enable_iem", False):
        adapter = weather_sources.IEMMetarAdapter(enabled=True, timeout_seconds=timeout, cache_ttl_seconds=ttl)
        record = adapter.fetch_daily_high(str(candidate.get("station_id")), str(candidate.get("target_date")))
        high = record.data.get("daily_high_f") if isinstance(record.data, dict) else None
        if high is not None:
            provisional_value, provisional_bucket, reason = determine_label_value(float(high), candidate.get("title"), candidate.get("outcome"))
            if provisional_value is not None and provisional_bucket is not None:
                attempts.append(
                    build_label_attempt(
                        candidate,
                        attempted_at=attempted_at,
                        source_record=record,
                        source_provider=record.provider,
                        source_family=record.family,
                        source_status=record.status,
                        outcome_status=PROVISIONAL_LABEL_STATUS,
                        reason=f"supporting_iem_asos_{reason}",
                        final_high_f=float(high),
                        label_value=provisional_value,
                        bucket=provisional_bucket,
                        )
                )
        else:
            attempts.append(
                build_label_attempt(
                    candidate,
                    attempted_at=attempted_at,
                    source_record=record,
                    source_provider=record.provider,
                    source_family=record.family,
                    source_status=record.status,
                    outcome_status=PENDING_LABEL_STATUS if record.status != "error" else ERROR_LABEL_STATUS,
                    reason=record.error or record.status or "iem_daily_high_unavailable",
                    bucket=bucket,
                )
            )

    if getattr(args, "enable_metar_direct", False):
        adapter = weather_sources.AviationWeatherMetarAdapter(enabled=True, timeout_seconds=timeout, cache_ttl_seconds=ttl)
        record = adapter.fetch_current(str(candidate.get("station_id")))
        attempts.append(
            build_label_attempt(
                candidate,
                attempted_at=attempted_at,
                source_record=record,
                source_provider=record.provider,
                source_family=record.family,
                source_status=record.status,
                outcome_status=ERROR_LABEL_STATUS if record.status == "error" else PENDING_LABEL_STATUS,
                reason=record.error or "current_metar_not_final_daily_label",
                bucket=bucket,
            )
        )

    if getattr(args, "enable_meteostat", False):
        adapter = weather_sources.MeteostatAdapter(enabled=True, timeout_seconds=timeout, cache_ttl_seconds=ttl)
        record = adapter.historical_stub(str(candidate.get("station_id")), str(candidate.get("target_date")), str(candidate.get("target_date")))
        skipped_statuses = {"dependency_absent", "adapter_stub", "disabled"}
        attempts.append(
            build_label_attempt(
                candidate,
                attempted_at=attempted_at,
                source_record=record,
                source_provider=record.provider,
                source_family=record.family,
                source_status=record.status,
                outcome_status=SKIPPED_LABEL_STATUS if record.status in skipped_statuses else PENDING_LABEL_STATUS,
                reason=record.error or record.status or "meteostat_unavailable",
                bucket=bucket,
            )
        )

    if attempts:
        return attempts
    return [
        build_label_attempt(
            candidate,
            attempted_at=attempted_at,
            source_record=None,
            source_provider="labeler",
            source_family="labeler",
            source_status=SKIPPED_LABEL_STATUS,
            outcome_status=SKIPPED_LABEL_STATUS,
            reason="all_label_sources_disabled",
            bucket=bucket,
        )
    ]


def final_label_for_position(db: sqlite3.Connection, market_id: str, outcome: str) -> tuple[int, float, str | None, str | None, int | None, float | None] | None:
    row = db.execute(
        """
        select tr.id, tr.label_value, tr.label_source, tr.labeled_at, la.id, la.final_high_f
        from training_rows tr
        left join label_attempts la
          on la.training_row_id=tr.id
         and la.outcome_status=?
         and la.label_value=tr.label_value
        where tr.market_id=?
          and tr.outcome=?
          and tr.label_status=?
          and tr.label_value in (0, 1)
        order by tr.labeled_at desc, tr.id desc, la.id desc
        limit 1
        """,
        (FINAL_LABEL_STATUS, market_id, outcome, FINAL_LABEL_STATUS),
    ).fetchone()
    if not row:
        return None
    return int(row[0]), float(row[1]), row[2], row[3], int(row[4]) if row[4] is not None else None, float(row[5]) if row[5] is not None else None


def settle_paper_positions_from_labels(db: sqlite3.Connection, now: str) -> int:
    account = account_row(db)
    account_id = int(account[0])
    positions = db.execute(
        """
        select id, market_id, outcome, shares, cost_basis, event_key, strategy_family
        from paper_positions
        where account_id=? and status='open'
        """,
        (account_id,),
    ).fetchall()
    settled = 0
    for pos_id, market_id, outcome, shares, cost_basis, position_event_key, position_strategy_family in positions:
        label = final_label_for_position(db, str(market_id), str(outcome))
        if not label:
            continue
        source_training_row_id, label_value, label_source, _labeled_at, label_attempt_id, final_high_f = label
        meta = db.execute(
            "select event_key, strategy_family, candidate_key from training_rows where id=?",
            (source_training_row_id,),
        ).fetchone()
        event_key = (meta[0] if meta else None) or position_event_key
        strategy_family = (meta[1] if meta else None) or position_strategy_family
        candidate_key = meta[2] if meta else None
        existing = db.execute(
            "select id, realized_pnl from paper_settlements where position_id=?", (pos_id,)
        ).fetchone()
        if existing:
            # Settlement already recorded — sync position status if it drifted to 'open'
            db.execute(
                "update paper_positions set status='settled', realized_pnl=?, updated_at=? where id=? and status='open'",
                (existing[1], now, pos_id),
            )
            continue
        payout = float(shares) * float(label_value)
        realized = payout - float(cost_basis)
        db.execute("update paper_positions set latest_mark=?, updated_at=? where id=?", (label_value, now, pos_id))
        db.execute(
            """
            insert into paper_settlements(
              position_id, settled_at, outcome_status, payout, realized_pnl,
              source_signal_id, source_training_row_id, label_attempt_id,
              label_source, final_high_f, event_key, strategy_family
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pos_id,
                now,
                "resolved_win" if label_value >= 1.0 else "resolved_loss",
                payout,
                realized,
                None,
                source_training_row_id,
                label_attempt_id,
                label_source,
                final_high_f,
                event_key,
                strategy_family,
            ),
        )
        settlement_id = int(db.execute("select last_insert_rowid()").fetchone()[0])
        db.execute(
            "update paper_positions set status='settled', realized_pnl=?, updated_at=? where id=?",
            (realized, now, pos_id),
        )
        db.execute(
            "update paper_accounts set cash=cash+?, realized_pnl=realized_pnl+?, updated_at=? where id=?",
            (payout, realized, now, account_id),
        )
        update_lifecycle_links(
            db,
            candidate_key=candidate_key,
            paper_settlement_id=settlement_id,
            position_id=pos_id,
            label_attempt_id=label_attempt_id,
            label_status=FINAL_LABEL_STATUS,
            label_value=label_value,
            event_key=event_key,
            strategy_family=strategy_family,
        )
        settled += 1
    return settled


def label(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    if bool(getattr(args, "dry_run", False)) and int(getattr(args, "limit", 0) or 0) <= 0:
        now = utc_now_iso()
        today = dt.datetime.fromisoformat(now).date()
        cutoff = today - dt.timedelta(days=max(0, int(getattr(args, "min_age_days", 2))))
        print("labeler=paper_only")
        print("mode=paper_only live_trading=false wallet=false order_placement=false")
        print(
            f"dry_run=true cutoff_date={cutoff.isoformat()} candidates=0 attempts_written=0 "
            "final_label_groups=0 final_training_rows=0 provisional_attempts=0 "
            "pending_attempts=0 skipped_attempts=0 error_attempts=0 settled_positions=0"
        )
        return
    db = init_db(args.db)
    now = utc_now_iso()
    today = dt.datetime.fromisoformat(now).date()
    cutoff = today - dt.timedelta(days=max(0, int(getattr(args, "min_age_days", 2))))
    retry_before_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(0.0, float(getattr(args, "retry_after_hours", 12.0))))
    retry_before = retry_before_dt.replace(microsecond=0).isoformat()
    candidates = label_candidate_groups(db, cutoff.isoformat(), retry_before, int(args.limit))
    attempts_written = 0
    final_groups = 0
    final_rows = 0
    provisional = 0
    pending = 0
    skipped = 0
    errors = 0

    for candidate in candidates:
        attempts = attempt_delayed_label(candidate, args, now)
        for attempt in attempts:
            status = str(attempt.get("outcome_status") or "")
            if status == FINAL_LABEL_STATUS:
                final_groups += 1
            elif status == PROVISIONAL_LABEL_STATUS:
                provisional += 1
            elif status == PENDING_LABEL_STATUS:
                pending += 1
            elif status == SKIPPED_LABEL_STATUS:
                skipped += 1
            elif status == ERROR_LABEL_STATUS:
                errors += 1
            if args.dry_run:
                continue
            attempt_id = insert_label_attempt(db, attempt)
            record_source_snapshot_from_label_attempt(db, candidate=candidate, attempt=attempt)
            attempts_written += 1
            if status == FINAL_LABEL_STATUS:
                final_rows += apply_final_label_to_training_rows(db, candidate, attempt, attempt_id)

    settled_positions = 0
    if not args.dry_run and not args.no_settle:
        settled_positions = settle_paper_positions_from_labels(db, now)
        if final_rows or settled_positions or attempts_written:
            record_account_snapshot(db, None, now)
    if not args.dry_run:
        db.commit()

    print("labeler=paper_only")
    print("mode=paper_only live_trading=false wallet=false order_placement=false")
    print(
        f"dry_run={str(bool(args.dry_run)).lower()} cutoff_date={cutoff.isoformat()} "
        f"candidates={len(candidates)} attempts_written={attempts_written} "
        f"final_label_groups={final_groups} final_training_rows={final_rows} "
        f"provisional_attempts={provisional} pending_attempts={pending} "
        f"skipped_attempts={skipped} error_attempts={errors} settled_positions={settled_positions}"
    )


def reconcile(args: argparse.Namespace) -> None:
    """Reconcile zombie positions: sync any position stuck in 'open' that already has a settlement record."""
    db = init_db(args.db)
    now = utc_now_iso()
    fixed = reconcile_zombie_positions(db, now)
    db.commit()
    print(f"reconciled {fixed} zombie position(s) to 'settled'")


def evaluate(args: argparse.Namespace) -> None:
    db = init_db(args.db)
    signal_filter = "" if args.all_signals else "and coalesce(s.signal_type,'') like 'paper_buy%'"
    rows = db.execute(
        f"""
        select
          s.id, s.title, s.outcome, coalesce(s.entry_price,s.market_prob), s.model_prob, s.edge, s.created_at,
          latest.id, coalesce(latest.entry_price,latest.market_prob), latest.created_at, coalesce(s.signal_type,'')
        from signals s
        join (
          select title, outcome, max(id) as latest_id
          from signals
          where market_prob is not null
          group by title, outcome
        ) newest on newest.title = s.title and newest.outcome = s.outcome
        join signals latest on latest.id = newest.latest_id
        where s.edge is not null
          and s.market_prob is not null
          and latest.market_prob is not null
          and s.id <> latest.id
          and s.edge >= ?
          and coalesce(s.entry_price,s.market_prob) <= ?
          and coalesce(s.entry_price,s.market_prob) >= ?
          {signal_filter}
        order by s.edge desc, s.created_at asc
        """,
        (args.edge_threshold, args.max_entry, args.min_entry),
    ).fetchall()

    candidates = []
    dates = []
    for row in rows:
        sid, title, outcome, entry, model_prob, edge, created_at, latest_id, latest_prob, latest_at, signal_type = row
        pnl, status = mark_pnl(float(entry), float(latest_prob), float(edge))
        for value in (created_at, latest_at):
            date = parse_date_prefix(str(value))
            if date:
                dates.append(date)
        candidates.append(
            {
                "id": sid,
                "title": title,
                "outcome": outcome,
                "entry": float(entry),
                "model": float(model_prob),
                "edge": float(edge),
                "created_at": created_at,
                "latest_id": latest_id,
                "latest": float(latest_prob),
                "latest_at": latest_at,
                "pnl": pnl,
                "status": status,
                "verdict": signal_type or "paper_buy",
            }
        )

    selected = candidates[: args.limit]
    sample_days = (max(dates) - min(dates)).days + 1 if dates else 0
    total_pnl = sum(c["pnl"] for c in selected)
    mean_edge = sum(c["edge"] for c in selected) / len(selected) if selected else 0.0
    wins = sum(1 for c in selected if c["status"] == "resolved_win")
    losses = sum(1 for c in selected if c["status"] == "resolved_loss")
    unresolved = sum(1 for c in selected if c["status"] == "unresolved")
    all_unresolved = sum(1 for c in candidates if c["status"] == "unresolved")

    print("paper_evaluation=mark_to_market")
    print("mode=paper_only live_trading=false")
    print(
        f"candidates={len(candidates)} sample_days={sample_days} "
        f"evaluated_top_positive_edge={len(selected)} default_filter={'all' if args.all_signals else 'paper_buy'}"
    )
    if len(candidates) < 300 or sample_days < 14:
        print(
            "warning=insufficient_data "
            f"need_candidates>=300 need_calendar_days>=14 have_candidates={len(candidates)} have_calendar_days={sample_days}"
        )
    print(
        f"top_counts wins={wins} losses={losses} unresolved={unresolved} "
        f"mean_edge={mean_edge:+.2%} marked_pnl={total_pnl:+.4f}"
    )
    print(f"unresolved_count={all_unresolved}")
    for c in selected:
        print(
            f"{c['pnl']:+.4f} {c['status']} verdict={c['verdict']} edge={c['edge']:+.1%} "
            f"entry={c['entry']:.1%} latest={c['latest']:.1%} model={c['model']:.1%} "
            f"{c['outcome']} | {str(c['title'])[:90]} ({c['created_at']} -> {c['latest_at']})"
        )


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone())


def table_count(db: sqlite3.Connection, table: str) -> int:
    if not table_exists(db, table):
        return 0
    row = db.execute(f"select count(*) from {table}").fetchone()
    return int(row[0] or 0)


def health(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    db = connect_sqlite(args.db, readonly=True) if os.path.exists(args.db) else init_db(args.db)
    metrics = portfolio_metrics(db)
    counts = {
        "runs": table_count(db, "runs"),
        "markets": table_count(db, "markets"),
        "signals": table_count(db, "signals"),
        "training_rows": table_count(db, "training_rows"),
        "paper_orders": table_count(db, "paper_orders"),
        "paper_positions": table_count(db, "paper_positions"),
        "forecast_snapshots": table_count(db, "forecast_snapshots"),
        "station_observations": table_count(db, "station_observations"),
        "orderbook_snapshots": table_count(db, "orderbook_snapshots"),
    }
    print("status=ok")
    print("mode=paper_only live_trading=false wallet=false order_placement=false")
    print(f"db={args.db}")
    print(f"report={args.report}")
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    print(
        f"paper_account={metrics.get('account_name', active_paper_account_name())} cash={metrics['cash']:.2f} "
        f"equity={metrics['equity']:.2f} open_exposure={metrics['open_exposure']:.2f} "
        f"return={metrics['return_pct']:+.2%} drawdown={metrics['drawdown']:.2%}"
    )


def stations(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    db = init_db(args.db)
    where = ["1=1"]
    params: list[Any] = []
    if not args.include_inactive:
        where.append("active=1")
    if args.city:
        like = f"%{city_key(args.city) or args.city.lower()}%"
        where.append("(city_key like ? or lower(coalesce(city_name,'')) like ? or lower(station_id) like ?)")
        params.extend([like, like, like])
    rows = db.execute(
        f"""
        select city_key, city_name, station_id, station_name, source_url, timezone, reliability, active
        from station_registry
        where {' and '.join(where)}
        order by city_key
        limit ?
        """,
        (*params, args.limit),
    ).fetchall()
    override_count = table_count(db, "station_overrides")
    print(f"stations={len(rows)} overrides={override_count}")
    for city_key_value, city_name, station_id, station_name, source_url, timezone_name, reliability, active in rows:
        status = "active" if active else "inactive"
        print(
            f"{city_key_value} station={station_id} name={station_name or city_name or ''} "
            f"timezone={timezone_name or ''} reliability={reliability or ''} status={status} source={source_url or ''}"
        )


def latest_run_id(db: sqlite3.Connection) -> int | None:
    row = db.execute("select max(id) from runs").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def ladder_groups_from_db(db: sqlite3.Connection, run_id: int | None = None, all_runs: bool = False) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    params: list[Any] = []
    where = ["1=1"]
    if run_id is not None:
        where.append("run_id=?")
        params.append(run_id)
    elif not all_runs:
        latest = latest_run_id(db)
        if latest is None:
            return {}
        where.append("run_id=?")
        params.append(latest)
    rows = db.execute(
        f"""
        select run_id, market_id, title, city, target_date, station_id, market_family,
               outcome, model_prob, entry_price, bid, ask, bucket_state
        from signals
        where {' and '.join(where)}
        order by run_id desc, market_id, bucket_lo_f, outcome
        """,
        params,
    ).fetchall()
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row[0], row[1], row[2], row[3], row[4], row[5], row[6])
        grouped.setdefault(key, []).append(
            {
                "outcome": row[7],
                "model_prob": row[8],
                "entry_price": row[9],
                "bid": row[10],
                "ask": row[11],
                "bucket_state": row[12],
            }
        )
    return grouped


def ladders(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    db = init_db(args.db)
    groups = ladder_groups_from_db(db, args.run_id, args.all_runs)
    print(f"ladders={len(groups)}")
    for index, (key, rows) in enumerate(groups.items()):
        if index >= args.limit:
            break
        run_id, market_id, title, city, target_date, station_id, family = key
        diag = ladder_diagnostics(rows)
        print(
            f"run_id={run_id} market_id={market_id} city={city or ''} date={target_date or ''} "
            f"station={station_id or ''} family={family or ''} outcomes={len(rows)} diag={diag} | {str(title)[:100]}"
        )


def portfolio(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    db = init_db(args.db)
    now = utc_now_iso()
    if args.settle:
        reconcile_zombie_positions(db, now)
        settle_paper_positions_from_latest_prices(db, now)
    if getattr(args, "settle_labels", False):
        settle_paper_positions_from_labels(db, now)
    if args.snapshot:
        record_account_snapshot(db, None, now)
    if args.settle or getattr(args, "settle_labels", False) or args.snapshot:
        db.commit()
    metrics = portfolio_metrics(db)
    print("portfolio=paper_only")
    print(
        f"cash={metrics['cash']:.2f} equity={metrics['equity']:.2f} "
        f"open_exposure={metrics['open_exposure']:.2f} realized_pnl={metrics['realized_pnl']:.2f} "
        f"unrealized_pnl={metrics['unrealized_pnl']:.2f} return={metrics['return_pct']:+.2%} "
        f"drawdown={metrics['drawdown']:.2%} unresolved_positions={metrics['unresolved_positions']}"
    )
    rows = db.execute(
        """
        select market_id, outcome, shares, avg_price, cost_basis, latest_mark, status, updated_at
        from paper_positions
        where account_id=?
        order by updated_at desc, id desc
        limit ?
        """,
        (metrics["account_id"], args.positions),
    ).fetchall()
    for market_id, outcome, shares, avg_price, cost_basis, latest_mark, status, updated_at in rows:
        mark = "" if latest_mark is None else f"{float(latest_mark):.3f}"
        print(
            f"position status={status} shares={float(shares):.2f} avg={float(avg_price):.3f} "
            f"cost={float(cost_basis):.2f} mark={mark} outcome={outcome} market={market_id} updated={updated_at}"
        )


def export_training_rows(db: sqlite3.Connection, output_path: str, limit: int | None = None, include_features: bool = False) -> int:
    columns = list(TRAINING_EXPORT_COLUMNS)
    if include_features:
        columns.append("features_json")
    sql = f"select {','.join(columns)} from training_rows order by id"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " limit ?"
        params = (limit,)
    rows = db.execute(sql, params).fetchall()
    if output_path == "-":
        out = sys.stdout
        close_after = False
    else:
        directory = os.path.dirname(os.path.abspath(output_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        out = open(output_path, "w", encoding="utf-8", newline="")
        close_after = True
    try:
        writer = csv.writer(out)
        writer.writerow(columns)
        writer.writerows(rows)
    finally:
        if close_after:
            out.close()
    return len(rows)


def export_training(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    db = init_db(args.db)
    count = export_training_rows(db, args.output, args.limit, args.include_features)
    print(f"training_rows_exported={count} output={args.output}")


def goal_minimum(text: str, key: str, default: int) -> int:
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(\d+)\s*$", text, re.M)
    return int(m.group(1)) if m else default


def training_calendar_days(db: sqlite3.Connection) -> int:
    rows = db.execute("select min(created_at), max(created_at) from training_rows").fetchone()
    start = parse_date_prefix(rows[0]) if rows and rows[0] else None
    end = parse_date_prefix(rows[1]) if rows and rows[1] else None
    return ((end - start).days + 1) if start and end else 0


def tune(args: argparse.Namespace) -> None:
    ensure_paper_only_guard(args)
    if args.init_goal:
        wrote = write_default_goal(args.goal, overwrite=args.overwrite_goal)
        print(f"goal_file={args.goal} {'created' if wrote else 'exists'}")
    state = tuning_evaluator.evaluate_tuning_state(
        args.db,
        args.goal,
        tuning_evaluator.RUNTIME_TUNABLES_PATH,
    )
    evidence = state["evidence"]
    minimums = state["minimums"]
    status = state["status"]
    print("tune=paper_only")
    print(
        f"goal_file={args.goal} exists={str(state['goal_exists']).lower()} "
        f"guardrails={'ok' if state['guardrails_ok'] else 'blocked'}"
    )
    print(
        f"training_rows={evidence['training_rows']} labeled_rows={evidence['labeled_rows']} "
        f"paper_buy_rows={evidence['paper_buy_rows']} calendar_days={evidence['calendar_days']} "
        f"need_training_rows={minimums['training_rows']} need_labeled_rows={minimums['labeled_rows']} "
        f"need_days={minimums['calendar_days']}"
    )
    print(
        f"current_tunables={json.dumps(state['current_tunables'], sort_keys=True)} "
        f"allowed_tunables={json.dumps(state['allowed_tunables'], sort_keys=True)}"
    )
    print(f"source_families={json.dumps(state.get('source_families', []), sort_keys=True)}")
    print(f"feature_families={json.dumps(state.get('feature_families', []), sort_keys=True)}")
    print(f"metric_readiness={json.dumps(state.get('metric_readiness', {}), sort_keys=True)}")
    post_labels = state.get("post_labels", {})
    print(
        f"paper_forward_test={post_labels.get('status', 'not-approved')} "
        f"approved_for_paper_forward_test={str(post_labels.get('approved_for_paper_forward_test', False)).lower()} "
        "live_trading=false order_placement=false live_money_deployment=false"
    )
    iteration = tuning_evaluator.record_tuning_iteration(state, args.iteration_log)
    print(f"iteration_id={iteration['id']} iteration_log={args.iteration_log}")
    print(f"status={status} promotion=propose_only live_trading=false order_placement=false")
    if status in {"ready_for_proposals", "approved_for_paper_forward_test"}:
        print("candidate_scope=config_only tunables=sources,cadence,cache_ttl,sigma,edge_threshold,max_spread,entry_bounds,paper_size,min_fill,source_quality,position_caps,paper_forward_gate")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Paper research scanner for Polymarket weather markets.")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--report", default=REPORT_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--url", default=os.environ.get("POLYMARKET_WEATHER_URL", DEFAULT_URL))
    s.add_argument("--city", help="Fallback city if a market title does not expose one")
    s.add_argument("--sigma", type=float, default=float(os.environ.get("WEATHER_SIGMA_F", "3.5")))
    s.add_argument("--pause", type=float, default=float(os.environ.get("SCAN_PAUSE_SECONDS", "0.2")), help="Seconds between weather API calls")
    s.add_argument("--paper-size", type=float, default=float(os.environ.get("PAPER_SIZE_SHARES", "5.0")), help="Paper size in outcome shares for executable CLOB entry")
    s.add_argument("--edge-threshold", type=float, default=float(os.environ.get("EDGE_THRESHOLD", "0.08")), help="Minimum edge for a strong paper_buy signal")
    s.add_argument("--max-spread", type=float, default=float(os.environ.get("MAX_SPREAD", "0.12")), help="Maximum acceptable CLOB spread for paper_buy")
    s.add_argument("--max-entry", type=float, default=float(os.environ.get("MAX_ENTRY", "0.95")), help="Skip effectively settled/too-expensive paper entries")
    s.add_argument("--min-entry", type=float, default=float(os.environ.get("MIN_ENTRY", "0.02")), help="Skip dust/stale paper entries below this executable price")
    s.add_argument("--enable-wu", action="store_true", default=env_bool("ENABLE_WU", False), help="Attempt slow/best-effort Weather Underground station reads; default off for cron reliability")
    s.add_argument("--disable-ledger", action="store_true", help="Do not write simulated paper orders/fills/positions for this scan")
    s.add_argument("--allow-weak-families", action="store_true", help="Allow paper ledger fills for strategy families that have not passed the survival gate")
    s.add_argument("--strict-survival-gate", action="store_true", help="Promote-only paper fills: block INCONCLUSIVE and CONTINUE_OBSERVING families as well as killed families")
    s.add_argument("--shadow-ladder-inconsistency", dest="shadow_ladder_inconsistency", action="store_true", default=env_bool("SHADOW_LADDER_INCONSISTENCY", True), help="Write ladder_inconsistency fills only to the counterfactual shadow ledger (default on)")
    s.add_argument("--allow-ladder-inconsistency-official", dest="shadow_ladder_inconsistency", action="store_false", help="Permit ladder_inconsistency to mutate official paper ledger; paper-only override for experiments")
    s.add_argument("--shadow-strategy-family", dest="shadow_strategy_families", action="append", default=[], help="Additional strategy family to write only to the shadow ledger; repeatable")
    s.set_defaults(disable_weak_families=True)
    s.add_argument("--max-position-pct", type=float, default=float(os.environ.get("MAX_POSITION_PCT", "0.02")), help="Max simulated cash exposure per market position")
    s.add_argument("--max-city-date-pct", type=float, default=float(os.environ.get("MAX_CITY_DATE_PCT", "0.10")), help="Max simulated cash exposure per city/date")
    s.add_argument("--max-open-exposure-pct", type=float, default=float(os.environ.get("MAX_OPEN_EXPOSURE_PCT", "0.50")), help="Max simulated open exposure")
    s.add_argument("--min-fill-shares", type=float, default=float(os.environ.get("MIN_FILL_SHARES", "1.0")), help="Minimum simulated executable shares for a paper fill")
    s.set_defaults(func=scan)
    h = sub.add_parser("health", help="Show local paper-only system health and database counts")
    h.set_defaults(func=health)
    st = sub.add_parser("stations", help="List station registry entries")
    st.add_argument("--city", help="Filter by city key, city name, or station id")
    st.add_argument("--limit", type=int, default=50)
    st.add_argument("--include-inactive", action="store_true")
    st.set_defaults(func=stations)
    l = sub.add_parser("ladders", help="Show ladder diagnostics from stored signals")
    l.add_argument("--run-id", type=int)
    l.add_argument("--all-runs", action="store_true")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=ladders)
    pf = sub.add_parser("portfolio", help="Show simulated paper portfolio metrics")
    pf.add_argument("--positions", type=int, default=10, help="Number of recent positions to print")
    pf.add_argument("--snapshot", action="store_true", help="Record a paper account snapshot")
    pf.add_argument("--settle", action="store_true", help="Settle paper positions from latest stored prices when possible")
    pf.add_argument("--settle-labels", action="store_true", help="Settle paper positions from final delayed labels when available")
    pf.set_defaults(func=portfolio)
    lb = sub.add_parser("label", help="Attempt delayed read-only weather labels and settle paper positions from final labels")
    lb.add_argument("--limit", type=int, default=int(os.environ.get("LABEL_LIMIT", "25")), help="Maximum unresolved market/outcome groups to attempt")
    lb.add_argument("--min-age-days", type=int, default=int(os.environ.get("LABEL_MIN_AGE_DAYS", "2")), help="Only label target dates at least this many days old")
    lb.add_argument("--retry-after-hours", type=float, default=float(os.environ.get("LABEL_RETRY_AFTER_HOURS", "12")), help="Do not retry the same unresolved group sooner than this")
    lb.add_argument("--pause", type=float, default=float(os.environ.get("LABEL_PAUSE_SECONDS", os.environ.get("SCAN_PAUSE_SECONDS", "0.05"))), help="Seconds between delayed source calls")
    lb.add_argument("--http-timeout", type=float, default=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "8")), help="HTTP timeout for read-only label sources")
    lb.add_argument("--cache-ttl", type=int, default=int(os.environ.get("OBSERVATION_CACHE_TTL_SECONDS", "300")), help="In-process source cache TTL seconds")
    lb.add_argument("--enable-ncei", dest="enable_ncei", action="store_true", default=env_bool("ENABLE_NCEI_DAILY", True), help="Use tokenless NOAA/NCEI daily summaries for final labels")
    lb.add_argument("--disable-ncei", dest="enable_ncei", action="store_false", help="Disable NOAA/NCEI daily summary label attempts")
    lb.add_argument("--enable-nws", dest="enable_nws", action="store_true", default=env_bool("ENABLE_NWS", False), help="Use NWS station observations as provisional supporting evidence")
    lb.add_argument("--disable-nws", dest="enable_nws", action="store_false", help="Disable NWS provisional label attempts")
    lb.add_argument("--enable-iem", dest="enable_iem", action="store_true", default=env_bool("ENABLE_IEM", False), help="Use IEM/ASOS daily highs as provisional supporting evidence")
    lb.add_argument("--disable-iem", dest="enable_iem", action="store_false", help="Disable IEM/ASOS provisional label attempts")
    lb.add_argument("--enable-metar-direct", dest="enable_metar_direct", action="store_true", default=env_bool("ENABLE_METAR_DIRECT", False), help="Use AviationWeather current METAR as non-final diagnostic evidence")
    lb.add_argument("--disable-metar-direct", dest="enable_metar_direct", action="store_false", help="Disable AviationWeather current METAR diagnostics")
    lb.add_argument("--enable-meteostat", dest="enable_meteostat", action="store_true", default=env_bool("ENABLE_METEOSTAT", False), help="Record Meteostat optional-backfill diagnostics when available")
    lb.add_argument("--disable-meteostat", dest="enable_meteostat", action="store_false", help="Disable Meteostat optional-backfill diagnostics")
    lb.add_argument("--dry-run", action="store_true", help="Find candidates and sources without writing labels, attempts, settlements, or snapshots")
    lb.add_argument("--no-settle", action="store_true", help="Do not settle paper positions after writing final labels")
    lb.set_defaults(func=label)
    x = sub.add_parser("export-training", help="Export decision-time training rows to CSV")
    x.add_argument("--output", default=TRAINING_EXPORT_PATH)
    x.add_argument("--limit", type=int)
    x.add_argument("--include-features", action="store_true")
    x.set_defaults(func=export_training)
    t = sub.add_parser("tune", help="Paper-only tuning scaffold over historical training rows")
    t.add_argument("--goal", default=GOAL_PATH)
    t.add_argument("--iteration-log", default=tuning_evaluator.TUNING_ITERATIONS_PATH, help="Append a paper-only tuning iteration JSONL record")
    t.add_argument("--init-goal", action="store_true", help="Create the default goal file if missing")
    t.add_argument("--overwrite-goal", action="store_true", help="Overwrite the goal file when used with --init-goal")
    t.set_defaults(func=tune)
    q = sub.add_parser("summary")
    q.add_argument("--limit", type=int, default=12)
    q.set_defaults(func=summary)
    e = sub.add_parser("evaluate", help="Evaluate autonomous paper candidates against latest observed prices")
    e.add_argument("--limit", type=int, default=20, help="Number of top positive-edge paper candidates to report")
    e.add_argument("--edge-threshold", type=float, default=0.08, help="Minimum model edge for a paper candidate")
    e.add_argument("--max-entry", type=float, default=0.95, help="Skip effectively settled/too-expensive entries")
    e.add_argument("--min-entry", type=float, default=0.02, help="Skip dust/stale entries below this entry price")
    e.add_argument("--all-signals", action="store_true", help="Evaluate watch/skip rows too; default is paper_buy only")
    e.set_defaults(func=evaluate)
    rc = sub.add_parser("reconcile", help="Sync zombie positions stuck in 'open' that already have settlement records")
    rc.set_defaults(func=reconcile)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ensure_paper_only_guard(args)
        args.func(args)
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
