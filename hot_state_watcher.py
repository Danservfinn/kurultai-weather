#!/usr/bin/env python3
"""Paper-only observed-high threshold touch watcher.

The default providers are fail-soft and do not start network loops. Tests can
inject observations and public books directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import time
from typing import Any, Callable, Optional

import edge_validation
import scanner
import settlement_states
from clob_hot_cache import HotBookCache


ObservationProvider = Callable[[sqlite3.Row], Optional[dict[str, Any]]]
BookProvider = Callable[[sqlite3.Row], Optional[dict[str, Any]]]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def default_observation_provider(row: sqlite3.Row) -> dict[str, Any] | None:
    return {
        "high_so_far_f": row["current_high_f"] if "current_high_f" in row.keys() else None,
        "source_provider": "watchlist_snapshot",
        "confidence_class": "source_missing",
        "raw_status": "source_missing",
        "observed_at": None,
        "fetched_at": utc_now_iso(),
        "settlement_source_match": 0,
    }


def default_book_provider(row: sqlite3.Row) -> dict[str, Any] | None:
    return None


def active_watchlist_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    if not db.execute("select 1 from sqlite_master where type='table' and name='touch_watchlist'").fetchone():
        return []
    return list(db.execute("select * from touch_watchlist where active=1 order by hotness_score desc, watch_started_at"))


def settlement_state_for_watch(row: sqlite3.Row, observed_high_f: float | None) -> settlement_states.SettlementState:
    side = settlement_states.SIDE_NO if str(row["side"] or "").lower() == "no" else settlement_states.SIDE_YES
    threshold = float(row["threshold_f"])
    spec = settlement_states.ContractSpec(
        settlement_states.CONTRACT_THRESHOLD,
        side=side,
        low_f=threshold,
        high_f=None,
        threshold_f=threshold,
        threshold_direction="gte",
        label="hot_state_watch",
        bucket_kind="open_above_inclusive",
    )
    return settlement_states.settlement_state(spec, observed_high_f, local_day_complete=False)


def record_threshold_touch_event(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    obs: dict[str, Any],
    state: settlement_states.SettlementState,
    detected_at: str,
) -> int:
    fetched_at = obs.get("fetched_at") or detected_at
    source_observed_at = obs.get("observed_at")
    fetched_dt = parse_iso(fetched_at)
    observed_dt = parse_iso(source_observed_at)
    detected_dt = parse_iso(detected_at)
    source_age = (fetched_dt - observed_dt).total_seconds() if fetched_dt and observed_dt else None
    detection_delay = (detected_dt - observed_dt).total_seconds() if detected_dt and observed_dt else None
    cur = db.execute(
        """
        insert into threshold_touch_events(
          event_key, market_id, token_id, city, station_id, target_date,
          contract_type, side, threshold_f, observed_high_f, source_provider,
          source_observed_at, source_fetched_at, scanner_detected_at,
          source_age_seconds, detection_delay_seconds, settlement_source_match,
          confidence_class, raw_status, created_at
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["event_key"],
            row["market_id"],
            row["token_id"],
            row["city"],
            row["station_id"],
            row["target_date"],
            state.contract_type,
            state.side,
            row["threshold_f"],
            obs.get("high_so_far_f"),
            obs.get("source_provider"),
            source_observed_at,
            fetched_at,
            detected_at,
            source_age,
            detection_delay,
            1 if obs.get("settlement_source_match") else 0,
            obs.get("confidence_class"),
            obs.get("raw_status", state.reason),
            detected_at,
        ),
    )
    return int(cur.lastrowid)


