#!/usr/bin/env python3
"""No-lookahead feature engineering for paper weather research."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FEATURE_SCHEMA_VERSION = 2

FEATURE_FAMILIES: dict[str, str] = {
    "settlement_source": "Settlement source/station confidence",
    "forecast_ensemble": "Forecast ensemble/spread/bias placeholders",
    "live_observation": "Live observation/touched/impossible state",
    "local_time": "Station-local day/time remaining",
    "microstructure": "Order book liquidity/spread/depth",
    "ladder_consistency": "Cross-outcome ladder consistency",
    "source_quality": "Source quality and missingness",
    "portfolio_risk": "Portfolio and risk context",
    "event_model": "Event key, latent final-high distribution, and payout mapping",
    "no_lookahead": "Label separation and timestamp gate",
}


TIMESTAMP_KEYS = (
    "created_at",
    "fetched_at",
    "observed_at",
    "captured_at",
    "report_time_utc",
    "forecast_run_time_utc",
    "source_timestamp",
)


LABEL_LIKE_KEYS = {
    "label_status",
    "label_value",
    "label_source",
    "labeled_at",
    "final_outcome",
    "settlement_value",
    "resolved_at",
    "payout",
}


def parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(text[:19] + "+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_or_none(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(amount):
        return None
    return amount


def to_bool_int(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 1 if bool(value) else 0


def stable_hash(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def public_record(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    return {k: v for k, v in dict(record).items() if k not in LABEL_LIKE_KEYS}


def record_time(record: dict[str, Any] | None) -> dt.datetime | None:
    if not record:
        return None
    for key in TIMESTAMP_KEYS:
        parsed = parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def available_record(
    record: dict[str, Any] | None,
    decision_time: dt.datetime,
    excluded: list[dict[str, Any]],
    family: str,
) -> dict[str, Any]:
    record = public_record(record)
    if not record:
        return {}
    seen_at = record_time(record)
    if seen_at is not None and seen_at > decision_time:
        excluded.append(
            {
                "family": family,
                "provider": record.get("provider") or record.get("source") or record.get("execution_source"),
                "record_time": iso_or_none(seen_at),
            }
        )
        return {}
    return record


def minutes_between(start: dt.datetime | None, end: dt.datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def local_time_features(decision_time: dt.datetime, target_date: str | None, timezone_name: str | None) -> dict[str, Any]:
    zone = dt.timezone.utc
    timezone_confidence = "utc_fallback"
    if timezone_name:
        try:
            zone = ZoneInfo(timezone_name)
            timezone_confidence = "configured"
        except ZoneInfoNotFoundError:
            timezone_confidence = "invalid_timezone"
    local_dt = decision_time.astimezone(zone)
    local_midnight = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_midnight + dt.timedelta(days=1)
    typical_peak = local_midnight + dt.timedelta(hours=16)
    return {
        "market_date_local": target_date,
        "decision_time_local": local_dt.replace(microsecond=0).isoformat(),
        "minutes_since_local_midnight": max(0.0, (local_dt - local_midnight).total_seconds() / 60.0),
        "minutes_until_local_end_of_day": max(0.0, (local_end - local_dt).total_seconds() / 60.0),
        "minutes_until_typical_peak": (typical_peak - local_dt).total_seconds() / 60.0,
        "local_hour": local_dt.hour,
        "local_weekday": local_dt.weekday(),
        "day_of_year": local_dt.timetuple().tm_yday,
        "month": local_dt.month,
        "late_day_flag": to_bool_int(local_dt.hour >= 17),
        "post_peak_flag": to_bool_int(local_dt >= typical_peak),
        "peak_heating_window_flag": to_bool_int(12 <= local_dt.hour <= 17),
        "timezone_confidence": timezone_confidence,
    }


def threshold_features(forecast_high: float | None, observed_high: float | None, bucket_lo: float | None, bucket_hi: float | None, bucket_state: str | None) -> dict[str, Any]:
    threshold = bucket_lo if bucket_lo is not None and (bucket_hi is None or math.isinf(bucket_hi)) else bucket_hi
    remaining_margin = None
    if observed_high is not None and threshold is not None:
        remaining_margin = threshold - observed_high
    forecast_margin = None
    if forecast_high is not None and threshold is not None:
        forecast_margin = forecast_high - threshold
    touched = bool(bucket_state in {"already_won", "already_touched"} or (bucket_hi is not None and math.isinf(bucket_hi) and observed_high is not None and bucket_lo is not None and observed_high >= bucket_lo))
    impossible = bool(bucket_state in {"already_lost", "impossible_now"})
    return {
        "forecast_vs_threshold_f": forecast_margin,
        "threshold_remaining_margin_f": remaining_margin,
        "threshold_touched_flag": to_bool_int(touched),
        "impossible_now_flag": to_bool_int(impossible),
    }


def missing_mask(groups: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    mask: dict[str, list[str]] = {}
    for family, values in groups.items():
        mask[family] = sorted(k for k, v in values.items() if v in (None, "", [], {}))
    return mask


def quality_score(mask: dict[str, list[str]], groups: dict[str, dict[str, Any]]) -> float:
    total = 0
    missing = 0
    for family, values in groups.items():
        if family == "no_lookahead":
            continue
        total += len(values)
        missing += len(mask.get(family, []))
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - missing / total))


def build_decision_features(
    *,
    decision_time: str,
    market: dict[str, Any] | None = None,
    forecast: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    orderbook: dict[str, Any] | None = None,
    source_records: list[dict[str, Any]] | None = None,
    ladder: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    tunables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a flat, JSON-safe feature dictionary.

    Records timestamped after ``decision_time`` are excluded from value
    features and counted under ``excluded_future_source_count``. Label/final
    outcome fields are stripped from all input records.
    """
    decision_dt = parse_time(decision_time) or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    excluded: list[dict[str, Any]] = []
    market = available_record(market, decision_dt, excluded, "market")
    forecast = available_record(forecast, decision_dt, excluded, "forecast")
    observation = available_record(observation, decision_dt, excluded, "observation")
    orderbook = available_record(orderbook, decision_dt, excluded, "orderbook")
    ladder = available_record(ladder, decision_dt, excluded, "ladder")
    portfolio = available_record(portfolio, decision_dt, excluded, "portfolio")
    tunables = public_record(tunables)

    source_records = [
        available_record(record, decision_dt, excluded, "source_record")
        for record in (source_records or [])
    ]
    source_records = [record for record in source_records if record]

    bucket_lo = to_float(market.get("bucket_lo_f"))
    bucket_hi = to_float(market.get("bucket_hi_f"))
    forecast_high = to_float(forecast.get("forecast_high_f") or forecast.get("forecast_daily_high_f"))
    observed_high = to_float(observation.get("high_so_far_f") or observation.get("observed_high_so_far_f"))
    current_temp = to_float(observation.get("current_temp_f"))
    bid = to_float(orderbook.get("bid") or orderbook.get("best_bid"))
    ask = to_float(orderbook.get("ask") or orderbook.get("best_ask"))
    spread = to_float(orderbook.get("spread"))
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    depth = to_float(orderbook.get("depth") or orderbook.get("depth_at_ask"))
    entry_price = to_float(orderbook.get("entry_price"))
    station_confidence = market.get("source_confidence") or market.get("station_confidence")

    settlement = {
        "settlement_source_type": market.get("source_host") or market.get("source_type") or "unknown",
        "settlement_source_url_hash": stable_hash(market.get("source_url")),
        "settlement_station_id_normalized": str(market.get("station_id") or "").upper() or None,
        "station_confidence_score": {"high": 1.0, "medium": 0.6, "low": 0.25}.get(str(station_confidence or "").lower(), 0.0),
        "station_confidence_reason": market.get("station_confidence_reason") or station_confidence or "unknown",
        "settlement_rule_version_hash": stable_hash(" ".join(str(market.get(k) or "") for k in ("rules_text", "resolution_text", "source_text"))),
        "threshold_low_f": bucket_lo,
        "threshold_high_f": bucket_hi,
        "bucket_width_f": None if bucket_lo is None or bucket_hi is None or math.isinf(bucket_lo) or math.isinf(bucket_hi) else bucket_hi - bucket_lo,
        "ambiguous_rule_flag": to_bool_int(market.get("eligibility_class") in {"ambiguous_resolution", "unclear_source", "unclear_station"}),
        "multi_station_rule_flag": to_bool_int("multiple station" in str(market.get("rules_text") or "").lower()),
        "source_unavailable_flag": to_bool_int(not market.get("source_url") and not market.get("station_id")),
    }

    forecast_group = {
        "forecast_provider": forecast.get("provider") or "open_meteo",
        "forecast_model": forecast.get("forecast_model"),
        "forecast_run_time_utc": forecast.get("forecast_run_time_utc"),
        "forecast_age_hours": None,
        "forecast_daily_high_f": forecast_high,
        "forecast_hourly_max_f": forecast.get("forecast_hourly_max_f"),
        "ensemble_high_mean_f": forecast.get("ensemble_high_mean_f") or forecast_high,
        "ensemble_high_min_f": forecast.get("ensemble_high_min_f") or forecast.get("forecast_high_min_f"),
        "ensemble_high_max_f": forecast.get("ensemble_high_max_f") or forecast.get("forecast_high_max_f"),
        "ensemble_high_std_f": forecast.get("ensemble_high_std_f"),
        "model_disagreement_f": forecast.get("model_disagreement_f") or forecast.get("model_spread_f"),
        "provider_spread_f": forecast.get("provider_spread_f"),
        "forecast_delta_prev_run_f": forecast.get("forecast_delta_prev_run_f"),
        "climatology_percentile": forecast.get("climatology_percentile"),
        "bias_corrected_high_f": forecast.get("bias_corrected_high_f"),
    }
    fetched_at = parse_time(forecast.get("fetched_at"))
    forecast_group["forecast_age_hours"] = None if fetched_at is None else max(0.0, (decision_dt - fetched_at).total_seconds() / 3600.0)
    forecast_group.update(threshold_features(forecast_high, observed_high, bucket_lo, bucket_hi, market.get("bucket_state")))

    obs_time = parse_time(observation.get("observed_at") or observation.get("obs_time_utc"))
    live_observation = {
        "obs_provider": observation.get("source") or observation.get("provider"),
        "obs_station_id": observation.get("station_id") or settlement["settlement_station_id_normalized"],
        "obs_time_utc": iso_or_none(obs_time),
        "obs_age_minutes": minutes_between(obs_time, decision_dt),
        "current_temp_f": current_temp,
        "observed_high_so_far_f": observed_high,
        "threshold_touched_flag": forecast_group["threshold_touched_flag"],
        "threshold_remaining_margin_f": forecast_group["threshold_remaining_margin_f"],
        "impossible_now_flag": forecast_group["impossible_now_flag"],
        "obs_count_today": observation.get("obs_count_today"),
        "obs_gap_minutes_max": observation.get("obs_gap_minutes_max"),
        "obs_missing_rate_today": observation.get("obs_missing_rate_today"),
        "nearby_station_touched_count": observation.get("nearby_station_touched_count"),
    }

    time_group = local_time_features(decision_dt, market.get("target_date"), market.get("timezone") or observation.get("timezone"))

    microstructure = {
        "best_bid": bid,
        "best_ask": ask,
        "mid_price": midpoint,
        "displayed_price": to_float(orderbook.get("displayed_price")),
        "last_trade_price": to_float(orderbook.get("last_trade_price")),
        "spread": spread,
        "spread_pct_mid": None if midpoint in (None, 0.0) or spread is None else spread / midpoint,
        "mid_vs_displayed_gap": None,
        "ask_depth_to_5_shares": orderbook.get("ask_depth_to_5_shares") or depth,
        "ask_depth_to_10_shares": orderbook.get("ask_depth_to_10_shares"),
        "bid_depth_to_5_shares": orderbook.get("bid_depth_to_5_shares"),
        "paper_size_shares": tunables.get("paper_size_shares"),
        "paper_fill_price": entry_price,
        "paper_fill_shares": orderbook.get("fill_shares") or depth,
        "paper_slippage": None if entry_price is None or ask is None else entry_price - ask,
        "paper_fill_source": orderbook.get("execution_source"),
        "quote_age_seconds": to_float(orderbook.get("quote_age_seconds")),
        "depth_sufficient_flag": to_bool_int(orderbook.get("depth_sufficient")),
        "stale_book_flag": to_bool_int(orderbook.get("stale_book_flag")),
        "wide_spread_flag": to_bool_int(spread is not None and spread > float(tunables.get("max_spread") or 0.12)),
        "thin_market_flag": to_bool_int(depth is not None and depth < float(tunables.get("min_fill_shares") or 1.0)),
        "executable_edge_after_slippage": market.get("edge"),
    }
    if midpoint is not None and microstructure["displayed_price"] is not None:
        microstructure["mid_vs_displayed_gap"] = midpoint - float(microstructure["displayed_price"])

    ladder_group = {
        "ladder_group_key": ladder.get("ladder_group_key") or "|".join(str(market.get(k) or "") for k in ("city", "target_date", "station_id")),
        "threshold_rank": ladder.get("threshold_rank"),
        "num_thresholds_in_ladder": ladder.get("num_thresholds_in_ladder"),
        "adjacent_threshold_gap_f": ladder.get("adjacent_threshold_gap_f"),
        "market_prob_monotonicity_violation_flag": to_bool_int(ladder.get("market_prob_monotonicity_violation_flag")),
        "model_prob_monotonicity_violation_flag": to_bool_int(ladder.get("model_prob_monotonicity_violation_flag")),
        "bucket_sum_implied_probability": ladder.get("bucket_sum_implied_probability"),
        "bucket_sum_deviation_from_one": ladder.get("bucket_sum_deviation_from_one"),
        "contradictory_signal_flag": to_bool_int(ladder.get("contradictory_signal_flag")),
        "ladder_diagnostic": ladder.get("ladder_diagnostic"),
    }

    portfolio_group = {
        "paper_cash": portfolio.get("cash"),
        "paper_equity": portfolio.get("equity"),
        "open_position_count": portfolio.get("open_position_count") or portfolio.get("unresolved_positions"),
        "open_exposure_pct": portfolio.get("open_exposure_pct"),
        "position_pct_equity": portfolio.get("position_pct_equity"),
        "city_date_exposure_pct": portfolio.get("city_date_exposure_pct"),
        "station_date_exposure_pct": portfolio.get("station_date_exposure_pct"),
        "same_ladder_exposure_pct": portfolio.get("same_ladder_exposure_pct"),
        "max_correlated_loss_pct": portfolio.get("max_correlated_loss_pct"),
        "drawdown_pct": portfolio.get("drawdown") or portfolio.get("drawdown_pct"),
    }

    event_group = {
        "event_key": market.get("event_key"),
        "strategy_family": market.get("strategy_family"),
        "contract_type": market.get("contract_type"),
        "settlement_state": market.get("settlement_state"),
        "latent_final_high_mean_f": forecast.get("latent_final_high_mean_f") or forecast_high,
        "latent_final_high_sigma_f": forecast.get("latent_final_high_sigma_f") or forecast.get("ensemble_high_std_f"),
        "contract_payout_mapping_hash": stable_hash(market.get("payout_mapping_json") or market.get("payout_mapping")),
    }

    source_statuses = [record.get("status") for record in source_records if record.get("status")]
    source_quality = {
        "source_fetch_status": observation.get("raw_status") or forecast.get("raw_status") or orderbook.get("raw_status"),
        "source_snapshot_age_seconds": None,
        "source_timeout_flag": to_bool_int(any(status == "timeout" for status in source_statuses)),
        "source_parse_error_flag": to_bool_int(any(status in {"parse_error", "error"} for status in source_statuses)),
        "fallback_source_used_flag": to_bool_int(market.get("station_source") == "station_registry"),
        "fallback_source_type": market.get("station_source"),
        "source_record_count": len(source_records),
        "source_provider_list": sorted({str(record.get("provider")) for record in source_records if record.get("provider")}),
        "source_disagreement_score": forecast.get("source_disagreement_score"),
    }

    groups = {
        "settlement_source": settlement,
        "forecast_ensemble": forecast_group,
        "live_observation": live_observation,
        "local_time": time_group,
        "microstructure": microstructure,
        "ladder_consistency": ladder_group,
        "portfolio_risk": portfolio_group,
        "event_model": event_group,
        "source_quality": source_quality,
    }
    mask = missing_mask(groups)
    source_quality["field_missing_mask"] = mask
    source_quality["source_quality_score"] = quality_score(mask, groups)
    source_quality["decision_features_complete_flag"] = to_bool_int(not excluded and all(len(mask.get(family, [])) == 0 for family in ("settlement_source", "forecast_ensemble", "microstructure")))

    flat: dict[str, Any] = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "decision_time_utc": iso_or_none(decision_dt),
        "paper_only": True,
        "no_lookahead_enforced": True,
        "excluded_future_source_count": len(excluded),
        "excluded_future_sources": excluded,
        "feature_families": sorted(FEATURE_FAMILIES),
    }
    for values in groups.values():
        flat.update(values)
    flat["field_missing_mask_json"] = json.dumps(mask, sort_keys=True, ensure_ascii=True)
    return flat


def family_coverage(features: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return simple per-family coverage from a built feature dictionary."""
    coverage: dict[str, dict[str, Any]] = {}
    missing = {}
    try:
        missing = json.loads(features.get("field_missing_mask_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        missing = {}
    for key, label in FEATURE_FAMILIES.items():
        if key == "no_lookahead":
            coverage[key] = {
                "label": label,
                "present": bool(features.get("no_lookahead_enforced") and not features.get("excluded_future_source_count")),
                "missing_fields": [],
            }
            continue
        missing_fields = list(missing.get(key, []))
        coverage[key] = {
            "label": label,
            "present": len(missing_fields) == 0,
            "missing_fields": missing_fields,
        }
    return coverage
