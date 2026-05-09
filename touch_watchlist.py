#!/usr/bin/env python3
"""Hot threshold-touch watchlist helpers for paper-only latency research."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _bool_feature(data: dict[str, Any], name: str) -> bool:
    return bool(data.get(name))


def compute_hotness_score(
    candidate: dict[str, Any],
    observation: dict[str, Any] | None,
    quote: dict[str, Any] | None,
    local_features: dict[str, Any] | None,
) -> dict[str, Any]:
    """Score whether a threshold contract should be watched at high cadence."""
    observation = observation or {}
    quote = quote or {}
    local_features = local_features or {}
    threshold = _float(candidate.get("threshold_f"))
    high = _float(observation.get("high_so_far_f", candidate.get("current_high_f")))
    distance = None if threshold is None or high is None else threshold - high
    abs_distance = abs(distance) if distance is not None else None

    reasons: list[str] = []
    score = 0.0
    if abs_distance is not None and abs_distance <= 1.0:
        score += 0.35
        reasons.append("within_1f_of_threshold")
    elif abs_distance is not None and abs_distance <= 2.0:
        score += 0.18
        reasons.append("within_2f_of_threshold")

    local_hour = local_features.get("local_hour")
    try:
        hour = int(local_hour)
    except (TypeError, ValueError):
        hour = None
    if hour is not None and 11 <= hour <= 17:
        score += 0.20
        reasons.append("peak_heating_window")
    elif hour is not None and 17 < hour <= 20 and abs_distance is not None and abs_distance <= 2.0:
        score += 0.12
        reasons.append("post_peak_near_threshold")

    if _bool_feature(local_features, "current_temp_rising") or _bool_feature(observation, "current_temp_rising"):
        score += 0.15
        reasons.append("current_temp_rising")

    ask = _float(quote.get("ask", quote.get("best_ask")))
    if ask is not None and ask < 0.95:
        score += 0.15
        reasons.append("market_underprices_absorbing_risk")

    source_conf = str(candidate.get("source_confidence") or observation.get("confidence_class") or "").lower()
    if source_conf in {"high", "direct_settlement_source", "official_proxy_source", "clean_station"}:
        score += 0.10
        reasons.append("clean_station_source")

    depth = _float(quote.get("depth", quote.get("ask_depth")))
    if quote.get("depth_sufficient") or (depth is not None and depth > 0.0):
        score += 0.05
        reasons.append("sufficient_clob_depth")

    activate = score >= 0.70 or (abs_distance is not None and abs_distance <= 1.0)
    return {
        "hotness_score": min(1.0, score),
        "hot_reason": ",".join(reasons) if reasons else "no_hot_features",
        "active": 1 if activate else 0,
        "current_high_f": high,
        "distance_to_threshold_f": distance,
        "local_hour": hour,
        "last_seen_ask": ask,
        "last_seen_bid": _float(quote.get("bid", quote.get("best_bid"))),
        "last_seen_depth": depth,
    }


def upsert_touch_watchlist(db: sqlite3.Connection, candidate: dict[str, Any], hotness: dict[str, Any]) -> None:
    now = utc_now_iso()
    db.execute(
        """
        insert into touch_watchlist(
          event_key, market_id, token_id, city, station_id, target_date,
          strategy_family, contract_type, threshold_f, side, current_high_f,
          distance_to_threshold_f, local_hour, hotness_score, hot_reason,
          watch_started_at, last_source_poll_at, last_book_update_at,
          last_seen_ask, last_seen_bid, last_seen_depth, active
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(event_key, market_id, token_id) do update set
          city=excluded.city,
          station_id=excluded.station_id,
          target_date=excluded.target_date,
          strategy_family=excluded.strategy_family,
          contract_type=excluded.contract_type,
          threshold_f=excluded.threshold_f,
          side=excluded.side,
          current_high_f=excluded.current_high_f,
          distance_to_threshold_f=excluded.distance_to_threshold_f,
          local_hour=excluded.local_hour,
          hotness_score=excluded.hotness_score,
          hot_reason=excluded.hot_reason,
          last_source_poll_at=excluded.last_source_poll_at,
          last_book_update_at=excluded.last_book_update_at,
          last_seen_ask=excluded.last_seen_ask,
          last_seen_bid=excluded.last_seen_bid,
          last_seen_depth=excluded.last_seen_depth,
          active=excluded.active
        """,
        (
            candidate.get("event_key"),
            candidate.get("market_id"),
            candidate.get("token_id"),
            candidate.get("city"),
            candidate.get("station_id"),
            candidate.get("target_date"),
            candidate.get("strategy_family", "latency_absorbing_state"),
            candidate.get("contract_type", "threshold"),
            candidate.get("threshold_f"),
            candidate.get("side", "yes"),
            hotness.get("current_high_f"),
            hotness.get("distance_to_threshold_f"),
            hotness.get("local_hour"),
            hotness.get("hotness_score"),
            hotness.get("hot_reason"),
            candidate.get("watch_started_at") or now,
            now,
            candidate.get("last_book_update_at"),
            hotness.get("last_seen_ask"),
            hotness.get("last_seen_bid"),
            hotness.get("last_seen_depth"),
            int(hotness.get("active") or 0),
        ),
    )


def deactivate_stale_watchlist_rows(db: sqlite3.Connection, now: str | None = None) -> int:
    """Deactivate rows for target dates before the UTC date containing ``now``."""
    as_of = dt.datetime.fromisoformat((now or utc_now_iso()).replace("Z", "+00:00"))
    today = as_of.date().isoformat()
    cur = db.execute(
        "update touch_watchlist set active=0 where active=1 and target_date is not null and target_date < ?",
        (today,),
    )
    return int(cur.rowcount or 0)


def hotness_json(hotness: dict[str, Any]) -> str:
    return json.dumps(hotness, sort_keys=True, separators=(",", ":"))
