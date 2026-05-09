#!/usr/bin/env python3
"""Render a paper-only performance dashboard for the weather engine.

Reads the local SQLite research ledger and writes a self-contained static HTML
artifact into the brain project directory. This script is read-only with
respect to the database: it does not trade, sign, place orders, or mutate the
paper ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import sqlite3
from collections.abc import Sequence
from typing import Any

import edge_validation
import latency_metrics
import tuning_evaluator


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "paper_weather.sqlite3")
DEFAULT_OUTPUT = "/Users/kublai/brain/projects/polymarket-weather-engine-performance.html"
DEFAULT_JSON_OUTPUT = "/Users/kublai/brain/projects/polymarket-weather-engine-performance.json"
DEFAULT_BANKROLL_USD = 1000.0
EVIDENCE_DAY_TARGET = 14
EVIDENCE_CANDIDATE_TARGET = 300
EVALUATION_EDGE_THRESHOLD = 0.08
EVALUATION_MIN_ENTRY = 0.02
EVALUATION_MAX_ENTRY = 0.95
SNAPSHOT_SCHEMA_VERSION = 3
POLL_INTERVAL_MS = int(float(os.environ.get("DASHBOARD_POLL_SECONDS", "30")) * 1000)


def clean_float(value: float, epsilon: float = 1e-9) -> float:
    return 0.0 if abs(value) < epsilon else value


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_money(value: Any, signed: bool = False) -> str:
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    amount = clean_float(amount)
    prefix = "+" if signed and amount > 0 else ""
    return f"{prefix}${amount:,.2f}"


def fmt_num(value: Any, digits: int = 0) -> str:
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    amount = clean_float(amount)
    if digits <= 0:
        return f"{amount:,.0f}"
    return f"{amount:,.{digits}f}"


def fmt_pct(value: Any, signed: bool = False) -> str:
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    amount = clean_float(amount)
    prefix = "+" if signed and amount > 0 else ""
    return f"{prefix}{amount:.2%}"


def fmt_price(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{clean_float(float(value)):.3f}"
    except (TypeError, ValueError):
        return "-"


def tone_for(value: float, inverse: bool = False) -> str:
    value = clean_float(value)
    if abs(value) < 1e-9:
        return "neutral"
    positive = value > 0
    if inverse:
        positive = not positive
    return "positive" if positive else "negative"


def connect(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def script_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return bool(row)


def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(db, table):
        return False
    return column in {row[1] for row in db.execute(f"pragma table_info({table})")}


def table_count(db: sqlite3.Connection, table: str) -> int:
    allowed = {
        "runs",
        "markets",
        "signals",
        "forecast_snapshots",
        "station_observations",
        "orderbook_snapshots",
        "training_rows",
        "paper_accounts",
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "paper_settlements",
        "label_attempts",
        "events",
        "event_exposure_snapshots",
        "contract_payouts",
        "lifecycle_attribution",
        "calibration_rows",
    }
    if table not in allowed:
        raise ValueError(f"unexpected table name: {table}")
    if not table_exists(db, table):
        return 0
    row = db.execute(f"select count(*) from {table}").fetchone()
    return int(row[0] or 0)


def latest_account(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute(
        """
        select id, name, starting_cash, cash, realized_pnl, created_at, updated_at
        from paper_accounts
        order by id desc
        limit 1
        """
    ).fetchone()


def portfolio_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    account = latest_account(db)
    if account is None:
        return {
            "account_id": None,
            "account_name": "default-paper",
            "starting_cash": DEFAULT_BANKROLL_USD,
            "cash": DEFAULT_BANKROLL_USD,
            "open_exposure": 0.0,
            "open_value": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "equity": DEFAULT_BANKROLL_USD,
            "return_pct": 0.0,
            "drawdown": 0.0,
            "unresolved_positions": 0,
            "updated_at": None,
        }

    account_id = int(account["id"])
    starting_cash = float(account["starting_cash"] or DEFAULT_BANKROLL_USD)
    cash = float(account["cash"] or 0.0)
    realized = float(account["realized_pnl"] or 0.0)
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
    for position in positions:
        if position["status"] != "open":
            continue
        unresolved += 1
        shares = float(position["shares"] or 0.0)
        cost_basis = float(position["cost_basis"] or 0.0)
        latest_mark = float(position["latest_mark"] or 0.0)
        open_cost += cost_basis
        open_value += shares * latest_mark

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
        "account_name": account["name"],
        "starting_cash": starting_cash,
        "cash": cash,
        "open_exposure": open_cost,
        "open_value": open_value,
        "realized_pnl": realized,
        "unrealized_pnl": clean_float(unrealized),
        "equity": equity,
        "return_pct": clean_float((equity - starting_cash) / starting_cash if starting_cash else 0.0),
        "drawdown": clean_float(drawdown),
        "unresolved_positions": unresolved,
        "updated_at": account["updated_at"],
    }


def evaluation_progress(db: sqlite3.Connection) -> dict[str, Any]:
    rows = db.execute(
        """
        select
          s.id,
          s.created_at as candidate_at,
          latest.created_at as latest_at,
          s.edge,
          coalesce(s.entry_price, s.market_prob) as entry_price
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
          and coalesce(s.entry_price, s.market_prob) <= ?
          and coalesce(s.entry_price, s.market_prob) >= ?
          and coalesce(s.signal_type, '') like 'paper_buy%'
        order by s.edge desc, s.created_at asc
        """,
        (EVALUATION_EDGE_THRESHOLD, EVALUATION_MAX_ENTRY, EVALUATION_MIN_ENTRY),
    ).fetchall()

    dates: list[dt.date] = []
    for row in rows:
        for key in ("candidate_at", "latest_at"):
            value = row[key]
            if not value:
                continue
            try:
                dates.append(dt.datetime.fromisoformat(str(value)).date())
            except ValueError:
                try:
                    dates.append(dt.date.fromisoformat(str(value)[:10]))
                except ValueError:
                    pass

    sample_days = (max(dates) - min(dates)).days + 1 if dates else 0
    mean_edge = sum(float(row["edge"] or 0.0) for row in rows) / len(rows) if rows else 0.0
    return {
        "candidate_count": len(rows),
        "sample_days": sample_days,
        "mean_edge": mean_edge,
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
        "candidate_target": EVIDENCE_CANDIDATE_TARGET,
        "day_target": EVIDENCE_DAY_TARGET,
        "ready": len(rows) >= EVIDENCE_CANDIDATE_TARGET and sample_days >= EVIDENCE_DAY_TARGET,
    }


def labeling_progress(db: sqlite3.Connection) -> dict[str, Any]:
    final_where = tuning_evaluator.FINAL_LABEL_WHERE
    training_rows = table_count(db, "training_rows")
    labeled_rows = int(db.execute(f"select count(*) from training_rows where {final_where}").fetchone()[0] or 0)
    pending_rows = int(
        db.execute(
            """
            select count(*)
            from training_rows
            where target_date is not null
              and target_date <> ''
              and target_date <= date('now')
              and coalesce(label_status,'') <> 'final'
            """
        ).fetchone()[0]
        or 0
    )
    future_rows = int(
        db.execute(
            """
            select count(*)
            from training_rows
            where target_date is not null
              and target_date <> ''
              and target_date > date('now')
              and coalesce(label_status,'') <> 'final'
            """
        ).fetchone()[0]
        or 0
    )
    open_positions = int(db.execute("select count(*) from paper_positions where status='open'").fetchone()[0] or 0)
    settled_positions = int(db.execute("select count(*) from paper_positions where status='settled'").fetchone()[0] or 0)
    settlement_rows = table_count(db, "paper_settlements")
    attempt_rows = table_count(db, "label_attempts")
    if not table_exists(db, "label_attempts"):
        return {
            "training_rows": training_rows,
            "labeled_rows": labeled_rows,
            "pending_rows": pending_rows,
            "future_rows": future_rows,
            "open_positions": open_positions,
            "settled_positions": settled_positions,
            "settlements": settlement_rows,
            "attempts": 0,
            "attempt_status_counts": {},
            "source_coverage": [],
            "recent_attempts": [],
            "blockers": [{"reason": "label_attempts table has not been created yet", "count": pending_rows}] if pending_rows else [],
        }
    attempt_status_counts = {
        str(row["outcome_status"] or "unknown"): int(row["count"] or 0)
        for row in db.execute(
            """
            select coalesce(outcome_status, 'unknown') as outcome_status, count(*) as count
            from label_attempts
            group by coalesce(outcome_status, 'unknown')
            """
        ).fetchall()
    }
    source_coverage = [
        row_dict(row)
        for row in db.execute(
            """
            select
              coalesce(source_provider, 'unknown') as source_provider,
              coalesce(outcome_status, 'unknown') as outcome_status,
              count(*) as count,
              max(attempted_at) as latest_attempt
            from label_attempts
            group by coalesce(source_provider, 'unknown'), coalesce(outcome_status, 'unknown')
            order by count desc, source_provider
            limit 12
            """
        ).fetchall()
    ]
    recent_attempts = [
        row_dict(row)
        for row in db.execute(
            """
            select
              id, attempted_at, source_provider, source_status, outcome_status,
              station_id, final_high_f, label_value, reason, title, outcome
            from label_attempts
            order by id desc
            limit 10
            """
        ).fetchall()
    ]
    blockers = [
        row_dict(row)
        for row in db.execute(
            """
            select coalesce(reason, outcome_status, 'unknown') as reason, count(*) as count
            from label_attempts
            where outcome_status in ('pending', 'skipped', 'error')
            group by coalesce(reason, outcome_status, 'unknown')
            order by count desc, reason
            limit 8
            """
        ).fetchall()
    ]
    if not blockers and pending_rows:
        blockers = [{"reason": "No delayed label attempts recorded for pending rows", "count": pending_rows}]

    return {
        "training_rows": training_rows,
        "labeled_rows": labeled_rows,
        "pending_rows": pending_rows,
        "future_rows": future_rows,
        "open_positions": open_positions,
        "settled_positions": settled_positions,
        "settlements": settlement_rows,
        "attempts": attempt_rows,
        "attempt_status_counts": attempt_status_counts,
        "source_coverage": source_coverage,
        "recent_attempts": recent_attempts,
        "blockers": blockers,
    }


def _bucket_edge(edge: Any) -> str:
    if edge is None:
        return "unknown"
    value = float(edge)
    if value < 0:
        return "negative"
    if value < 0.05:
        return "0-5pct"
    if value < 0.10:
        return "5-10pct"
    return "10pct_plus"


def _delay_bucket(days: float) -> str:
    if days <= 1:
        return "0-1d"
    if days <= 3:
        return "2-3d"
    if days <= 7:
        return "4-7d"
    return "8d_plus"


def _close_bucket(minutes: Any) -> str:
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        return "unknown"
    if value <= 60:
        return "0-1h"
    if value <= 360:
        return "1-6h"
    if value <= 720:
        return "6-12h"
    return "12h_plus"


def research_metrics(db: sqlite3.Connection) -> dict[str, Any]:
    strategy_pnl: list[dict[str, Any]] = []
    if table_exists(db, "paper_positions"):
        strategy_pnl = [
            row_dict(row)
            for row in db.execute(
                """
                select coalesce(strategy_family, 'unknown') as strategy_family,
                       count(*) as positions,
                       sum(cost_basis) as cost_basis,
                       sum(case when status='settled' then realized_pnl else (shares * coalesce(latest_mark, 0)) - cost_basis end) as pnl
                from paper_positions
                group by coalesce(strategy_family, 'unknown')
                order by abs(coalesce(pnl, 0)) desc, strategy_family
                """
            ).fetchall()
        ]

    calibration_by_contract: list[dict[str, Any]] = []
    if table_exists(db, "calibration_rows"):
        calibration_by_contract = [
            row_dict(row)
            for row in db.execute(
                """
                select coalesce(contract_type, 'unknown') as contract_type,
                       coalesce(strategy_family, 'unknown') as strategy_family,
                       count(*) as rows,
                       avg(brier) as brier,
                       avg(log_loss) as log_loss
                from calibration_rows
                group by coalesce(contract_type, 'unknown'), coalesce(strategy_family, 'unknown')
                order by rows desc, contract_type, strategy_family
                limit 24
                """
            ).fetchall()
        ]

    edge_buckets: dict[str, dict[str, Any]] = {}
    if table_exists(db, "training_rows"):
        rows = db.execute(
            """
            select edge, model_prob, label_value
            from training_rows
            where edge is not null
            """
        ).fetchall()
        for edge, model_prob, label_value in rows:
            bucket = _bucket_edge(edge)
            item = edge_buckets.setdefault(bucket, {"bucket": bucket, "rows": 0, "labeled_rows": 0, "brier": 0.0, "log_loss": 0.0})
            item["rows"] += 1
            if model_prob is not None and label_value in (0, 1, 0.0, 1.0):
                prob = max(1e-6, min(1.0 - 1e-6, float(model_prob)))
                label = float(label_value)
                item["labeled_rows"] += 1
                item["brier"] += (prob - label) * (prob - label)
                item["log_loss"] += -(label * math.log(prob) + (1.0 - label) * math.log(1.0 - prob))
        for item in edge_buckets.values():
            labeled = int(item["labeled_rows"] or 0)
            if labeled:
                item["brier"] = item["brier"] / labeled
                item["log_loss"] = item["log_loss"] / labeled
            else:
                item["brier"] = None
                item["log_loss"] = None

    fill_realism = {
        "orders": table_count(db, "paper_orders"),
        "fills": table_count(db, "paper_fills"),
        "fill_rate": None,
        "clob_fills": 0,
        "displayed_price_fills": 0,
        "avg_slippage": None,
        "skipped_depth_orders": 0,
    }
    if table_exists(db, "paper_orders"):
        orders = fill_realism["orders"]
        fills = fill_realism["fills"]
        fill_realism["fill_rate"] = None if not orders else fills / orders
        fill_realism["skipped_depth_orders"] = int(
            db.execute("select count(*) from paper_orders where reason like '%depth%' or reason like '%min_fill%'").fetchone()[0] or 0
        )
    if table_exists(db, "paper_fills"):
        row = db.execute(
            "select sum(case when source='clob_book' then 1 else 0 end), sum(case when source='displayed_price' then 1 else 0 end), avg(slippage) from paper_fills"
        ).fetchone()
        fill_realism["clob_fills"] = int(row[0] or 0)
        fill_realism["displayed_price_fills"] = int(row[1] or 0)
        fill_realism["avg_slippage"] = float(row[2]) if row and row[2] is not None else None

    label_delay_histogram: dict[str, int] = {}
    if table_exists(db, "training_rows"):
        rows = db.execute(
            """
            select target_date, labeled_at
            from training_rows
            where target_date is not null and labeled_at is not null and label_status='final'
            """
        ).fetchall()
        for target_date, labeled_at in rows:
            try:
                target_end = dt.datetime.fromisoformat(str(target_date)[:10] + "T23:59:59+00:00")
                labeled = dt.datetime.fromisoformat(str(labeled_at))
                if labeled.tzinfo is None:
                    labeled = labeled.replace(tzinfo=dt.timezone.utc)
                days = max(0.0, (labeled.astimezone(dt.timezone.utc) - target_end).total_seconds() / 86400.0)
                label_delay_histogram[_delay_bucket(days)] = label_delay_histogram.get(_delay_bucket(days), 0) + 1
            except ValueError:
                label_delay_histogram["unknown"] = label_delay_histogram.get("unknown", 0) + 1

    reaction_lag = {"avg_quote_age_seconds": None, "stale_quote_rows": 0, "rows": 0}
    if table_exists(db, "signals") and column_exists(db, "signals", "quote_age_seconds"):
        row = db.execute(
            "select avg(quote_age_seconds), sum(coalesce(stale_book_flag,0)), count(*) from signals where quote_age_seconds is not null"
        ).fetchone()
        reaction_lag = {
            "avg_quote_age_seconds": float(row[0]) if row and row[0] is not None else None,
            "stale_quote_rows": int(row[1] or 0) if row else 0,
            "rows": int(row[2] or 0) if row else 0,
        }

    ladder_violations: list[dict[str, Any]] = []
    if table_exists(db, "signals") and column_exists(db, "signals", "ladder_violation_type"):
        ladder_violations = [
            row_dict(row)
            for row in db.execute(
                """
                select coalesce(ladder_violation_type, 'none') as violation, count(*) as rows
                from signals
                group by coalesce(ladder_violation_type, 'none')
                order by rows desc, violation
                limit 12
                """
            ).fetchall()
        ]

    station_source_disagreement = []
    if table_exists(db, "training_rows"):
        station_source_disagreement = [
            row_dict(row)
            for row in db.execute(
                """
                select coalesce(source_confidence, 'unknown') as source_confidence,
                       coalesce(eligibility_class, 'unknown') as eligibility_class,
                       count(*) as rows
                from training_rows
                group by coalesce(source_confidence, 'unknown'), coalesce(eligibility_class, 'unknown')
                order by rows desc
                limit 12
                """
            ).fetchall()
        ]

    time_to_local_close: dict[str, int] = {}
    if table_exists(db, "training_rows") and column_exists(db, "training_rows", "features_json"):
        for (features_json,) in db.execute("select features_json from training_rows where features_json is not null").fetchall():
            try:
                payload = json.loads(features_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            bucket = _close_bucket(payload.get("minutes_until_local_end_of_day"))
            time_to_local_close[bucket] = time_to_local_close.get(bucket, 0) + 1

    rule_ambiguity_loss = []
    if table_exists(db, "training_rows"):
        rule_ambiguity_loss = [
            row_dict(row)
            for row in db.execute(
                """
                select coalesce(eligibility_class, 'unknown') as eligibility_class,
                       count(*) as rows,
                       avg(case when label_value in (0,1) and model_prob is not null then (model_prob - label_value) * (model_prob - label_value) end) as brier
                from training_rows
                group by coalesce(eligibility_class, 'unknown')
                order by rows desc
                """
            ).fetchall()
        ]

    lifecycle_funnel = {
        "candidates": table_count(db, "lifecycle_attribution"),
        "signals": table_count(db, "signals"),
        "training_rows": table_count(db, "training_rows"),
        "orders": table_count(db, "paper_orders"),
        "fills": table_count(db, "paper_fills"),
        "labels": int(db.execute(f"select count(*) from training_rows where {tuning_evaluator.FINAL_LABEL_WHERE}").fetchone()[0] or 0) if table_exists(db, "training_rows") else 0,
        "calibration_rows": table_count(db, "calibration_rows"),
        "settlements": table_count(db, "paper_settlements"),
    }

    event_exposure_latent_summary = []
    if table_exists(db, "events"):
        event_exposure_latent_summary = [
            row_dict(row)
            for row in db.execute(
                """
                select event_key, city, target_date, station_id,
                       latent_final_high_mean_f, latent_final_high_sigma_f,
                       observed_high_f, local_day_complete, open_exposure,
                       open_position_count, contract_count
                from events
                order by open_exposure desc, last_seen desc
                limit 20
                """
            ).fetchall()
        ]

    return {
        "strategy_pnl": strategy_pnl,
        "edge_buckets": sorted(edge_buckets.values(), key=lambda item: item["bucket"]),
        "calibration_by_contract": calibration_by_contract,
        "fill_realism": fill_realism,
        "label_delay_histogram": label_delay_histogram,
        "reaction_lag_stale_quote": reaction_lag,
        "ladder_violations": ladder_violations,
        "station_source_disagreement": station_source_disagreement,
        "time_to_local_close": time_to_local_close,
        "rule_ambiguity_loss": rule_ambiguity_loss,
        "lifecycle_funnel": lifecycle_funnel,
        "event_exposure_latent_summary": event_exposure_latent_summary,
    }


def render_research_metrics_panel(metrics: dict[str, Any]) -> str:
    strategy_rows = [
        [
            escape(row.get("strategy_family")),
            f'<span class="num">{escape(fmt_num(row.get("positions")))}</span>',
            f'<span class="num">{escape(fmt_money(row.get("cost_basis")))}</span>',
            f'<span class="num {escape(tone_for(float(row.get("pnl") or 0.0)))}">{escape(fmt_money(row.get("pnl"), signed=True))}</span>',
        ]
        for row in metrics.get("strategy_pnl", [])
    ]
    calibration_rows = [
        [
            escape(row.get("contract_type")),
            escape(row.get("strategy_family")),
            f'<span class="num">{escape(fmt_num(row.get("rows")))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("brier"), 3))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("log_loss"), 3))}</span>',
        ]
        for row in metrics.get("calibration_by_contract", [])
    ]
    edge_rows = [
        [
            escape(row.get("bucket")),
            f'<span class="num">{escape(fmt_num(row.get("rows")))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("labeled_rows")))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("brier"), 3))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("log_loss"), 3))}</span>',
        ]
        for row in metrics.get("edge_buckets", [])
    ]
    ladder_rows = [
        [escape(row.get("violation")), f'<span class="num">{escape(fmt_num(row.get("rows")))}</span>']
        for row in metrics.get("ladder_violations", [])
    ]
    event_rows = [
        [
            f'<span class="title-cell">{escape(row.get("city"))}</span><br><span class="muted">{escape(row.get("target_date"))} {escape(row.get("station_id"))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("latent_final_high_mean_f"), 1))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("latent_final_high_sigma_f"), 1))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("observed_high_f"), 1))}</span>',
            f'<span class="num">{escape(fmt_money(row.get("open_exposure")))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("contract_count")))}</span>',
        ]
        for row in metrics.get("event_exposure_latent_summary", [])
    ]
    fill = metrics.get("fill_realism") or {}
    reaction = metrics.get("reaction_lag_stale_quote") or {}
    funnel = metrics.get("lifecycle_funnel") or {}
    delay = metrics.get("label_delay_histogram") or {}
    close = metrics.get("time_to_local_close") or {}
    return f"""
      <div class="panel-header">
        <div>
          <h2>Research Metrics</h2>
          <p>Calibration and Brier/log loss lead post-label evaluation; paper return stays secondary.</p>
        </div>
        {status_pill("proposal-only")}
      </div>
      <div class="tuning-grid compact">
        <div>
          <h3>Fill Realism</h3>
          <dl class="metric-list">
            <div><dt>Fill Rate</dt><dd>{escape(fmt_pct(fill.get("fill_rate")))}</dd></div>
            <div><dt>CLOB Fills</dt><dd>{escape(fmt_num(fill.get("clob_fills")))} / {escape(fmt_num(fill.get("fills")))}</dd></div>
            <div><dt>Avg Slippage</dt><dd>{escape(fmt_price(fill.get("avg_slippage")))}</dd></div>
            <div><dt>Depth Skips</dt><dd>{escape(fmt_num(fill.get("skipped_depth_orders")))}</dd></div>
          </dl>
        </div>
        <div>
          <h3>Timing</h3>
          <dl class="metric-list">
            <div><dt>Quote Age</dt><dd>{escape(fmt_num(reaction.get("avg_quote_age_seconds"), 1))}s avg</dd></div>
            <div><dt>Stale Quotes</dt><dd>{escape(fmt_num(reaction.get("stale_quote_rows")))} / {escape(fmt_num(reaction.get("rows")))}</dd></div>
            <div><dt>Label Delay</dt><dd>{escape(json.dumps(delay, sort_keys=True))}</dd></div>
            <div><dt>Local Close</dt><dd>{escape(json.dumps(close, sort_keys=True))}</dd></div>
          </dl>
        </div>
      </div>
      <div class="split">
        <div>
          <h3>Strategy PnL</h3>
          {render_table(["Strategy", "Positions", "Cost", "PnL"], strategy_rows, "No strategy PnL yet.")}
        </div>
        <div>
          <h3>Calibration By Contract</h3>
          {render_table(["Contract", "Strategy", "Rows", "Brier", "Log Loss"], calibration_rows, "No calibration rows yet.")}
        </div>
      </div>
      <div class="split">
        <div>
          <h3>Edge Buckets</h3>
          {render_table(["Bucket", "Rows", "Labels", "Brier", "Log Loss"], edge_rows, "No edge buckets yet.")}
        </div>
        <div>
          <h3>Ladder Violations</h3>
          {render_table(["Violation", "Rows"], ladder_rows, "No ladder violations recorded.")}
        </div>
      </div>
      <div class="table-section">
        <h3>Event Exposure and Latent Final High</h3>
        {render_table(["Event", "Mean F", "Sigma", "Observed", "Exposure", "Contracts"], event_rows, "No event exposure rows yet.")}
      </div>
      <p class="muted">Lifecycle funnel: {escape(json.dumps(funnel, sort_keys=True))}</p>
    """


def render_observed_high_latency_panel(metrics: dict[str, Any]) -> str:
    bucket_rows = [
        [escape(bucket), f'<span class="num">{escape(fmt_num(count))}</span>']
        for bucket, count in (metrics.get("delay_buckets") or {}).items()
    ]
    if not metrics.get("signals"):
        body = '<div class="empty-panel">No observed-high threshold touches recorded yet.</div>'
    else:
        body = f"""
          <div class="tuning-grid compact">
            <div>
              <h3>Half-Life Metrics</h3>
              <dl class="metric-list">
                <div><dt>Touches</dt><dd>{escape(fmt_num(metrics.get("signals")))}</dd></div>
                <div><dt>Snapshots</dt><dd>{escape(fmt_num(metrics.get("snapshots")))}</dd></div>
                <div><dt>Ask >= .95</dt><dd>{escape(fmt_num(metrics.get("seconds_to_ask_95"), 1))}s</dd></div>
                <div><dt>Ask >= .98</dt><dd>{escape(fmt_num(metrics.get("seconds_to_ask_98"), 1))}s</dd></div>
              </dl>
            </div>
            <div>
              <h3>Book Quality</h3>
              <dl class="metric-list">
                <div><dt>Avg Ask</dt><dd>{escape(fmt_price(metrics.get("avg_ask")))}</dd></div>
                <div><dt>Avg Spread</dt><dd>{escape(fmt_price(metrics.get("avg_spread")))}</dd></div>
                <div><dt>Quote Age</dt><dd>{escape(fmt_num(metrics.get("avg_quote_age_seconds"), 1))}s avg</dd></div>
                <div><dt>Depth Present</dt><dd>{escape(fmt_pct(metrics.get("depth_sufficient_pct")))}</dd></div>
              </dl>
            </div>
          </div>
          {render_table(["Detection Delay", "Touches"], bucket_rows, "No detection delay buckets yet.")}
        """
    return f"""
      <div class="panel-header">
        <div>
          <h2>Observed-High Latency Half-Life</h2>
          <p>Threshold-touch detection and post-touch public-book repricing snapshots.</p>
        </div>
        {status_pill("paper-only")}
      </div>
      {body}
    """


def signal_mix(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        select coalesce(nullif(signal_type, ''), 'unclassified') as signal_type, count(*) as count
        from signals
        group by coalesce(nullif(signal_type, ''), 'unclassified')
        order by count desc, signal_type
        """
    ).fetchall()


