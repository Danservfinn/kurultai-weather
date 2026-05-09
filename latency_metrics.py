#!/usr/bin/env python3
"""Latency half-life metrics for observed-high threshold touches."""

from __future__ import annotations

import sqlite3
from typing import Any


DELAY_BUCKETS = (
    ("0-30s", 0, 30),
    ("30-60s", 30, 60),
    ("1-2m", 60, 120),
    ("2-5m", 120, 300),
    ("5-15m", 300, 900),
    ("15m+", 900, None),
)


def delay_bucket(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    for label, lo, hi in DELAY_BUCKETS:
        if value >= lo and (hi is None or value < hi):
            return label
    return "unknown"


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _first_time_to(rows: list[sqlite3.Row], threshold: float) -> float | None:
    times = [
        float(row["seconds_after_touch"])
        for row in rows
        if row["seconds_after_touch"] is not None and row["best_ask"] is not None and float(row["best_ask"]) >= threshold
    ]
    return min(times) if times else None


def aggregate_latency_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    if not db.execute("select 1 from sqlite_master where type='table' and name='threshold_touch_events'").fetchone():
        return empty_latency_metrics()
    touches = db.execute("select * from threshold_touch_events order by id").fetchall()
    if not touches:
        return empty_latency_metrics()
    snapshots: list[sqlite3.Row] = []
    if db.execute("select 1 from sqlite_master where type='table' and name='post_touch_repricing'").fetchone():
        snapshots = db.execute("select * from post_touch_repricing order by touch_event_id, seconds_after_touch").fetchall()
    by_touch: dict[int, list[sqlite3.Row]] = {}
    for row in snapshots:
        if row["touch_event_id"] is not None:
            by_touch.setdefault(int(row["touch_event_id"]), []).append(row)

    bucket_counts: dict[str, int] = {label: 0 for label, _, _ in DELAY_BUCKETS}
    bucket_counts["unknown"] = 0
    for touch in touches:
        bucket_counts[delay_bucket(touch["detection_delay_seconds"])] += 1

    asks = [float(r["best_ask"]) for r in snapshots if r["best_ask"] is not None]
    spreads = [float(r["spread"]) for r in snapshots if r["spread"] is not None]
    ages = [float(r["book_age_seconds"]) for r in snapshots if r["book_age_seconds"] is not None]
    depth_flags = [1.0 if (r["ask_depth"] is not None and float(r["ask_depth"]) > 0.0) else 0.0 for r in snapshots]
    time95 = [_first_time_to(rows, 0.95) for rows in by_touch.values()]
    time98 = [_first_time_to(rows, 0.98) for rows in by_touch.values()]

    return {
        "signals": len(touches),
        "snapshots": len(snapshots),
        "delay_buckets": bucket_counts,
        "avg_ask": _avg(asks),
        "avg_spread": _avg(spreads),
        "avg_quote_age_seconds": _avg(ages),
        "depth_sufficient_pct": _avg(depth_flags),
        "seconds_to_ask_95": _avg([v for v in time95 if v is not None]),
        "seconds_to_ask_98": _avg([v for v in time98 if v is not None]),
    }


def empty_latency_metrics() -> dict[str, Any]:
    return {
        "signals": 0,
        "snapshots": 0,
        "delay_buckets": {label: 0 for label, _, _ in DELAY_BUCKETS} | {"unknown": 0},
        "avg_ask": None,
        "avg_spread": None,
        "avg_quote_age_seconds": None,
        "depth_sufficient_pct": None,
        "seconds_to_ask_95": None,
        "seconds_to_ask_98": None,
    }
