#!/usr/bin/env python3
"""Read-only tuning readiness evaluator for the paper weather dashboard."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from typing import Any

import features


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "paper_weather.sqlite3")
GOAL_PATH = os.path.join(ROOT, "goals", "paper_weather_edge_v1.yaml")
RUNTIME_TUNABLES_PATH = os.path.join(ROOT, "runtime_tunables.env")
TUNING_ITERATIONS_PATH = os.path.join(ROOT, "tuning_iterations.jsonl")
ITERATION_SCHEMA_VERSION = 1
FINAL_LABEL_WHERE = "label_status='final' and label_value in (0, 1)"


TUNABLE_ENV_NAMES = {
    "SCAN_INTERVAL_MINUTES": "scan_interval_minutes",
    "DASHBOARD_POLL_SECONDS": "dashboard_poll_seconds",
    "SCAN_PAUSE_SECONDS": "scan_pause_seconds",
    "PAPER_SIZE_SHARES": "paper_size_shares",
    "EDGE_THRESHOLD": "edge_threshold",
    "MAX_SPREAD": "max_spread",
    "MIN_ENTRY": "min_entry",
    "MAX_ENTRY": "max_entry",
    "MAX_POSITION_PCT": "max_position_pct",
    "MAX_CITY_DATE_PCT": "max_city_date_pct",
    "MAX_OPEN_EXPOSURE_PCT": "max_open_exposure_pct",
    "MIN_FILL_SHARES": "min_fill_shares",
    "ENABLE_WU": "enable_wu",
    "ENABLE_NWS": "enable_nws",
    "ENABLE_IEM": "enable_iem",
    "ENABLE_NCEI_DAILY": "enable_ncei_daily",
    "ENABLE_CDO": "enable_cdo",
    "ENABLE_METEOSTAT": "enable_meteostat",
    "ENABLE_METAR_DIRECT": "enable_metar_direct",
    "ENABLE_OM_MODELS": "enable_om_models",
    "ENABLE_OM_HISTORICAL_FORECASTS": "enable_om_historical_forecasts",
    "ENABLE_POLY_HISTORY": "enable_poly_history",
    "ALLOW_PAID_PROVIDER_FEATURES": "allow_paid_provider_features",
    "MARKET_METADATA_CACHE_TTL_SECONDS": "market_metadata_cache_ttl_seconds",
    "FORECAST_CACHE_TTL_SECONDS": "forecast_cache_ttl_seconds",
    "OBSERVATION_CACHE_TTL_SECONDS": "observation_cache_ttl_seconds",
    "ORDERBOOK_CACHE_TTL_SECONDS": "orderbook_cache_ttl_seconds",
    "HTTP_TIMEOUT_SECONDS": "http_timeout_seconds",
    "SOURCE_BACKOFF_MINUTES": "source_backoff_minutes",
    "PER_SOURCE_DAILY_CALL_BUDGET": "per_source_daily_call_budget",
    "EDGE_THRESHOLD_TOUCHED": "edge_threshold_touched",
    "EDGE_THRESHOLD_AMBIGUOUS_STATION": "edge_threshold_ambiguous_station",
    "MAX_SLIPPAGE": "max_slippage",
    "MIN_TOP_OF_BOOK_DEPTH": "min_top_of_book_depth",
    "STALE_BOOK_MAX_AGE_SECONDS": "stale_book_max_age_seconds",
    "MAX_STATION_DATE_PCT": "max_station_date_pct",
    "MAX_LADDER_GROUP_PCT": "max_ladder_group_pct",
    "MIN_STATION_CONFIDENCE_FOR_BUY": "min_station_confidence_for_buy",
    "STATION_DISTANCE_PENALTY_PER_MILE": "station_distance_penalty_per_mile",
    "ELEVATION_DELTA_PENALTY_PER_100FT": "elevation_delta_penalty_per_100ft",
    "UNKNOWN_STATION_EDGE_HAIRCUT": "unknown_station_edge_haircut",
    "AMBIGUOUS_RULE_EDGE_HAIRCUT": "ambiguous_rule_edge_haircut",
    "MISSINGNESS_PENALTY": "missingness_penalty",
    "SOURCE_DISAGREEMENT_THRESHOLD_F": "source_disagreement_threshold_f",
    "SOURCE_WEIGHT_OPEN_METEO": "source_weight_open_meteo",
    "SOURCE_WEIGHT_NWS": "source_weight_nws",
    "SOURCE_WEIGHT_IEM": "source_weight_iem",
    "SOURCE_WEIGHT_WU": "source_weight_wu",
    "SOURCE_WEIGHT_NCEI": "source_weight_ncei",
    "PAPER_FORWARD_MIN_LABELED_ROWS": "paper_forward_min_labeled_rows",
    "PAPER_FORWARD_MIN_CALENDAR_DAYS": "paper_forward_min_calendar_days",
    "PAPER_FORWARD_MAX_DRAWDOWN": "paper_forward_max_drawdown",
    "PAPER_FORWARD_MIN_FEATURE_COVERAGE": "paper_forward_min_feature_coverage",
}


BOOL_TUNABLE_NAMES = {
    "enable_wu",
    "enable_nws",
    "enable_iem",
    "enable_ncei_daily",
    "enable_cdo",
    "enable_meteostat",
    "enable_metar_direct",
    "enable_om_models",
    "enable_om_historical_forecasts",
    "enable_poly_history",
    "allow_paid_provider_features",
}


SOURCE_FAMILIES = [
    {"key": "polymarket_metadata", "label": "Polymarket market metadata", "required": True, "flag": None, "table": "markets"},
    {"key": "polymarket_clob", "label": "Polymarket CLOB public order book", "required": True, "flag": None, "table": "orderbook_snapshots"},
    {"key": "open_meteo", "label": "Open-Meteo baseline forecast", "required": True, "flag": None, "table": "forecast_snapshots", "provider": "open_meteo"},
    {"key": "weather_underground", "label": "Weather Underground station reads", "required": False, "flag": "enable_wu", "table": "station_observations", "source": "weather_underground"},
    {"key": "nws", "label": "NWS points/stations/observations", "required": False, "flag": "enable_nws", "table": "station_observations", "source": "nws"},
    {"key": "iem_metar", "label": "IEM/ASOS/METAR observations", "required": False, "flag": "enable_iem", "table": "station_observations", "source": "iem_metar"},
    {"key": "metar_direct", "label": "AviationWeather METAR direct", "required": False, "flag": "enable_metar_direct", "table": "station_observations", "source": "metar_direct"},
    {"key": "ncei_daily_labels", "label": "NOAA/NCEI delayed labels", "required": False, "flag": "enable_ncei_daily", "table": "training_rows", "label_source": "ncei"},
    {"key": "meteostat", "label": "Meteostat history/climatology", "required": False, "flag": "enable_meteostat", "table": None},
    {"key": "commercial_weather", "label": "Commercial weather provider stubs", "required": False, "flag": "allow_paid_provider_features", "table": None, "adapter_stub": True},
]


def _strip_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return value[:index].strip()
    return value.strip()


def _parse_scalar(value: str) -> Any:
    raw = _strip_comment(value)
    if raw == "":
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    lower = raw.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    try:
        if any(ch in raw for ch in (".", "e", "E")):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def parse_goal_config(text: str) -> dict[str, Any]:
    """Parse the small declarative goal YAML subset used by this project."""
    config: dict[str, Any] = {}
    current_section: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = _strip_comment(raw_value)
        if indent == 0:
            if value == "":
                config[key] = {}
                current_section = key
            else:
                config[key] = _parse_scalar(value)
                current_section = None
        elif current_section and isinstance(config.get(current_section), dict):
            config[current_section][key] = _parse_scalar(value)
    return config


def load_goal_config(path: str = GOAL_PATH) -> tuple[dict[str, Any], bool]:
    if not os.path.exists(path):
        return {}, False
    with open(path, encoding="utf-8") as f:
        return parse_goal_config(f.read()), True


def load_runtime_tunables(path: str = RUNTIME_TUNABLES_PATH) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    values: dict[str, Any] = {}
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            raw_key, raw_value = line.split("=", 1)
            key = TUNABLE_ENV_NAMES.get(raw_key.strip(), raw_key.strip().lower())
            values[key] = _parse_scalar(raw_value.strip())
    for key in BOOL_TUNABLE_NAMES:
        if key in values:
            raw = values[key]
            if isinstance(raw, bool):
                values[key] = raw
            elif str(raw).isdigit():
                values[key] = bool(int(str(raw)))
            else:
                values[key] = str(raw).strip().lower() in {"true", "yes", "on", "enabled"}
    return values


def parse_date_prefix(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value)
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return bool(row)


def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(db, table):
        return False
    return column in {row[1] for row in db.execute(f"pragma table_info({table})")}


def count_rows(db: sqlite3.Connection, table: str, where: str = "1=1") -> int:
    allowed = {
        "training_rows",
        "paper_account_snapshots",
        "markets",
        "signals",
        "forecast_snapshots",
        "station_observations",
        "orderbook_snapshots",
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "paper_settlements",
        "label_attempts",
    }
    if table not in allowed:
        raise ValueError(f"unexpected table name: {table}")
    if not table_exists(db, table):
        return 0
    return int(db.execute(f"select count(*) from {table} where {where}").fetchone()[0] or 0)


def calendar_days(db: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(db, "training_rows"):
        return {"days": 0, "first_date": None, "last_date": None}
    row = db.execute("select min(created_at), max(created_at) from training_rows").fetchone()
    first = parse_date_prefix(row[0]) if row and row[0] else None
    last = parse_date_prefix(row[1]) if row and row[1] else None
    days = ((last - first).days + 1) if first and last else 0
    return {
        "days": days,
        "first_date": first.isoformat() if first else None,
        "last_date": last.isoformat() if last else None,
    }


def guardrails_ok(goal: dict[str, Any]) -> bool:
    guardrails = goal.get("guardrails") if isinstance(goal.get("guardrails"), dict) else {}
    return (
        goal.get("paper_only") is True
        and goal.get("mode") == "paper_only"
        and goal.get("promotion") == "propose_only"
        and guardrails.get("live_trading") is False
        and guardrails.get("wallet_required") is False
        and guardrails.get("order_placement") is False
        and guardrails.get("live_money_deployment", False) is False
    )


def range_label(values: Any) -> str:
    if isinstance(values, list):
        return ", ".join(str(value) for value in values)
    return str(values)


def sqlite_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe sqlite identifier: {name}")
    return name


def source_specific_count(db: sqlite3.Connection, family: dict[str, Any]) -> int:
    table = family.get("table")
    if not table or not table_exists(db, table):
        return 0
    sqlite_identifier(str(table))
    if family.get("provider") and column_exists(db, table, "provider"):
        return count_rows(db, table, f"lower(coalesce(provider,''))='{str(family['provider']).lower()}'")
    if family.get("source") and column_exists(db, table, "source"):
        return count_rows(
            db,
            table,
            f"lower(coalesce(source,''))='{str(family['source']).lower()}' "
            "and lower(coalesce(raw_status,'')) not in ('disabled','wu_disabled','missing_station_or_date')",
        )
    if family.get("label_source") and column_exists(db, table, "label_source"):
        needle = str(family["label_source"]).lower()
        status_filter = f" and {FINAL_LABEL_WHERE}" if table == "training_rows" and column_exists(db, table, "label_status") else ""
        return count_rows(db, table, f"lower(coalesce(label_source,'')) like '%{needle}%'{status_filter}")
    return count_rows(db, table)


def evaluate_source_families(db: sqlite3.Connection | None, runtime: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family in SOURCE_FAMILIES:
        enabled = True if family.get("flag") is None else bool(runtime.get(str(family["flag"]), False))
        observed = source_specific_count(db, family) if db is not None else 0
        required = bool(family.get("required"))
        if observed > 0:
            status = "active"
        elif not enabled:
            status = "missing" if required else "disabled"
        elif required:
            status = "missing"
        else:
            status = "optional"
        rows.append(
            {
                "key": family["key"],
                "label": family["label"],
                "status": status,
                "enabled": enabled,
                "required": required,
                "rows": observed,
                "runtime_flag": family.get("flag"),
                "adapter_stub": bool(family.get("adapter_stub", False)),
                "provenance_preserved": True,
                "read_only": True,
            }
        )
    return rows


def sql_count_if(db: sqlite3.Connection, table: str, condition: str) -> int:
    return count_rows(db, table, condition)


def feature_family_coverage(db: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if db is None or not table_exists(db, "training_rows"):
        total = 0
    else:
        total = count_rows(db, "training_rows")
    signal_rows = count_rows(db, "signals") if db is not None else 0
    snapshot_rows = count_rows(db, "paper_account_snapshots") if db is not None else 0
    leakage_rows = 0
    if db is not None and table_exists(db, "training_rows") and column_exists(db, "training_rows", "features_json"):
        leakage_rows = count_rows(
            db,
            "training_rows",
            "coalesce(features_json,'') like '%label_value%' or coalesce(features_json,'') like '%final_outcome%' or coalesce(features_json,'') like '%settlement_value%'",
        )
    feature_json_quality_condition = (
        "coalesce(features_json,'') like '%source_quality_score%'"
        if db is not None and column_exists(db, "training_rows", "features_json")
        else "0"
    )
    ladder_count = (
        count_rows(db, "signals", "coalesce(ladder_diagnostic,'') <> ''")
        if db is not None and column_exists(db, "signals", "ladder_diagnostic")
        else 0
    )

    def coverage_row(key: str, label: str, covered: int, critical: bool = True) -> dict[str, Any]:
        denominator = total if key not in {"ladder_consistency", "portfolio_risk"} else max(total, signal_rows, snapshot_rows)
        if denominator <= 0:
            pct = 0.0
        else:
            pct = min(1.0, max(0.0, covered / denominator))
        if denominator <= 0:
            status = "missing" if critical else "optional"
        elif covered <= 0 and critical:
            status = "missing"
        elif covered <= 0:
            status = "optional"
        else:
            status = "active"
        return {
            "key": key,
            "label": label,
            "status": status,
            "covered_rows": int(covered),
            "total_rows": int(denominator),
            "coverage_pct": pct,
            "critical": critical,
        }

    if db is None or total <= 0:
        rows = [
            coverage_row(key, label, 0, critical=(key != "portfolio_risk"))
            for key, label in features.FEATURE_FAMILIES.items()
        ]
    else:
        rows = [
            coverage_row("settlement_source", features.FEATURE_FAMILIES["settlement_source"], sql_count_if(db, "training_rows", "station_id is not null or source_url is not null or source_confidence is not null")),
            coverage_row("forecast_ensemble", features.FEATURE_FAMILIES["forecast_ensemble"], sql_count_if(db, "training_rows", "forecast_snapshot_id is not null or model_prob is not null")),
            coverage_row("live_observation", features.FEATURE_FAMILIES["live_observation"], sql_count_if(db, "training_rows", "observation_id is not null or bucket_state is not null")),
            coverage_row("local_time", features.FEATURE_FAMILIES["local_time"], sql_count_if(db, "training_rows", "target_date is not null and created_at is not null")),
            coverage_row("microstructure", features.FEATURE_FAMILIES["microstructure"], sql_count_if(db, "training_rows", "orderbook_snapshot_id is not null or entry_price is not null or spread is not null or depth is not null")),
            coverage_row("ladder_consistency", features.FEATURE_FAMILIES["ladder_consistency"], ladder_count, critical=False),
            coverage_row("source_quality", features.FEATURE_FAMILIES["source_quality"], sql_count_if(db, "training_rows", f"source_confidence is not null or {feature_json_quality_condition}")),
            coverage_row("portfolio_risk", features.FEATURE_FAMILIES["portfolio_risk"], snapshot_rows, critical=False),
            {
                "key": "no_lookahead",
                "label": features.FEATURE_FAMILIES["no_lookahead"],
                "status": "active" if leakage_rows == 0 else "missing",
                "covered_rows": max(0, total - leakage_rows),
                "total_rows": total,
                "coverage_pct": 1.0 if total <= 0 else max(0.0, (total - leakage_rows) / total),
                "critical": True,
                "leakage_rows": leakage_rows,
            },
        ]
    return rows


def latest_drawdown(db: sqlite3.Connection | None) -> float:
    if db is None or not table_exists(db, "paper_account_snapshots"):
        return 0.0
    row = db.execute("select max(drawdown) from paper_account_snapshots").fetchone()
    return float(row[0] or 0.0)


def available_performance_metrics(db: sqlite3.Connection | None) -> dict[str, Any]:
    if db is None:
        return {
            "snapshot_rows": 0,
            "equity": None,
            "return_pct": None,
            "drawdown": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "training_rows": 0,
            "labeled_rows": 0,
            "paper_buy_rows": 0,
            "open_positions": 0,
            "settled_positions": 0,
            "paper_settlements": 0,
            "avg_edge": None,
            "paper_buy_avg_edge": None,
            "brier_score": None,
            "unresolved_rate": None,
        }

    snapshot_rows = count_rows(db, "paper_account_snapshots")
    latest_snapshot: sqlite3.Row | None = None
    if table_exists(db, "paper_account_snapshots"):
        latest_snapshot = db.execute(
            """
            select equity, return_pct, drawdown, realized_pnl, unrealized_pnl
            from paper_account_snapshots
            order by id desc
            limit 1
            """
        ).fetchone()

    training_rows = count_rows(db, "training_rows")
    labeled_rows = count_rows(db, "training_rows", FINAL_LABEL_WHERE)
    paper_buy_rows = count_rows(db, "training_rows", "coalesce(signal_type,'') like 'paper_buy%'")
    open_positions = count_rows(db, "paper_positions", "status='open'")
    settled_positions = count_rows(db, "paper_positions", "status='settled'")
    paper_settlements = count_rows(db, "paper_settlements")
    avg_edge = paper_buy_avg_edge = brier_score = None
    if table_exists(db, "training_rows"):
        avg_edge = db.execute("select avg(edge) from training_rows where edge is not null").fetchone()[0]
        paper_buy_avg_edge = db.execute(
            "select avg(edge) from training_rows where edge is not null and coalesce(signal_type,'') like 'paper_buy%'"
        ).fetchone()[0]
        brier_score = db.execute(
            """
            select avg((model_prob - label_value) * (model_prob - label_value))
            from training_rows
            where model_prob is not null
              and label_value is not null
              and label_value in (0, 1)
            """
        ).fetchone()[0]

    unresolved_rate = None
    if training_rows > 0:
        unresolved_rate = max(0.0, (training_rows - labeled_rows) / training_rows)

    def value(row: sqlite3.Row | None, key: str) -> float | None:
        if row is None or row[key] is None:
            return None
        return float(row[key])

    return {
        "snapshot_rows": snapshot_rows,
        "equity": value(latest_snapshot, "equity"),
        "return_pct": value(latest_snapshot, "return_pct"),
        "drawdown": value(latest_snapshot, "drawdown"),
        "realized_pnl": value(latest_snapshot, "realized_pnl"),
        "unrealized_pnl": value(latest_snapshot, "unrealized_pnl"),
        "training_rows": training_rows,
        "labeled_rows": labeled_rows,
        "paper_buy_rows": paper_buy_rows,
        "open_positions": open_positions,
        "settled_positions": settled_positions,
        "paper_settlements": paper_settlements,
        "avg_edge": float(avg_edge) if avg_edge is not None else None,
        "paper_buy_avg_edge": float(paper_buy_avg_edge) if paper_buy_avg_edge is not None else None,
        "brier_score": float(brier_score) if brier_score is not None else None,
        "unresolved_rate": unresolved_rate,
    }


def proposed_tunables_for_state(
    status: str,
    current_tunables: dict[str, Any],
    allowed_tunables: dict[str, Any],
) -> dict[str, Any]:
    if status not in {"ready_for_proposals", "approved_for_paper_forward_test"}:
        return {}
    proposals: dict[str, Any] = {}
    for name, values in sorted(allowed_tunables.items()):
        candidate_values = values if isinstance(values, list) else [values]
        proposals[name] = {
            "current": current_tunables.get(name),
            "candidate_values": candidate_values,
            "selected": None,
            "approval_required": True,
            "note": "Config candidate only; no deployment or live trading approval is implied.",
        }
    return proposals


def metric_family_readiness(
    evidence: dict[str, Any],
    minimums: dict[str, int],
    feature_families: list[dict[str, Any]],
    snapshot_rows: int,
    max_drawdown_seen: float,
    guardrail_max_drawdown: float,
) -> dict[str, dict[str, Any]]:
    feature_ok = all(row["status"] != "missing" for row in feature_families if row.get("critical"))
    labeled_ready = evidence["labeled_rows"] >= minimums["labeled_rows"]
    training_ready = evidence["training_rows"] >= minimums["training_rows"]
    days_ready = evidence["calendar_days"] >= minimums["calendar_days"]
    return {
        "realized_return_pct": {
            "ready": snapshot_rows > 0,
            "evidence": snapshot_rows,
            "missing": [] if snapshot_rows > 0 else ["paper_account_snapshots"],
        },
        "brier_score": {
            "ready": labeled_ready and training_ready and feature_ok,
            "evidence": evidence["labeled_rows"],
            "missing": [name for name, ok in (("labeled_rows", labeled_ready), ("training_rows", training_ready), ("critical_features", feature_ok)) if not ok],
        },
        "max_drawdown": {
            "ready": snapshot_rows > 0 and max_drawdown_seen <= guardrail_max_drawdown,
            "evidence": max_drawdown_seen,
            "missing": [] if snapshot_rows > 0 else ["paper_account_snapshots"],
            "guardrail": guardrail_max_drawdown,
        },
        "unresolved_rate": {
            "ready": training_ready and days_ready,
            "evidence": evidence["training_rows"],
            "missing": [name for name, ok in (("training_rows", training_ready), ("calendar_days", days_ready)) if not ok],
        },
        "paper_forward_test": {
            "ready": labeled_ready and training_ready and days_ready and feature_ok and max_drawdown_seen <= guardrail_max_drawdown,
            "evidence": evidence["labeled_rows"],
            "missing": [
                name
                for name, ok in (
                    ("labeled_rows", labeled_ready),
                    ("training_rows", training_ready),
                    ("calendar_days", days_ready),
                    ("critical_features", feature_ok),
                    ("max_drawdown_guardrail", max_drawdown_seen <= guardrail_max_drawdown),
                )
                if not ok
            ],
        },
    }


def evaluate_tuning_state(
    db_path: str = DB_PATH,
    goal_path: str = GOAL_PATH,
    runtime_path: str = RUNTIME_TUNABLES_PATH,
) -> dict[str, Any]:
    """Return JSON-friendly paper-only tuning state without mutating SQLite."""
    goal, goal_exists = load_goal_config(goal_path)
    runtime_tunables = load_runtime_tunables(runtime_path)
    allowed_tunables = goal.get("allowed_tunables") if isinstance(goal.get("allowed_tunables"), dict) else {}
    minimum_training_rows = int(goal.get("minimum_training_rows") or 300)
    minimum_labeled_rows = int(goal.get("minimum_labeled_rows") or minimum_training_rows)
    minimum_calendar_days = int(goal.get("minimum_calendar_days") or 14)
    guardrails = goal.get("guardrails") if isinstance(goal.get("guardrails"), dict) else {}
    guardrail_max_drawdown = float(guardrails.get("max_drawdown") or runtime_tunables.get("paper_forward_max_drawdown") or 0.20)

    if not os.path.exists(db_path):
        training_rows = labeled_rows = paper_buy_rows = snapshot_rows = 0
        span = {"days": 0, "first_date": None, "last_date": None}
        source_families = evaluate_source_families(None, runtime_tunables)
        feature_families = feature_family_coverage(None)
        max_drawdown_seen = 0.0
        performance_metrics = available_performance_metrics(None)
    else:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            training_rows = count_rows(db, "training_rows")
            labeled_rows = count_rows(db, "training_rows", FINAL_LABEL_WHERE)
            paper_buy_rows = count_rows(db, "training_rows", "coalesce(signal_type,'') like 'paper_buy%'")
            snapshot_rows = count_rows(db, "paper_account_snapshots")
            span = calendar_days(db)
            source_families = evaluate_source_families(db, runtime_tunables)
            feature_families = feature_family_coverage(db)
            max_drawdown_seen = latest_drawdown(db)
            performance_metrics = available_performance_metrics(db)

    gates = [
        {
            "name": "training_rows",
            "current": training_rows,
            "minimum": minimum_training_rows,
            "ready": training_rows >= minimum_training_rows,
        },
        {
            "name": "labeled_rows",
            "current": labeled_rows,
            "minimum": minimum_labeled_rows,
            "ready": labeled_rows >= minimum_labeled_rows,
        },
        {
            "name": "calendar_days",
            "current": span["days"],
            "minimum": minimum_calendar_days,
            "ready": span["days"] >= minimum_calendar_days,
        },
    ]
    blocked_reasons = [gate["name"] for gate in gates if not gate["ready"]]
    safe_goal = guardrails_ok(goal)
    critical_feature_missing = [row["key"] for row in feature_families if row.get("critical") and row.get("status") == "missing"]
    no_lookahead_row = next((row for row in feature_families if row["key"] == "no_lookahead"), {})
    leakage_rows = int(no_lookahead_row.get("leakage_rows") or 0)
    minimums = {
        "training_rows": minimum_training_rows,
        "labeled_rows": minimum_labeled_rows,
        "calendar_days": minimum_calendar_days,
    }
    evidence = {
        "training_rows": training_rows,
        "labeled_rows": labeled_rows,
        "paper_buy_rows": paper_buy_rows,
        "calendar_days": span["days"],
        "first_date": span["first_date"],
        "last_date": span["last_date"],
        "snapshot_rows": snapshot_rows,
    }
    metric_readiness = metric_family_readiness(
        evidence,
        minimums,
        feature_families,
        snapshot_rows,
        max_drawdown_seen,
        guardrail_max_drawdown,
    )
    paper_forward_ready = (
        safe_goal
        and not blocked_reasons
        and not critical_feature_missing
        and leakage_rows == 0
        and max_drawdown_seen <= guardrail_max_drawdown
    )
    if not goal_exists or not safe_goal:
        status = "scaffold_only"
    elif paper_forward_ready:
        status = "approved_for_paper_forward_test"
    elif blocked_reasons:
        status = "insufficient_data"
    else:
        status = "ready_for_proposals"

    current_tunables = dict(goal.get("current_runtime") or {})
    current_tunables.update(runtime_tunables)
    proposed_tunables = proposed_tunables_for_state(status, current_tunables, allowed_tunables)
    source_counts = {
        "active": sum(1 for row in source_families if row["status"] == "active"),
        "missing": sum(1 for row in source_families if row["status"] == "missing"),
        "optional": sum(1 for row in source_families if row["status"] == "optional"),
        "disabled": sum(1 for row in source_families if row["status"] == "disabled"),
    }
    feature_counts = {
        "active": sum(1 for row in feature_families if row["status"] == "active"),
        "missing": sum(1 for row in feature_families if row["status"] == "missing"),
        "optional": sum(1 for row in feature_families if row["status"] == "optional"),
    }
    post_labels_note = (
        "Approved for paper-forward-test config proposals only; live trading remains disabled."
        if paper_forward_ready
        else "Collect more labeled paper outcomes before approved-for-paper-forward-test proposal approval."
    )

    return {
        "status": status,
        "scaffold_only": status == "scaffold_only",
        "proposal_only": True,
        "promotion": "propose_only",
        "goal_file": os.path.abspath(goal_path),
        "goal_exists": goal_exists,
        "runtime_file": os.path.abspath(runtime_path),
        "guardrails_ok": safe_goal,
        "safety": {
            "paper_only": True,
            "wallet": False,
            "live_trading": False,
            "order_placement": False,
            "signing": False,
            "deployment": False,
            "live_money_deployment": False,
        },
        "evidence": evidence,
        "minimums": minimums,
        "gates": gates,
        "blocked_reasons": blocked_reasons,
        "source_families": source_families,
        "source_family_counts": source_counts,
        "feature_families": feature_families,
        "feature_family_counts": feature_counts,
        "feature_missingness_summary": {
            "training_rows": training_rows,
            "critical_feature_missing": critical_feature_missing,
            "no_lookahead_leakage_rows": leakage_rows,
            "feature_schema_version": features.FEATURE_SCHEMA_VERSION,
        },
        "metric_readiness": metric_readiness,
        "available_performance_metrics": performance_metrics,
        "post_labels": {
            "status": "approved-for-paper-forward-test" if paper_forward_ready else "not-approved",
            "approved_for_paper_forward_test": paper_forward_ready,
            "guardrails_passed": safe_goal and leakage_rows == 0 and max_drawdown_seen <= guardrail_max_drawdown,
            "guardrail_max_drawdown": guardrail_max_drawdown,
            "max_drawdown_seen": max_drawdown_seen,
            "live_trading_approval": False,
            "live_money_deployment": False,
            "note": post_labels_note,
        },
        "current_tunables": current_tunables,
        "proposed_tunables": proposed_tunables,
        "allowed_tunables": allowed_tunables,
        "target_metrics": {
            "primary": goal.get("primary_metric") or "realized_return_pct",
            "secondary": goal.get("secondary_metrics") or "brier_score, max_drawdown, unresolved_rate",
            "validation_method": goal.get("validation_method") or "date_walk_forward",
        },
        "proposal_trace": {
            "active": paper_forward_ready,
            "count": len(proposed_tunables),
            "latest": None,
            "note": post_labels_note,
        },
    }


def _existing_iteration_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _safe_iteration_id(timestamp: str, sequence: int) -> str:
    safe_ts = re.sub(r"[^0-9A-Za-z]", "", timestamp.replace("+00:00", "Z"))
    return f"tune-{safe_ts}-{sequence:06d}"


def build_tuning_iteration_record(
    state: dict[str, Any],
    *,
    timestamp: str | None = None,
    iteration_id: str | None = None,
    sequence: int | None = None,
    persisted: bool = True,
) -> dict[str, Any]:
    ts = timestamp or utc_now_iso()
    seq = 0 if sequence is None else sequence
    post_labels = state.get("post_labels") or {}
    evidence = dict(state.get("evidence") or {})
    performance = dict(state.get("available_performance_metrics") or {})
    safety = {
        "paper_only": True,
        "wallet": False,
        "api_key": False,
        "private_key": False,
        "live_trading": False,
        "order_placement": False,
        "signing": False,
        "deployment": False,
        "live_money_deployment": False,
    }
    return {
        "schema_version": ITERATION_SCHEMA_VERSION,
        "id": iteration_id or _safe_iteration_id(ts, seq),
        "timestamp": ts,
        "persisted": persisted,
        "status": state.get("status") or "unknown",
        "approval_status": post_labels.get("status") or "not-approved",
        "approval": {
            "proposal_only": True,
            "promotion": state.get("promotion") or "propose_only",
            "approved_for_paper_forward_test": bool(post_labels.get("approved_for_paper_forward_test")),
            "live_trading_approval": False,
            "order_placement": False,
            "live_money_deployment": False,
        },
        "evidence_counts": evidence,
        "gates": list(state.get("gates") or []),
        "target_metrics": dict(state.get("target_metrics") or {}),
        "current_tunables": dict(state.get("current_tunables") or {}),
        "proposed_tunables": dict(state.get("proposed_tunables") or {}),
        "available_performance_metrics": performance,
        "labeling": {
            "labels_available": int(evidence.get("labeled_rows") or 0) > 0,
            "labeled_rows": int(evidence.get("labeled_rows") or 0),
            "paper_settlements": int(performance.get("paper_settlements") or 0),
            "settled_positions": int(performance.get("settled_positions") or 0),
            "open_positions": int(performance.get("open_positions") or 0),
        },
        "blocked_reasons": list(state.get("blocked_reasons") or []),
        "guardrails_ok": bool(state.get("guardrails_ok")),
        "safety": safety,
        "safety_ok": True,
    }


def record_tuning_iteration(
    state: dict[str, Any],
    path: str = TUNING_ITERATIONS_PATH,
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    sequence = _existing_iteration_count(path) + 1
    record = build_tuning_iteration_record(state, timestamp=timestamp, sequence=sequence, persisted=True)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        f.write("\n")
    return record


def iteration_safety_ok(record: dict[str, Any]) -> bool:
    safety = record.get("safety") if isinstance(record.get("safety"), dict) else {}
    approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
    return (
        safety.get("paper_only") is True
        and safety.get("wallet") is False
        and safety.get("api_key") is False
        and safety.get("private_key") is False
        and safety.get("live_trading") is False
        and safety.get("order_placement") is False
        and safety.get("signing") is False
        and safety.get("live_money_deployment") is False
        and approval.get("live_trading_approval", False) is False
        and approval.get("order_placement", False) is False
        and approval.get("live_money_deployment", False) is False
    )


def sanitize_iteration_record(record: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(record)
    ok = iteration_safety_ok(record)
    sanitized["safety_ok"] = ok
    sanitized["safety"] = {
        "paper_only": True,
        "wallet": False,
        "api_key": False,
        "private_key": False,
        "live_trading": False,
        "order_placement": False,
        "signing": False,
        "deployment": False,
        "live_money_deployment": False,
    }
    approval = dict(sanitized.get("approval") or {})
    approval["proposal_only"] = True
    approval["live_trading_approval"] = False
    approval["order_placement"] = False
    approval["live_money_deployment"] = False
    sanitized["approval"] = approval
    if not ok:
        sanitized["status"] = "rejected_unsafe_log_entry"
        sanitized["approval_status"] = "rejected-unsafe"
    return sanitized


def load_tuning_iterations(path: str = TUNING_ITERATIONS_PATH, limit: int = 25) -> list[dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(sanitize_iteration_record(record))
    if limit > 0:
        return records[-limit:]
    return records