def record_repricing_snapshot(
    db: sqlite3.Connection,
    touch_event_id: int,
    row: sqlite3.Row,
    book: dict[str, Any] | None,
    observed_at: str,
    seconds_after_touch: float = 0.0,
    source: str = "hot_state_watcher",
) -> None:
    book = book or {}
    bid = book.get("best_bid", book.get("bid"))
    ask = book.get("best_ask", book.get("ask"))
    spread = book.get("spread")
    if spread is None and bid is not None and ask is not None:
        spread = float(ask) - float(bid)
    midpoint = book.get("midpoint")
    if midpoint is None and bid is not None and ask is not None:
        midpoint = (float(bid) + float(ask)) / 2.0
    db.execute(
        """
        insert into post_touch_repricing(
          touch_event_id, event_key, market_id, token_id, observed_at,
          seconds_after_touch, best_bid, best_ask, spread, ask_depth,
          midpoint, last_trade_price, book_age_seconds, source
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            touch_event_id,
            row["event_key"],
            row["market_id"],
            row["token_id"],
            observed_at,
            seconds_after_touch,
            bid,
            ask,
            spread,
            book.get("ask_depth", book.get("depth")),
            midpoint,
            book.get("last_trade_price"),
            book.get("quote_age_seconds", book.get("book_age_seconds")),
            source,
        ),
    )


def should_record_signal(obs: dict[str, Any], book: dict[str, Any] | None, state: settlement_states.SettlementState, *, min_edge: float) -> tuple[bool, str]:
    confidence = str(obs.get("confidence_class") or "").lower()
    if confidence not in {"direct_settlement_source", "official_proxy_source"}:
        return False, "source_not_confident"
    if not edge_validation.quote_is_fresh(book or {}, "latency_absorbing_state"):
        return False, "skip_touch_quote_stale"
    if not bool((book or {}).get("depth_sufficient")):
        return False, "skip_touch_depth_missing"
    ask = (book or {}).get("best_ask", (book or {}).get("ask"))
    if ask is None:
        return False, "skip_touch_depth_missing"
    implied = state.payout if state.payout is not None else 1.0
    if float(implied) - float(ask) < min_edge:
        return False, "skip_touch_edge_too_small"
    return True, "paper_signal_allowed"


def process_once(
    db: sqlite3.Connection,
    *,
    observation_provider: ObservationProvider = default_observation_provider,
    book_provider: BookProvider = default_book_provider,
    cache: HotBookCache | None = None,
    min_edge: float = 0.04,
    now: str | None = None,
) -> dict[str, int]:
    scanner.ensure_paper_only_guard(None)
    cache = cache or HotBookCache()
    detected_at = now or utc_now_iso()
    stats = {"watchlist": 0, "source_missing": 0, "touch_events": 0, "repricing_snapshots": 0, "signals_allowed": 0, "skipped": 0}
    for row in active_watchlist_rows(db):
        stats["watchlist"] += 1
        obs = observation_provider(row) or {}
        high = obs.get("high_so_far_f")
        db.execute(
            "update touch_watchlist set last_source_poll_at=?, current_high_f=? where event_key=? and market_id=? and token_id is ?",
            (detected_at, high, row["event_key"], row["market_id"], row["token_id"]),
        )
        if high is None:
            stats["source_missing"] += 1
            continue
        state = settlement_state_for_watch(row, float(high))
        if not state.absorbing:
            continue
        touch_id = record_threshold_touch_event(db, row, obs, state, detected_at)
        stats["touch_events"] += 1
        book = cache.get_book(row["token_id"]) or book_provider(row)
        if book is not None:
            record_repricing_snapshot(db, touch_id, row, book, detected_at)
            stats["repricing_snapshots"] += 1
            db.execute(
                "update touch_watchlist set last_book_update_at=?, last_seen_ask=?, last_seen_bid=?, last_seen_depth=? where event_key=? and market_id=? and token_id is ?",
                (
                    detected_at,
                    book.get("best_ask", book.get("ask")),
                    book.get("best_bid", book.get("bid")),
                    book.get("ask_depth", book.get("depth")),
                    row["event_key"],
                    row["market_id"],
                    row["token_id"],
                ),
            )
        ok, _reason = should_record_signal(obs, book, state, min_edge=min_edge)
        if ok:
            stats["signals_allowed"] += 1
        else:
            stats["skipped"] += 1
    db.commit()
    return stats


def empty_stats(**overrides: int) -> dict[str, int]:
    stats = {"watchlist": 0, "source_missing": 0, "touch_events": 0, "repricing_snapshots": 0, "signals_allowed": 0, "skipped": 0}
    stats.update(overrides)
    return stats


def is_sqlite_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def process_once_resilient(
    db: sqlite3.Connection,
    *,
    min_edge: float = 0.04,
    processor: Callable[..., dict[str, int]] = process_once,
) -> dict[str, int]:
    try:
        return processor(db, min_edge=min_edge)
    except sqlite3.OperationalError as exc:
        if not is_sqlite_locked_error(exc):
            raise
        db.rollback()
        return empty_stats(sqlite_locked=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-only hot observed-high threshold watcher.")
    parser.add_argument("--db", default=scanner.DB_PATH)
    parser.add_argument("--once", action="store_true", help="Process active watchlist once and exit.")
    parser.add_argument("--loop", action="store_true", help="Poll active watchlist repeatedly.")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--min-edge", type=float, default=0.04)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.once and not args.loop:
        args.once = True
    db = scanner.init_db(args.db)
    try:
        while True:
            stats = process_once_resilient(db, min_edge=args.min_edge)
            print(" ".join(f"{key}={value}" for key, value in stats.items()))
            if args.once:
                break
            time.sleep(max(1.0, float(args.poll_seconds)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
