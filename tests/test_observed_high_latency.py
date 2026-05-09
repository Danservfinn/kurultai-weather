from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest

import clob_hot_cache
import hot_state_watcher
import latency_metrics
import render_brain_performance
import scanner
import touch_watchlist


def temp_db() -> tuple[str, sqlite3.Connection]:
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    return path, scanner.init_db(path)


def insert_watch(db: sqlite3.Connection, *, current_high: float = 81.0, active: int = 1) -> None:
    db.execute(
        """
        insert into touch_watchlist(
          event_key, market_id, token_id, city, station_id, target_date,
          strategy_family, contract_type, threshold_f, side, current_high_f,
          distance_to_threshold_f, local_hour, hotness_score, hot_reason,
          watch_started_at, active
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "austin-2026-05-09",
            "m1",
            "t1",
            "Austin",
            "KAUS",
            "2026-05-09",
            "latency_absorbing_state",
            "threshold",
            82.0,
            "yes",
            current_high,
            82.0 - current_high,
            14,
            0.8,
            "within_1f_of_threshold",
            "2026-05-09T12:00:00+00:00",
            active,
        ),
    )
    db.commit()


class ObservedHighLatencyTests(unittest.TestCase):
    def test_init_db_creates_touch_latency_tables_and_indexes(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        names = {
            row[0]
            for row in db.execute(
                "select name from sqlite_master where type in ('table','index') and name like '%touch%' or name like 'idx_post_touch%'"
            )
        }
        db.close()
        self.assertIn("touch_watchlist", names)
        self.assertIn("threshold_touch_events", names)
        self.assertIn("post_touch_repricing", names)
        self.assertIn("idx_touch_watchlist_active", names)
        self.assertIn("idx_threshold_touch_events_event", names)
        self.assertIn("idx_post_touch_repricing_touch", names)

    def test_hotness_activates_near_threshold_and_upsert_is_idempotent(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        candidate = {
            "event_key": "e1",
            "market_id": "m1",
            "token_id": "t1",
            "city": "Austin",
            "station_id": "KAUS",
            "target_date": "2026-05-09",
            "threshold_f": 82.0,
            "side": "yes",
            "source_confidence": "high",
        }
        hotness = touch_watchlist.compute_hotness_score(
            candidate,
            {"high_so_far_f": 81.2, "current_temp_rising": True},
            {"ask": 0.80, "bid": 0.78, "depth": 5.0, "depth_sufficient": True},
            {"local_hour": 14},
        )
        self.assertEqual(hotness["active"], 1)
        self.assertIn("within_1f_of_threshold", hotness["hot_reason"])
        touch_watchlist.upsert_touch_watchlist(db, candidate, hotness)
        touch_watchlist.upsert_touch_watchlist(db, candidate, hotness)
        db.commit()
        count, active = db.execute("select count(*), active from touch_watchlist").fetchone()
        db.close()
        self.assertEqual(count, 1)
        self.assertEqual(active, 1)

    def test_hot_book_cache_updates_best_ask_depth_and_age(self) -> None:
        now = dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.timezone.utc)
        cache = clob_hot_cache.HotBookCache(now_fn=lambda: now)
        book = cache.update_from_message(
            {
                "token_id": "t1",
                "timestamp": "2026-05-09T11:59:30+00:00",
                "bids": [{"price": "0.70", "size": "4"}],
                "asks": [{"price": "0.82", "size": "3"}, {"price": "0.84", "size": "2"}],
            }
        )
        self.assertEqual(book["best_ask"], 0.82)
        self.assertEqual(book["ask_depth"], 3.0)
        self.assertEqual(cache.get_book("t1")["quote_age_seconds"], 30.0)

    def test_source_missing_does_not_crash_watcher(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_watch(db)
        stats = hot_state_watcher.process_once(db, observation_provider=lambda row: None, now="2026-05-09T12:00:00+00:00")
        self.assertEqual(stats["source_missing"], 1)
        self.assertEqual(db.execute("select count(*) from threshold_touch_events").fetchone()[0], 0)
        db.close()

    def test_threshold_touch_records_event(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_watch(db)
        obs = {
            "high_so_far_f": 82.0,
            "source_provider": "fixture",
            "confidence_class": "direct_settlement_source",
            "raw_status": "ok",
            "observed_at": "2026-05-09T11:59:50+00:00",
            "fetched_at": "2026-05-09T12:00:00+00:00",
            "settlement_source_match": 1,
        }
        stats = hot_state_watcher.process_once(db, observation_provider=lambda row: obs, now="2026-05-09T12:00:05+00:00")
        row = db.execute("select observed_high_f, detection_delay_seconds, confidence_class from threshold_touch_events").fetchone()
        db.close()
        self.assertEqual(stats["touch_events"], 1)
        self.assertEqual(tuple(row), (82.0, 15.0, "direct_settlement_source"))

    def test_stale_missing_book_skips_signal(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_watch(db)
        obs = {
            "high_so_far_f": 82.0,
            "source_provider": "fixture",
            "confidence_class": "direct_settlement_source",
            "raw_status": "ok",
            "observed_at": "2026-05-09T12:00:00+00:00",
            "fetched_at": "2026-05-09T12:00:00+00:00",
            "settlement_source_match": 1,
        }
        stale_book = {
            "best_ask": 0.80,
            "best_bid": 0.78,
            "spread": 0.02,
            "ask_depth": 5.0,
            "depth_sufficient": True,
            "quote_age_seconds": 61.0,
            "stale_book_flag": 0,
            "execution_source": "clob_book",
        }
        stats = hot_state_watcher.process_once(
            db,
            observation_provider=lambda row: obs,
            book_provider=lambda row: stale_book,
            now="2026-05-09T12:00:00+00:00",
        )
        self.assertEqual(stats["signals_allowed"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(db.execute("select count(*) from post_touch_repricing").fetchone()[0], 1)
        db.close()

    def test_latency_delay_bucket_assignment_and_empty_panel_rendering(self) -> None:
        self.assertEqual(latency_metrics.delay_bucket(29.9), "0-30s")
        self.assertEqual(latency_metrics.delay_bucket(30), "30-60s")
        self.assertEqual(latency_metrics.delay_bucket(60), "1-2m")
        self.assertEqual(latency_metrics.delay_bucket(900), "15m+")
        panel = render_brain_performance.render_observed_high_latency_panel(latency_metrics.empty_latency_metrics())
        self.assertIn("Observed-High Latency Half-Life", panel)
        self.assertIn("No observed-high threshold touches recorded yet.", panel)

    def test_dashboard_snapshot_includes_observed_high_latency(self) -> None:
        path, db = temp_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        db.row_factory = sqlite3.Row
        snapshot = render_brain_performance.build_snapshot(db, path, json_filename="dashboard.json")
        db.close()
        self.assertIn("observed_high_latency", snapshot)
        self.assertIn("observed_high_latency_panel", snapshot["fragments"])

    def test_no_live_trading_guard_remains_healthy(self) -> None:
        self.assertTrue(scanner.ensure_paper_only_guard(None))


if __name__ == "__main__":
    unittest.main()