def latest_runs(db: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    return db.execute(
        """
        select
          r.id,
          r.started_at,
          r.markets_seen,
          r.signals_seen,
          (select count(*) from signals s where s.run_id=r.id and coalesce(s.signal_type, '') like 'paper_buy%') as paper_buys,
          (select count(*) from paper_orders o where o.run_id=r.id) as orders,
          (
            select count(*)
            from paper_orders o
            join paper_fills f on f.order_id=o.id
            where o.run_id=r.id
          ) as fills
        from runs r
        order by r.id desc
        limit ?
        """,
        (limit,),
    ).fetchall()


def recent_orders(db: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return db.execute(
        """
        select
          o.id,
          o.created_at,
          o.status,
          o.side,
          o.requested_shares,
          o.limit_price,
          o.estimated_cost,
          o.reason,
          o.outcome,
          coalesce(s.title, m.title, o.market_id) as title
        from paper_orders o
        left join signals s on s.id=o.signal_id
        left join markets m on m.market_id=o.market_id
        order by o.id desc
        limit ?
        """,
        (limit,),
    ).fetchall()


def recent_fills(db: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return db.execute(
        """
        select
          f.id,
          f.filled_at,
          f.shares,
          f.price,
          f.cost,
          f.source,
          f.raw_status,
          o.outcome,
          coalesce(s.title, m.title, o.market_id) as title
        from paper_fills f
        join paper_orders o on o.id=f.order_id
        left join signals s on s.id=o.signal_id
        left join markets m on m.market_id=o.market_id
        order by f.id desc
        limit ?
        """,
        (limit,),
    ).fetchall()


def recent_positions(db: sqlite3.Connection, account_id: int | None, limit: int = 12) -> list[sqlite3.Row]:
    if account_id is None:
        return []
    return db.execute(
        """
        select
          id,
          updated_at,
          status,
          shares,
          avg_price,
          cost_basis,
          latest_mark,
          realized_pnl,
          city,
          target_date,
          title,
          outcome,
          case
            when status='open' then (shares * coalesce(latest_mark, 0)) - cost_basis
            else realized_pnl
          end as pnl
        from paper_positions
        where account_id=?
        order by updated_at desc, id desc
        limit ?
        """,
        (account_id, limit),
    ).fetchall()


def latest_snapshots(db: sqlite3.Connection, account_id: int | None, limit: int = 24) -> list[sqlite3.Row]:
    if account_id is None:
        return []
    rows = db.execute(
        """
        select captured_at, cash, open_exposure, realized_pnl, unrealized_pnl, equity, return_pct, drawdown, unresolved_positions
        from paper_account_snapshots
        where account_id=?
        order by captured_at desc, id desc
        limit ?
        """,
        (account_id, limit),
    ).fetchall()
    return list(reversed(rows))


def progress_bar(value: float, target: float) -> str:
    pct = 0.0 if target <= 0 else min(100.0, max(0.0, value / target * 100.0))
    return (
        '<div class="progress" aria-hidden="true">'
        f'<span style="width:{pct:.1f}%"></span>'
        "</div>"
    )


def stat_card(label: str, value: str, detail: str = "", tone: str = "neutral") -> str:
    return (
        f'<article class="stat {escape(tone)}">'
        f'<span class="stat-label">{escape(label)}</span>'
        f'<strong>{escape(value)}</strong>'
        f'<small>{escape(detail)}</small>'
        "</article>"
    )


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    if rows:
        body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    else:
        body = f'<tr><td colspan="{len(headers)}" class="empty">{escape(empty)}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def status_pill(label: Any) -> str:
    raw = "" if label is None else str(label)
    normalized = raw.lower()
    if "filled" in normalized or "open" in normalized:
        tone = "good"
    elif "settled" in normalized:
        tone = "neutral"
    elif "skip" in normalized or "fail" in normalized or "loss" in normalized:
        tone = "bad"
    else:
        tone = "watch"
    return f'<span class="pill {tone}">{escape(raw or "-")}</span>'


def equity_svg(snapshots: Sequence[sqlite3.Row], starting_cash: float) -> str:
    if not snapshots:
        return '<div class="empty-panel">No account snapshots yet. Run scans or `python3 scanner.py portfolio --snapshot` to extend the equity trail.</div>'

    width = 760
    height = 170
    pad_x = 34
    pad_y = 22
    values = [float(row["equity"] or 0.0) for row in snapshots]
    values.append(float(starting_cash or DEFAULT_BANKROLL_USD))
    min_value = min(values)
    max_value = max(values)
    if abs(max_value - min_value) < 1e-9:
        min_value -= 1.0
        max_value += 1.0
    plot_w = width - pad_x * 2
    plot_h = height - pad_y * 2
    usable = snapshots if len(snapshots) > 1 else [snapshots[0], snapshots[0]]
    points: list[str] = []
    fill_points: list[str] = []
    for index, row in enumerate(usable):
        x = pad_x + (plot_w * index / max(1, len(usable) - 1))
        equity = float(row["equity"] or 0.0)
        y = pad_y + (max_value - equity) / (max_value - min_value) * plot_h
        points.append(f"{x:.1f},{y:.1f}")
        fill_points.append(f"{x:.1f},{y:.1f}")
    baseline_y = pad_y + (max_value - min_value) / (max_value - min_value) * plot_h
    fill_polygon = " ".join([f"{pad_x:.1f},{baseline_y:.1f}", *fill_points, f"{width - pad_x:.1f},{baseline_y:.1f}"])
    latest = snapshots[-1]
    return f"""
    <svg class="equity-chart" viewBox="0 0 {width} {height}" role="img" aria-label="Paper equity history">
      <line x1="{pad_x}" y1="{baseline_y:.1f}" x2="{width - pad_x}" y2="{baseline_y:.1f}" />
      <line x1="{pad_x}" y1="{pad_y}" x2="{pad_x}" y2="{height - pad_y}" />
      <polygon points="{fill_polygon}" />
      <polyline points="{" ".join(points)}" />
      <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}" r="4" />
      <text x="{pad_x}" y="16">{escape(fmt_money(max_value))}</text>
      <text x="{pad_x}" y="{height - 4}">{escape(fmt_money(min_value))}</text>
      <text x="{width - pad_x}" y="16" text-anchor="end">{escape(latest["captured_at"])}</text>
    </svg>
    """


def render_tunables_table(tuning_state: dict[str, Any]) -> str:
    current = tuning_state.get("current_tunables") or {}
    allowed = tuning_state.get("allowed_tunables") or {}
    names = sorted(set(current) | set(allowed))
    rows = [
        [
            escape(name),
            f'<span class="num">{escape(current.get(name, "-"))}</span>',
            escape(tuning_evaluator.range_label(allowed.get(name, "-"))),
        ]
        for name in names
    ]
    return render_table(["Tunable", "Current", "Allowed Proposal Values"], rows, "No tunable config found.")


def render_source_feature_tuning_panel(tuning_state: dict[str, Any]) -> str:
    source_rows = []
    for row in tuning_state.get("source_families") or []:
        source_rows.append(
            [
                escape(row.get("label") or row.get("key")),
                status_pill(row.get("status")),
                f'<span class="num">{escape(fmt_num(row.get("rows")))}</span>',
                escape(row.get("runtime_flag") or "core"),
                escape("required" if row.get("required") else ("stub" if row.get("adapter_stub") else "optional")),
            ]
        )
    feature_rows = []
    for row in tuning_state.get("feature_families") or []:
        feature_rows.append(
            [
                escape(row.get("label") or row.get("key")),
                status_pill(row.get("status")),
                f'<span class="num">{escape(fmt_pct(row.get("coverage_pct")))}</span>',
                f'<span class="num">{escape(fmt_num(row.get("covered_rows")))} / {escape(fmt_num(row.get("total_rows")))}</span>',
                escape("critical" if row.get("critical") else "optional"),
            ]
        )
    knob_names = [
        "enable_wu",
        "enable_nws",
        "enable_iem",
        "enable_ncei_daily",
        "enable_meteostat",
        "enable_metar_direct",
        "enable_om_models",
        "allow_paid_provider_features",
        "forecast_cache_ttl_seconds",
        "observation_cache_ttl_seconds",
        "orderbook_cache_ttl_seconds",
        "http_timeout_seconds",
        "missingness_penalty",
        "source_disagreement_threshold_f",
        "paper_forward_min_labeled_rows",
        "paper_forward_min_feature_coverage",
    ]
    current = tuning_state.get("current_tunables") or {}
    knob_rows = [
        [escape(name), f'<span class="num">{escape(current.get(name, "-"))}</span>']
        for name in knob_names
        if name in current
    ]
    post_labels = tuning_state.get("post_labels") or {}
    missingness = tuning_state.get("feature_missingness_summary") or {}
    source_counts = tuning_state.get("source_family_counts") or {}
    feature_counts = tuning_state.get("feature_family_counts") or {}
    return f"""
      <div class="panel-header">
        <div>
          <h2>Data Sources &amp; Feature Tuning</h2>
          <p>Read-only source status, no-lookahead feature coverage, and post-label paper-forward-test gates.</p>
        </div>
        {status_pill(post_labels.get("status") or "not-approved")}
      </div>
      <div class="tuning-banner" aria-label="Data source safety status">
        <strong>Read-only and cached</strong>
        <span>Commercial providers remain optional stubs; Weather Underground stays disabled unless <code>ENABLE_WU</code> or the runtime flag enables it.</span>
      </div>
      <div class="tuning-grid">
        <div>
          <h3>Post-Labels Gate</h3>
          <dl class="metric-list">
            <div><dt>Paper Forward</dt><dd>{escape(post_labels.get("status") or "not-approved")}</dd></div>
            <div><dt>Approved</dt><dd>{escape(str(bool(post_labels.get("approved_for_paper_forward_test"))).lower())}</dd></div>
            <div><dt>Live Trading</dt><dd>false</dd></div>
            <div><dt>Max Drawdown</dt><dd>{escape(fmt_pct(post_labels.get("max_drawdown_seen")))} / {escape(fmt_pct(post_labels.get("guardrail_max_drawdown")))}</dd></div>
          </dl>
          <p class="muted">{escape(post_labels.get("note") or "")}</p>
        </div>
        <div>
          <h3>Coverage Summary</h3>
          <dl class="metric-list">
            <div><dt>Sources</dt><dd>{escape(source_counts.get("active", 0))} active, {escape(source_counts.get("missing", 0))} missing, {escape(source_counts.get("optional", 0))} optional, {escape(source_counts.get("disabled", 0))} disabled</dd></div>
            <div><dt>Features</dt><dd>{escape(feature_counts.get("active", 0))} active, {escape(feature_counts.get("missing", 0))} missing, {escape(feature_counts.get("optional", 0))} optional</dd></div>
            <div><dt>No-Lookahead</dt><dd>{escape(fmt_num(missingness.get("no_lookahead_leakage_rows")))} leaked rows</dd></div>
            <div><dt>Schema</dt><dd>features v{escape(missingness.get("feature_schema_version") or "-")}</dd></div>
          </dl>
        </div>
      </div>
      <div class="split">
        <div>
          <h3>Source Families</h3>
          {render_table(["Source", "Status", "Rows", "Flag", "Role"], source_rows, "No source family metadata available.")}
        </div>
        <div>
          <h3>Feature Families</h3>
          {render_table(["Feature", "Status", "Coverage", "Rows", "Role"], feature_rows, "No feature coverage metadata available.")}
        </div>
      </div>
      <div class="table-section">
        <h3>Source and Forward-Test Knobs</h3>
        {render_table(["Knob", "Current"], knob_rows, "No source tuning knobs found.")}
      </div>
    """



def render_strategy_family_survival_panel(rows: list[dict[str, Any]]) -> str:
    promote = sum(1 for r in rows if r.get("verdict") == edge_validation.PROMOTE_PAPER_SIZE)
    killed = sum(1 for r in rows if r.get("verdict") == edge_validation.KILL_OR_DISABLE)
    disabled = sum(1 for r in rows if r.get("verdict") != edge_validation.PROMOTE_PAPER_SIZE)
    summary_cards = "".join([
        stat_card("Families", fmt_num(len(rows)), "Strategy buckets with evidence", "neutral"),
        stat_card("Promotable", fmt_num(promote), "Passes survival gate", "positive" if promote else "neutral"),
        stat_card("Disabled", fmt_num(disabled), "Blocked from new paper fills by default", "warning" if disabled else "neutral"),
        stat_card("Killed", fmt_num(killed), "Failed survival criteria", "negative" if killed else "neutral"),
    ])
    table_rows: list[list[str]] = []
    for row in rows[:16]:
        verdict = str(row.get("verdict") or "-")
        table_rows.append([
            f'<strong>{escape(row.get("strategy_family"))}</strong>',
            status_pill(verdict),
            f'<span class="num">{escape(fmt_num(row.get("survival_score"), 3))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("resolved_count")))}</span>',
            f'<span class="num">{escape(fmt_num(row.get("sample_days")))}</span>',
            f'<span class="num {escape(tone_for(float(row.get("realized_pnl") or 0.0)))}">{escape(fmt_money(row.get("realized_pnl"), signed=True))}</span>',
            f'<span class="num {escape(tone_for(float(row.get("roi") or 0.0)))}">{escape(fmt_pct(row.get("roi"), signed=True))}</span>',
            f'<span class="num {escape(tone_for(float(row.get("brier_delta") or 0.0)))}">{escape(fmt_num(row.get("brier_delta"), 4))}</span>',
            f'<span class="num">{escape(fmt_pct(row.get("edge_decile_persistence")))}</span>',
            f'<span class="num">{escape(fmt_pct(row.get("execution_realism")))}</span>',
            f'<span class="num">{escape(fmt_pct(row.get("ambiguity_control")))}</span>',
        ])
    return f"""
      <div class="panel-header">
        <div>
          <h2>Strategy Family Survival</h2>
          <p>Family-level paper edge validation: realized PnL, calibration advantage, edge-decile persistence, execution realism, and ambiguity control. Non-promoted families are disabled for new paper fills by default.</p>
        </div>
        {status_pill("paper gate")}
      </div>
      <div class="grid stats compact">{summary_cards}</div>
      {render_table(["Family", "Verdict", "Score", "Resolved", "Days", "PnL", "ROI", "Brier Δ", "Decile persistence", "Execution", "Ambiguity"], table_rows, "No strategy-family survival rows yet.")}
      <p class="muted">Promotion target: {edge_validation.RESOLVED_TARGET} resolved rows and {edge_validation.DAY_TARGET} sample days, positive realized PnL, positive model-vs-market Brier delta, persistent edge deciles, clean labels, and executable fills.</p>
    """

def render_labeling_settlement_panel(progress: dict[str, Any]) -> str:
    status_counts = progress.get("attempt_status_counts") or {}
    coverage_rows = [
        [
            escape(row.get("source_provider")),
            status_pill(row.get("outcome_status")),
            f'<span class="num">{escape(fmt_num(row.get("count")))}</span>',
            escape(row.get("latest_attempt") or "-"),
        ]
        for row in (progress.get("source_coverage") or [])
    ]
    attempt_rows = [
        [
            f'<span class="num">{escape(row.get("id"))}</span><br><span class="muted">{escape(row.get("attempted_at"))}</span>',
            escape(row.get("source_provider")),
            status_pill(row.get("outcome_status")),
            status_pill(row.get("source_status")),
            f'<span class="num">{escape(fmt_num(row.get("final_high_f"), 1))}</span>',
            f'<span class="num">{escape(fmt_price(row.get("label_value")))}</span>',
            escape(row.get("station_id")),
            f'<span class="title-cell">{escape(row.get("title"))}</span><br><span class="muted">{escape(row.get("outcome"))}</span>',
            escape(row.get("reason")),
        ]
        for row in (progress.get("recent_attempts") or [])
    ]
    blocker_rows = [
        [escape(row.get("reason")), f'<span class="num">{escape(fmt_num(row.get("count")))}</span>']
        for row in (progress.get("blockers") or [])
    ]
    return f"""
      <div class="panel-header">
        <div>
          <h2>Labeling and Settlement Progress</h2>
          <p>Delayed outcome labels stay separate from decision-time feature snapshots and drive paper-only settlement.</p>
        </div>
        {status_pill("labeler")}
      </div>
      <div class="tuning-banner" aria-label="Labeling safety status">
        <strong>Read-only labels</strong>
        <span>Final rows require delayed label evidence; provisional source attempts do not settle positions or unlock live trading.</span>
      </div>
      <div class="tuning-grid compact">
        <div>
          <h3>Progress</h3>
          <dl class="metric-list">
            <div><dt>Labeled Rows</dt><dd>{escape(fmt_num(progress.get("labeled_rows")))} / {escape(fmt_num(progress.get("training_rows")))}</dd></div>
            <div><dt>Pending Rows</dt><dd>{escape(fmt_num(progress.get("pending_rows")))}</dd></div>
            <div><dt>Future Rows</dt><dd>{escape(fmt_num(progress.get("future_rows")))}</dd></div>
            <div><dt>Attempts</dt><dd>{escape(fmt_num(progress.get("attempts")))}</dd></div>
          </dl>
        </div>
        <div>
          <h3>Paper Settlement</h3>
          <dl class="metric-list">
            <div><dt>Open Positions</dt><dd>{escape(fmt_num(progress.get("open_positions")))}</dd></div>
            <div><dt>Settled Positions</dt><dd>{escape(fmt_num(progress.get("settled_positions")))}</dd></div>
            <div><dt>Settlement Rows</dt><dd>{escape(fmt_num(progress.get("settlements")))}</dd></div>
            <div><dt>Attempt Status</dt><dd>final {escape(fmt_num(status_counts.get("final", 0)))}, provisional {escape(fmt_num(status_counts.get("provisional", 0)))}, pending {escape(fmt_num(status_counts.get("pending", 0)))}</dd></div>
          </dl>
        </div>
      </div>
      <div class="split">
        <div>
          <h3>Source Coverage</h3>
          {render_table(["Source", "Status", "Attempts", "Latest"], coverage_rows, "No label attempts recorded yet.")}
        </div>
        <div>
          <h3>Blockers</h3>
          {render_table(["Reason", "Rows"], blocker_rows, "No current label blockers recorded.")}
        </div>
      </div>
      <div class="table-section">
        <h3>Recent Label Attempts</h3>
        {render_table(["Attempt", "Source", "Outcome", "Fetch", "High F", "Label", "Station", "Market", "Reason"], attempt_rows, "No delayed label attempts recorded yet.")}
      </div>
    """


def render_tuning_section(tuning_state: dict[str, Any], metrics: dict[str, Any], progress: dict[str, Any]) -> str:
    status = str(tuning_state.get("status") or "scaffold_only")
    evidence = tuning_state.get("evidence") or {}
    minimums = tuning_state.get("minimums") or {}
    target_metrics = tuning_state.get("target_metrics") or {}
    proposal_trace = tuning_state.get("proposal_trace") or {}
    status_copy = {
        "scaffold_only": "Scaffold only: guardrails or goal config need review before proposals.",
        "insufficient_data": "Insufficient data: proposal generation remains locked.",
        "ready_for_proposals": "Ready for proposals: config suggestions may be produced, but promotion stays manual.",
        "approved_for_paper_forward_test": "Post-label gates passed: config suggestions may be approved for paper-forward-test only.",
    }.get(status, status)
    gate_rows = []
    for gate in tuning_state.get("gates") or []:
        label = str(gate.get("name", "")).replace("_", " ").title()
        current = float(gate.get("current") or 0)
        minimum = float(gate.get("minimum") or 0)
        gate_rows.append(
            f"""
            <div class="progress-row">
              <strong>{escape(label)}</strong>
              {progress_bar(current, minimum)}
              <span>{escape(fmt_num(current))} / {escape(fmt_num(minimum))}</span>
            </div>
            """
        )
    if not gate_rows:
        gate_rows.append('<div class="empty-panel">No readiness gates configured.</div>')

    proposal_note = proposal_trace.get("note") or "No tuning proposals are active."
    blocked = tuning_state.get("blocked_reasons") or []
    blocked_copy = ", ".join(str(reason).replace("_", " ") for reason in blocked) or "none"
    return f"""
      <div class="panel-header">
        <div>
          <h2>Tuning Readiness and Performance Trace</h2>
          <p>{escape(status_copy)}</p>
        </div>
        {status_pill(status)}
      </div>
      <div class="tuning-banner" aria-label="Tuning safety status">
        <strong>Paper-only, proposal-only</strong>
        <span>No wallet, no live trading, no order placement, no deployment or promotion from this dashboard.</span>
      </div>
      <div class="tuning-grid">
        <div>
          <h3>Target Metrics</h3>
          <dl class="metric-list">
            <div><dt>Primary</dt><dd>{escape(target_metrics.get("primary") or "-")}</dd></div>
            <div><dt>Secondary</dt><dd>{escape(target_metrics.get("secondary") or "-")}</dd></div>
            <div><dt>Validation</dt><dd>{escape(target_metrics.get("validation_method") or "-")}</dd></div>
            <div><dt>Current Return</dt><dd>{escape(fmt_pct(metrics.get("return_pct"), signed=True))}</dd></div>
            <div><dt>Current Drawdown</dt><dd>{escape(fmt_pct(metrics.get("drawdown")))}</dd></div>
          </dl>
        </div>
        <div>
          <h3>Evidence Gates</h3>
          {"".join(gate_rows)}
          <p class="muted">Window: {escape(evidence.get("first_date") or "-")} to {escape(evidence.get("last_date") or "-")}. Paper buy training rows: {escape(fmt_num(evidence.get("paper_buy_rows")))}. Snapshot rows: {escape(fmt_num(evidence.get("snapshot_rows")))}. Blocked gates: {escape(blocked_copy)}.</p>
        </div>
      </div>
      <div class="tuning-grid compact">
        <div>
          <h3>Performance Context</h3>
          <dl class="metric-list">
            <div><dt>Candidate Progress</dt><dd>{escape(fmt_num(progress.get("candidate_count")))} / {EVIDENCE_CANDIDATE_TARGET}</dd></div>
            <div><dt>Calendar Span</dt><dd>{escape(fmt_num(progress.get("sample_days")))} / {EVIDENCE_DAY_TARGET} days</dd></div>
            <div><dt>Training Rows</dt><dd>{escape(fmt_num(evidence.get("training_rows")))} / {escape(fmt_num(minimums.get("training_rows")))}</dd></div>
            <div><dt>Labeled Rows</dt><dd>{escape(fmt_num(evidence.get("labeled_rows")))} / {escape(fmt_num(minimums.get("labeled_rows")))}</dd></div>
          </dl>
        </div>
        <div>
          <h3>Proposal Trace</h3>
          <p class="muted">{escape(proposal_note)} Active proposals: {escape(fmt_num(proposal_trace.get("count")))}. Latest proposal: {escape(proposal_trace.get("latest") or "-")}.</p>
        </div>
      </div>
    """


def summarize_tunables(values: dict[str, Any], limit: int = 4) -> str:
    if not values:
        return "-"
    parts = []
    for name in sorted(values)[:limit]:
        value = values[name]
        if isinstance(value, dict):
            selected = value.get("selected")
            candidates = value.get("candidate_values")
            if selected is not None:
                rendered = selected
            elif candidates:
                rendered = f"candidates: {tuning_evaluator.range_label(candidates)}"
            else:
                rendered = "pending"
        else:
            rendered = value
        parts.append(f"{name}={rendered}")
    if len(values) > limit:
        parts.append(f"+{len(values) - limit} more")
    return ", ".join(str(part) for part in parts)


def render_tuning_iterations_section(iterations: Sequence[dict[str, Any]], iteration_log_path: str) -> str:
    rows = []
    for record in reversed(list(iterations)):
        evidence = record.get("evidence_counts") or {}
        gates = record.get("gates") or []
        metrics = record.get("available_performance_metrics") or {}
        approval = record.get("approval") or {}
        labeling = record.get("labeling") or {}
        ready_gates = sum(1 for gate in gates if gate.get("ready"))
        evidence_summary = (
            f"training {fmt_num(evidence.get('training_rows'))}; "
            f"labeled {fmt_num(evidence.get('labeled_rows'))}; "
            f"paper buys {fmt_num(evidence.get('paper_buy_rows'))}; "
            f"days {fmt_num(evidence.get('calendar_days'))}"
        )
        metric_summary = (
            f"return {fmt_pct(metrics.get('return_pct'), signed=True)}; "
            f"drawdown {fmt_pct(metrics.get('drawdown'))}; "
            f"brier {fmt_num(metrics.get('brier_score'), 3)}; "
            f"log loss {fmt_num(metrics.get('log_loss'), 3)}; "
            f"unresolved {fmt_pct(metrics.get('unresolved_rate'))}; "
            f"labels={'yes' if labeling.get('labels_available') else 'no'}; "
            f"settlements {fmt_num(labeling.get('paper_settlements') if labeling else metrics.get('paper_settlements'))}"
        )
        approval_summary = (
            f"{record.get('approval_status') or 'not-approved'}; "
            f"paper-forward={str(bool(approval.get('approved_for_paper_forward_test'))).lower()}; "
            "live=false"
        )
        rows.append(
            [
                f'<span class="num">{escape(record.get("id") or "-")}</span><br><span class="muted">{escape(record.get("timestamp") or "-")}</span>',
                status_pill(record.get("status") or "unknown"),
                escape(evidence_summary),
                escape(f"{ready_gates} / {len(gates)} gates ready"),
                escape(metric_summary),
                escape(summarize_tunables(record.get("current_tunables") or {})),
                escape(summarize_tunables(record.get("proposed_tunables") or {})),
                f'{status_pill(approval_summary)}<br><span class="muted">safety_ok={escape(str(bool(record.get("safety_ok"))).lower())}</span>',
            ]
        )
    return f"""
      <div class="panel-header">
        <div>
          <h2>Tuning Iteration Performance</h2>
          <p>Persistent paper-only tune attempts, including insufficient-data passes before labels exist.</p>
        </div>
        {status_pill("paper-only")}
      </div>
      <div class="tuning-banner" aria-label="Tuning iteration safety">
        <strong>Logged locally as JSONL</strong>
        <span>Each record is proposal-only and sanitized to keep wallet, live trading, signing, deployment, and order placement disabled.</span>
      </div>
      {render_table(
        ["Iteration", "Status", "Evidence", "Gates", "Metrics", "Current Tunables", "Proposed Tunables", "Approval"],
        rows,
        "No tuning iterations logged yet.",
      )}
      <p class="muted">Log path: <code>{escape(os.path.abspath(iteration_log_path))}</code>. Unsafe log entries are marked rejected and cannot enable trading controls.</p>
    """


def page_css() -> str:
    return """
    :root {
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #141821;
      --muted: #617083;
      --line: #dbe3ec;
      --line-strong: #c5d0dd;
      --teal: #0f766e;
      --blue: #2563eb;
      --amber: #b7791f;
      --red: #b42318;
      --green: #047857;
      --shadow: 0 18px 42px rgba(35, 48, 75, 0.08);
      color-scheme: light;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(180deg, #eef5f6 0, rgba(246, 248, 251, 0) 260px),
        var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    }
    main {
      width: min(1200px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 44px;
    }
    header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: start;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: clamp(28px, 4vw, 48px);
      line-height: 1.02;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 14px;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    h3 {
      margin: 0 0 10px;
      font-size: 13px;
      line-height: 1.2;
      letter-spacing: .04em;
      text-transform: uppercase;
      color: #344054;
    }
    p { margin: 0; color: var(--muted); }
    .subhead { margin-top: 10px; max-width: 760px; font-size: 15px; }
    .mode-stack {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      max-width: 400px;
    }
    .live-panel {
      grid-column: 1 / -1;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
      gap: 8px 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .connection-state {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      border-radius: 999px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
    }
    .connection-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--muted);
    }
    .connection-state.online .connection-dot { background: var(--green); }
    .connection-state.polling .connection-dot { background: var(--blue); }
    .connection-state.embedded .connection-dot { background: var(--amber); }
    .connection-state.error .connection-dot { background: var(--red); }
    .mode-label,
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      border-radius: 999px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .mode-label.safe { border-color: rgba(15, 118, 110, 0.24); color: #075e57; background: #e9f7f5; }
    .mode-label.stop { border-color: rgba(180, 35, 24, 0.20); color: var(--red); background: #fff1ef; }
    .notice {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      margin: 16px 0 22px;
      padding: 14px 16px;
      border: 1px solid rgba(183, 121, 31, 0.24);
      border-left: 4px solid var(--amber);
      background: #fff8eb;
      border-radius: 8px;
    }
    .notice strong { color: #7a4d0b; }
    .notice p { color: #6f5a37; }
    .tuning-banner {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      align-items: center;
      margin: 2px 0 16px;
      padding: 12px 14px;
      border: 1px solid rgba(15, 118, 110, 0.22);
      border-left: 4px solid var(--teal);
      border-radius: 8px;
      background: #ecfdf9;
      color: #075e57;
    }
    .tuning-banner span { color: #176b63; }
    .tuning-grid {
      display: grid;
      grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr);
      gap: 16px;
      align-items: start;
      margin-top: 14px;
    }
    .tuning-grid.compact {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }
    .metric-list {
      display: grid;
      gap: 8px;
      margin: 0;
    }
    .metric-list div {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 10px;
      align-items: baseline;
    }
    .metric-list dt {
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .metric-list dd {
      margin: 0;
      color: var(--ink);
      font-weight: 650;
    }
    .grid {
      display: grid;
      gap: 14px;
    }
    .stats {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-bottom: 16px;
    }
    .counts {
      grid-template-columns: repeat(6, minmax(0, 1fr));
      margin-bottom: 16px;
    }
    .stat,
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .stat {
      min-height: 118px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
    }
    .stat-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .stat strong {
      display: block;
      font-size: clamp(24px, 3vw, 34px);
      line-height: 1;
      letter-spacing: 0;
    }
    .stat small {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
    }
    .stat.positive strong { color: var(--green); }
    .stat.negative strong { color: var(--red); }
    .stat.warning strong { color: var(--amber); }
    .section-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
      gap: 16px;
      margin-bottom: 16px;
    }
    .panel {
      padding: 18px;
      overflow: hidden;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      margin-bottom: 12px;
    }
    .panel-header p { font-size: 13px; }
    .progress-row {
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr) 90px;
      gap: 12px;
      align-items: center;
      padding: 11px 0;
      border-top: 1px solid var(--line);
    }
    .progress-row:first-of-type { border-top: 0; }
    .progress-row strong { font-size: 13px; }
    .progress-row span { color: var(--muted); font-size: 12px; text-align: right; }
    .progress {
      height: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: #e9eef4;
    }
    .progress span {
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--teal), var(--blue));
      border-radius: inherit;
    }
    .equity-chart {
      display: block;
      width: 100%;
      height: auto;
      min-height: 170px;
      margin-top: 6px;
    }
    .equity-chart line {
      stroke: #d3dde8;
      stroke-width: 1;
    }
    .equity-chart polygon {
      fill: rgba(15, 118, 110, 0.10);
    }
    .equity-chart polyline {
      fill: none;
      stroke: var(--teal);
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .equity-chart circle { fill: var(--teal); }
    .equity-chart text {
      fill: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .table-section { margin-top: 16px; }
    .table-wrap {
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    table {
      width: 100%;
      min-width: 760px;
      border-collapse: collapse;
    }
    th,
    td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      position: sticky;
      top: 0;
      background: #f1f5f9;
      color: #344054;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .04em;
      z-index: 1;
    }
    tr:last-child td { border-bottom: 0; }
    td {
      color: #263241;
      font-size: 13px;
    }
    .title-cell {
      min-width: 260px;
      max-width: 420px;
      color: var(--ink);
      font-weight: 650;
    }
    .muted { color: var(--muted); }
    .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .good { color: var(--green); background: #ecfdf3; border-color: rgba(4, 120, 87, 0.18); }
    .bad { color: var(--red); background: #fff1f0; border-color: rgba(180, 35, 24, 0.18); }
    .watch { color: #7a4d0b; background: #fff8eb; border-color: rgba(183, 121, 31, 0.18); }
    .neutral { color: #344054; background: #f7f9fc; border-color: var(--line); }
    .empty,
    .empty-panel {
      color: var(--muted);
      background: #f8fafc;
    }
    .empty-panel {
      padding: 18px;
      border: 1px dashed var(--line-strong);
      border-radius: 8px;
    }
    .split {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 16px;
    }
    footer {
      margin-top: 22px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
    }
    code {
      padding: 2px 5px;
      border-radius: 5px;
      background: #edf2f7;
      color: #263241;
      font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    @media (max-width: 900px) {
      main { width: min(100% - 20px, 760px); padding-top: 18px; }
      header,
      .section-grid,
      .split {
        grid-template-columns: 1fr;
      }
      .live-panel,
      .mode-stack { justify-content: flex-start; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .counts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .tuning-grid,
      .tuning-grid.compact { grid-template-columns: 1fr; }
      .panel { padding: 14px; }
    }
    @media (max-width: 560px) {
      .stats,
      .counts {
        grid-template-columns: 1fr;
      }
      .notice { grid-template-columns: 1fr; }
      .progress-row {
        grid-template-columns: 1fr;
        gap: 6px;
      }
      .progress-row span { text-align: left; }
      .metric-list div { grid-template-columns: 1fr; gap: 2px; }
      table { min-width: 680px; }
    }
    """


def build_snapshot(
    db: sqlite3.Connection,
    db_path: str,
    json_filename: str = os.path.basename(DEFAULT_JSON_OUTPUT),
    iteration_log_path: str = tuning_evaluator.TUNING_ITERATIONS_PATH,
) -> dict[str, Any]:
    generated_at = utc_now_iso()
    metrics = portfolio_metrics(db)
    progress = evaluation_progress(db)
    label_progress = labeling_progress(db)
    research = research_metrics(db)
    observed_high_latency = latency_metrics.aggregate_latency_metrics(db)
    tuning_state = tuning_evaluator.evaluate_tuning_state(db_path=db_path)
    survival_rows = edge_validation.evaluate_strategy_families(db_path=db_path, persist=False)
    tuning_iterations = tuning_evaluator.load_tuning_iterations(iteration_log_path)
    if not tuning_iterations:
        tuning_iterations = [
            tuning_evaluator.build_tuning_iteration_record(
                tuning_state,
                timestamp=generated_at,
                iteration_id="current-tuning-snapshot",
                persisted=False,
            )
        ]
    counts = {
        "markets": table_count(db, "markets"),
        "signals": table_count(db, "signals"),
        "training rows": table_count(db, "training_rows"),
        "orders": table_count(db, "paper_orders"),
        "fills": table_count(db, "paper_fills"),
        "positions": table_count(db, "paper_positions"),
        "label attempts": table_count(db, "label_attempts"),
        "events": table_count(db, "events"),
        "calibration rows": table_count(db, "calibration_rows"),
    }
    expanded_counts = {
        "runs": table_count(db, "runs"),
        "forecasts": table_count(db, "forecast_snapshots"),
        "station observations": table_count(db, "station_observations"),
        "order books": table_count(db, "orderbook_snapshots"),
        "settlements": table_count(db, "paper_settlements"),
        "accounts": table_count(db, "paper_accounts"),
        "contract payouts": table_count(db, "contract_payouts"),
        "event snapshots": table_count(db, "event_exposure_snapshots"),
        "lifecycle rows": table_count(db, "lifecycle_attribution"),
    }
    paper_buy_count = db.execute(
        "select count(*) from signals where coalesce(signal_type, '') like 'paper_buy%'"
    ).fetchone()[0]
    snapshot_rows = latest_snapshots(db, metrics["account_id"])
    snapshot_data = [row_dict(row) for row in snapshot_rows]

    bankroll_stats = [
        stat_card("Cash", fmt_money(metrics["cash"]), f"Start: {fmt_money(metrics['starting_cash'])}", "neutral"),
        stat_card("Equity", fmt_money(metrics["equity"]), f"Open mark value: {fmt_money(metrics['open_value'])}", tone_for(float(metrics["return_pct"]))),
        stat_card("Open Exposure", fmt_money(metrics["open_exposure"]), "Simulated cost basis at risk", "warning" if metrics["open_exposure"] else "neutral"),
        stat_card("Realized PnL", fmt_money(metrics["realized_pnl"], signed=True), "Settled paper positions only", tone_for(float(metrics["realized_pnl"]))),
        stat_card("Unrealized PnL", fmt_money(metrics["unrealized_pnl"], signed=True), "Open positions marked to latest signal", tone_for(float(metrics["unrealized_pnl"]))),
        stat_card("Return", fmt_pct(metrics["return_pct"], signed=True), "Equity vs simulated bankroll", tone_for(float(metrics["return_pct"]))),
        stat_card("Drawdown", fmt_pct(metrics["drawdown"]), "From snapshot high-water mark", tone_for(float(metrics["drawdown"]), inverse=True) if metrics["drawdown"] else "neutral"),
        stat_card("Unresolved Positions", fmt_num(metrics["unresolved_positions"]), "Open paper positions", "warning" if metrics["unresolved_positions"] else "neutral"),
    ]

    count_cards = [
        stat_card(label.title(), fmt_num(value), "SQLite rows", "neutral")
        for label, value in counts.items()
    ]

    expanded_count_rows = [[escape(k.title()), f'<span class="num">{escape(fmt_num(v))}</span>'] for k, v in expanded_counts.items()]
    latest_run_data = [row_dict(row) for row in latest_runs(db)]
    latest_run_rows = [
        [
            f'<span class="num">{escape(row["id"])}</span>',
            escape(row["started_at"]),
            f'<span class="num">{escape(fmt_num(row["markets_seen"]))}</span>',
            f'<span class="num">{escape(fmt_num(row["signals_seen"]))}</span>',
            f'<span class="num">{escape(fmt_num(row["paper_buys"]))}</span>',
            f'<span class="num">{escape(fmt_num(row["orders"]))}</span>',
            f'<span class="num">{escape(fmt_num(row["fills"]))}</span>',
        ]
        for row in latest_run_data
    ]
    signal_mix_data = [row_dict(row) for row in signal_mix(db)]
    signal_mix_rows = [
        [escape(row["signal_type"]), f'<span class="num">{escape(fmt_num(row["count"]))}</span>']
        for row in signal_mix_data
    ]

    order_data = [row_dict(row) for row in recent_orders(db)]
    order_rows = [
        [
            f'<span class="num">{escape(row["id"])}</span>',
            escape(row["created_at"]),
            status_pill(row["status"]),
            escape(row["side"]),
            f'<span class="num">{escape(fmt_num(row["requested_shares"], 2))}</span>',
            f'<span class="num">{escape(fmt_price(row["limit_price"]))}</span>',
            f'<span class="num">{escape(fmt_money(row["estimated_cost"]))}</span>',
            f'<span class="title-cell">{escape(row["title"])}</span><br><span class="muted">{escape(row["outcome"])}</span>',
            escape(row["reason"]),
        ]
        for row in order_data
    ]

    fill_data = [row_dict(row) for row in recent_fills(db)]
    fill_rows = [
        [
            f'<span class="num">{escape(row["id"])}</span>',
            escape(row["filled_at"]),
            f'<span class="num">{escape(fmt_num(row["shares"], 2))}</span>',
            f'<span class="num">{escape(fmt_price(row["price"]))}</span>',
            f'<span class="num">{escape(fmt_money(row["cost"]))}</span>',
            escape(row["source"]),
            status_pill(row["raw_status"]),
            f'<span class="title-cell">{escape(row["title"])}</span><br><span class="muted">{escape(row["outcome"])}</span>',
        ]
        for row in fill_data
    ]

    position_rows = []
    position_data = []
    for row in recent_positions(db, metrics["account_id"]):
        position = row_dict(row)
        pnl = float(row["pnl"] or 0.0)
        position_data.append(position)
        position_rows.append(
            [
                f'<span class="num">{escape(row["id"])}</span>',
                escape(row["updated_at"]),
                status_pill(row["status"]),
                f'<span class="num">{escape(fmt_num(row["shares"], 2))}</span>',
                f'<span class="num">{escape(fmt_price(row["avg_price"]))}</span>',
                f'<span class="num">{escape(fmt_money(row["cost_basis"]))}</span>',
                f'<span class="num">{escape(fmt_price(row["latest_mark"]))}</span>',
                f'<span class="num {escape(tone_for(pnl))}">{escape(fmt_money(pnl, signed=True))}</span>',
                f'<span class="title-cell">{escape(row["title"])}</span><br><span class="muted">{escape(row["city"])} {escape(row["target_date"])} | {escape(row["outcome"])}</span>',
            ]
        )

    day_gap = max(0, EVIDENCE_DAY_TARGET - int(progress["sample_days"]))
    candidate_gap = max(0, EVIDENCE_CANDIDATE_TARGET - int(progress["candidate_count"]))
    readiness = "Ready for evaluation gate" if progress["ready"] else "Collecting evidence"
    readiness_detail = (
        "14-day and 300-candidate thresholds are met."
        if progress["ready"]
        else f"Need {day_gap} more calendar days and {candidate_gap} more marked paper candidates."
    )

    header_subhead = (
        f'Static performance tracker for the simulated {escape(fmt_money(metrics["starting_cash"]))} '
        f'paper bankroll. Generated from <code>{escape(os.path.basename(db_path))}</code> '
        f'at <code>{escape(generated_at)}</code>.'
    )
    evidence_progress = f"""
      <div class="panel-header">
        <div>
          <h2>Evidence Progress</h2>
          <p>{escape(readiness)}. {escape(readiness_detail)}</p>
        </div>
        {status_pill("paper-only")}
      </div>
      <div class="progress-row">
        <strong>Calendar span</strong>
        {progress_bar(float(progress["sample_days"]), EVIDENCE_DAY_TARGET)}
        <span>{escape(fmt_num(progress["sample_days"]))} / {EVIDENCE_DAY_TARGET} days</span>
      </div>
      <div class="progress-row">
        <strong>Candidates</strong>
        {progress_bar(float(progress["candidate_count"]), EVIDENCE_CANDIDATE_TARGET)}
        <span>{escape(fmt_num(progress["candidate_count"]))} / {EVIDENCE_CANDIDATE_TARGET}</span>
      </div>
      <div class="progress-row">
        <strong>Training rows</strong>
        {progress_bar(float(counts["training rows"]), EVIDENCE_CANDIDATE_TARGET)}
        <span>{escape(fmt_num(counts["training rows"]))} rows</span>
      </div>
      <p class="muted">Evaluation filter: paper_buy signals, edge >= {EVALUATION_EDGE_THRESHOLD:.0%}, entry {EVALUATION_MIN_ENTRY:.0%}-{EVALUATION_MAX_ENTRY:.0%}. Candidate dates: {escape(progress["first_date"] or "-")} to {escape(progress["last_date"] or "-")}. Paper-buy signals captured: {escape(fmt_num(paper_buy_count))}. Mean candidate edge: {escape(fmt_pct(progress["mean_edge"], signed=True))}.</p>
    """

    fragments = {
        "header_subhead": header_subhead,
        "bankroll_stats": "".join(bankroll_stats),
        "count_cards": "".join(count_cards),
        "evidence_progress": evidence_progress,
        "equity_chart": equity_svg(snapshot_rows, float(metrics["starting_cash"])),
        "labeling_settlement_panel": render_labeling_settlement_panel(label_progress),
        "research_metrics_panel": render_research_metrics_panel(research),
        "observed_high_latency_panel": render_observed_high_latency_panel(observed_high_latency),
        "tuning_section": render_tuning_section(tuning_state, metrics, progress),
        "strategy_family_survival_panel": render_strategy_family_survival_panel(survival_rows),
        "tuning_iterations_section": render_tuning_iterations_section(tuning_iterations, iteration_log_path),
        "data_sources_feature_tuning_panel": render_source_feature_tuning_panel(tuning_state),
        "tunables_table": render_tunables_table(tuning_state),
        "additional_counts_table": render_table(["Table", "Rows"], expanded_count_rows, "No counts available."),
        "signal_mix_table": render_table(["Signal Type", "Rows"], signal_mix_rows, "No signals recorded yet."),
        "latest_runs_table": render_table(["Run", "Started", "Markets", "Signals", "Paper Buys", "Orders", "Fills"], latest_run_rows, "No runs recorded yet."),
        "recent_orders_table": render_table(["ID", "Created", "Status", "Side", "Requested", "Limit", "Cost", "Market", "Reason"], order_rows, "No paper orders recorded yet."),
        "recent_fills_table": render_table(["ID", "Filled", "Shares", "Price", "Cost", "Source", "Status", "Market"], fill_rows, "No paper fills recorded yet."),
        "recent_positions_table": render_table(["ID", "Updated", "Status", "Shares", "Avg", "Cost", "Mark", "PnL", "Market"], position_rows, "No paper positions recorded yet."),
    }

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "poll_interval_ms": POLL_INTERVAL_MS,
        "json_file": json_filename,
        "db": {
            "path": os.path.abspath(db_path),
            "name": os.path.basename(db_path),
        },
        "safety": {
            "mode": "paper_only",
            "paper_only": True,
            "wallet": False,
            "api_key": False,
            "live_trading": False,
            "order_placement": False,
            "signing": False,
            "private_key": False,
        },
        "thresholds": {
            "evidence_days": EVIDENCE_DAY_TARGET,
            "evidence_candidates": EVIDENCE_CANDIDATE_TARGET,
            "evaluation_edge": EVALUATION_EDGE_THRESHOLD,
            "evaluation_min_entry": EVALUATION_MIN_ENTRY,
            "evaluation_max_entry": EVALUATION_MAX_ENTRY,
        },
        "metrics": dict(metrics),
        "progress": dict(progress),
        "labeling_settlement": dict(label_progress),
        "research_metrics": research,
        "observed_high_latency": observed_high_latency,
        "strategy_family_survival": {
            "rows": survival_rows,
            "thresholds": {
                "resolved_target": edge_validation.RESOLVED_TARGET,
                "day_target": edge_validation.DAY_TARGET,
            },
            "weights": edge_validation.SURVIVAL_WEIGHTS,
        },
        "tuning_performance": {
            "state": tuning_state,
            "current_metrics": {
                "equity": metrics["equity"],
                "return_pct": metrics["return_pct"],
                "drawdown": metrics["drawdown"],
                "realized_pnl": metrics["realized_pnl"],
                "unrealized_pnl": metrics["unrealized_pnl"],
            },
            "traces": {
                "equity_snapshots": snapshot_data,
                "latest_runs": latest_run_data,
                "readiness_gates": tuning_state.get("gates", []),
                "proposal_trace": tuning_state.get("proposal_trace", {}),
            },
        },
        "tuning_iterations": {
            "log_path": os.path.abspath(iteration_log_path),
            "count": len(tuning_iterations),
            "iterations": tuning_iterations,
        },
        "data_sources_feature_tuning": {
            "source_families": tuning_state.get("source_families", []),
            "source_family_counts": tuning_state.get("source_family_counts", {}),
            "feature_families": tuning_state.get("feature_families", []),
            "feature_family_counts": tuning_state.get("feature_family_counts", {}),
            "feature_missingness_summary": tuning_state.get("feature_missingness_summary", {}),
            "metric_readiness": tuning_state.get("metric_readiness", {}),
            "post_labels": tuning_state.get("post_labels", {}),
        },
        "counts": dict(counts),
        "expanded_counts": dict(expanded_counts),
        "paper_buy_count": int(paper_buy_count or 0),
        "latest_runs": latest_run_data,
        "signal_mix": signal_mix_data,
        "recent_orders": order_data,
        "recent_fills": fill_data,
        "recent_positions": position_data,
        "equity_snapshots": snapshot_data,
        "fragments": fragments,
    }


def build_html_from_snapshot(snapshot: dict[str, Any]) -> str:
    generated_at = str(snapshot["generated_at"])
    db_path = str(snapshot["db"]["path"])
    json_file = str(snapshot.get("json_file") or os.path.basename(DEFAULT_JSON_OUTPUT))
    fragments = snapshot["fragments"]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Polymarket Weather Engine Performance</title>
  <style>{page_css()}</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Polymarket Weather Engine Performance</h1>
      <p class="subhead" data-live-fragment="header_subhead">{fragments["header_subhead"]}</p>
    </div>
    <div class="mode-stack" aria-label="Safety labels">
      <span class="mode-label safe">Paper-only simulation</span>
      <span class="mode-label stop">No wallet</span>
      <span class="mode-label stop">No live trading</span>
      <span class="mode-label stop">No order placement</span>
    </div>
    <div class="live-panel" aria-label="Live data status">
      <span class="connection-state embedded" id="connection-state">
        <span class="connection-dot" aria-hidden="true"></span>
        <span id="connection-text">Embedded snapshot</span>
      </span>
      <span>Last updated: <time id="last-updated" datetime="{escape(generated_at)}">{escape(generated_at)}</time></span>
      <span>Polling: <code>{POLL_INTERVAL_MS // 1000}s</code></span>
    </div>
  </header>

  <section class="notice" aria-label="Paper-only notice">
    <strong>Research mode</strong>
    <p>This report reads stored public-market observations and simulated ledger rows only. It contains no secrets, no private keys, no wallet integration, and no live-trading controls.</p>
  </section>

  <section class="grid stats" aria-label="Bankroll dashboard" data-live-fragment="bankroll_stats">
    {fragments["bankroll_stats"]}
  </section>

  <section class="grid counts" aria-label="Core row counts" data-live-fragment="count_cards">
    {fragments["count_cards"]}
  </section>

  <section class="section-grid">
    <article class="panel" data-live-fragment="evidence_progress">
      {fragments["evidence_progress"]}
    </article>

    <article class="panel">
      <div class="panel-header">
        <div>
          <h2>Equity Trail</h2>
          <p>Snapshots are generated by scans or explicit portfolio snapshots.</p>
        </div>
      </div>
      <div data-live-fragment="equity_chart">{fragments["equity_chart"]}</div>
    </article>
  </section>

  <section class="panel table-section" aria-label="Labeling and settlement progress" data-live-fragment="labeling_settlement_panel">
    {fragments["labeling_settlement_panel"]}
  </section>

  <section class="panel table-section" aria-label="Research metrics" data-live-fragment="research_metrics_panel">
    {fragments["research_metrics_panel"]}
  </section>

  <section class="panel table-section" aria-label="Observed-high latency half-life" data-live-fragment="observed_high_latency_panel">
    {fragments["observed_high_latency_panel"]}
  </section>

  <section class="panel table-section" aria-label="Tuning readiness and performance traces" data-live-fragment="tuning_section">
    {fragments["tuning_section"]}
  </section>

  <section class="panel table-section" aria-label="Strategy family survival" data-live-fragment="strategy_family_survival_panel">
    {fragments["strategy_family_survival_panel"]}
  </section>

  <section class="panel table-section" aria-label="Tuning iteration performance" data-live-fragment="tuning_iterations_section">
    {fragments["tuning_iterations_section"]}
  </section>

  <section class="panel table-section" aria-label="Data sources and feature tuning" data-live-fragment="data_sources_feature_tuning_panel">
    {fragments["data_sources_feature_tuning_panel"]}
  </section>

  <section class="panel table-section">
    <div class="panel-header">
      <div>
        <h2>Runtime Tunables</h2>
        <p>Current paper runtime values beside the proposal-only ranges declared in the goal config.</p>
      </div>
    </div>
    <div data-live-fragment="tunables_table">{fragments["tunables_table"]}</div>
  </section>

  <section class="split">
    <article class="panel">
      <div class="panel-header"><h2>Additional Counts</h2></div>
      <div data-live-fragment="additional_counts_table">{fragments["additional_counts_table"]}</div>
    </article>
    <article class="panel">
      <div class="panel-header"><h2>Signal Mix</h2></div>
      <div data-live-fragment="signal_mix_table">{fragments["signal_mix_table"]}</div>
    </article>
  </section>

  <section class="panel table-section">
    <div class="panel-header">
      <div>
        <h2>Latest Runs</h2>
        <p>Recent scanner passes and their resulting simulated ledger activity.</p>
      </div>
    </div>
    <div data-live-fragment="latest_runs_table">{fragments["latest_runs_table"]}</div>
  </section>

  <section class="panel table-section">
    <div class="panel-header">
      <div>
        <h2>Recent Paper Orders</h2>
        <p>Simulated orders only. No live CLOB placement exists in this ledger.</p>
      </div>
    </div>
    <div data-live-fragment="recent_orders_table">{fragments["recent_orders_table"]}</div>
  </section>

  <section class="panel table-section">
    <div class="panel-header">
      <div>
        <h2>Recent Paper Fills</h2>
        <p>Fills represent simulated executable entries from stored public order-book reads.</p>
      </div>
    </div>
    <div data-live-fragment="recent_fills_table">{fragments["recent_fills_table"]}</div>
  </section>

  <section class="panel table-section">
    <div class="panel-header">
      <div>
        <h2>Recent Paper Positions</h2>
        <p>Open rows are marked to the latest stored signal price. Settled rows use realized paper PnL.</p>
      </div>
    </div>
    <div data-live-fragment="recent_positions_table">{fragments["recent_positions_table"]}</div>
  </section>

  <footer>
    <p>Mode: <code>paper_only=true</code> <code>wallet=false</code> <code>live_trading=false</code> <code>order_placement=false</code>. Source database: <code>{escape(db_path)}</code>. Live refresh reads the local sidecar JSON <code>{escape(json_file)}</code>; no external JavaScript, CSS, wallet, signing, or trading controls are used.</p>
  </footer>
</main>
<script id="initial-dashboard-data" type="application/json">{script_json(snapshot)}</script>
<script>
(() => {{
  "use strict";
  const jsonUrl = {script_json(json_file)};
  const pollMs = {POLL_INTERVAL_MS};
  const stateEl = document.getElementById("connection-state");
  const textEl = document.getElementById("connection-text");
  const updatedEl = document.getElementById("last-updated");
  const initialEl = document.getElementById("initial-dashboard-data");
  let lastSource = "embedded";

  function readInitialData() {{
    try {{
      return JSON.parse(initialEl.textContent || "{{}}");
    }} catch (error) {{
      return null;
    }}
  }}

  function formatTimestamp(value) {{
    if (!value) {{
      return "-";
    }}
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {{
      return value;
    }}
    return parsed.toLocaleString(undefined, {{
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    }});
  }}

  function setConnection(state, label) {{
    stateEl.className = `connection-state ${{state}}`;
    textEl.textContent = label;
  }}

  function applySnapshot(snapshot, source) {{
    if (!snapshot || !snapshot.fragments) {{
      return;
    }}
    document.querySelectorAll("[data-live-fragment]").forEach((node) => {{
      const key = node.getAttribute("data-live-fragment");
      if (Object.prototype.hasOwnProperty.call(snapshot.fragments, key)) {{
        node.innerHTML = snapshot.fragments[key];
      }}
    }});
    if (updatedEl) {{
      updatedEl.dateTime = snapshot.generated_at || "";
      updatedEl.textContent = formatTimestamp(snapshot.generated_at);
    }}
    lastSource = source;
  }}

  async function refreshSnapshot() {{
    if (window.location.protocol === "file:") {{
      setConnection("embedded", "Embedded snapshot (file mode)");
      return;
    }}
    setConnection("polling", "Refreshing local JSON");
    try {{
      const response = await fetch(`${{jsonUrl}}?t=${{Date.now()}}`, {{ cache: "no-store" }});
      if (!response.ok) {{
        throw new Error(`HTTP ${{response.status}}`);
      }}
      const snapshot = await response.json();
      applySnapshot(snapshot, "json");
      setConnection("online", "Live JSON connected");
    }} catch (error) {{
      const label = lastSource === "json"
        ? "JSON unavailable; showing last live snapshot"
        : "JSON unavailable; using embedded snapshot";
      setConnection("error", label);
    }}
  }}

  const initialData = readInitialData();
  applySnapshot(initialData, "embedded");
  if (window.location.protocol === "file:") {{
    setConnection("embedded", "Embedded snapshot (file mode)");
  }} else {{
    refreshSnapshot();
    window.setInterval(refreshSnapshot, pollMs);
  }}
}})();
</script>
</body>
</html>
"""


def build_html(
    db: sqlite3.Connection,
    db_path: str,
    json_filename: str = os.path.basename(DEFAULT_JSON_OUTPUT),
) -> str:
    return build_html_from_snapshot(build_snapshot(db, db_path, json_filename))


def write_json_snapshot(snapshot: dict[str, Any], output_path: str) -> str:
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)
        f.write("\n")
    return output_path


def render(
    db_path: str,
    output_path: str,
    json_output_path: str | None = DEFAULT_JSON_OUTPUT,
    iteration_log_path: str = tuning_evaluator.TUNING_ITERATIONS_PATH,
) -> tuple[str, str | None]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    json_filename = os.path.basename(json_output_path) if json_output_path else os.path.basename(DEFAULT_JSON_OUTPUT)
    with connect(db_path) as db:
        snapshot = build_snapshot(db, db_path, json_filename=json_filename, iteration_log_path=iteration_log_path)
        doc = build_html_from_snapshot(snapshot)
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    if json_output_path:
        write_json_snapshot(snapshot, json_output_path)
    return output_path, json_output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the paper-only Polymarket weather performance dashboard."
    )
    parser.add_argument("--db", default=DB_PATH, help="SQLite database to read.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="HTML output path. Defaults to the brain project artifact.",
    )
    parser.add_argument(
        "--json-output",
        default=DEFAULT_JSON_OUTPUT,
        help="Sidecar JSON snapshot output path. Pass an empty string to skip JSON.",
    )
    parser.add_argument(
        "--iteration-log",
        default=tuning_evaluator.TUNING_ITERATIONS_PATH,
        help="Tuning iteration JSONL log to display.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_output = args.json_output or None
    html_output, json_output = render(args.db, args.output, json_output, args.iteration_log)
    print(f"wrote_html={html_output}")
    if json_output:
        print(f"wrote_json={json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
