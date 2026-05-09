import argparse
import contextlib
import csv
import io
import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scanner


class WeatherEdgeTests(unittest.TestCase):
    def test_bucket_bounds_fahrenheit_range_not_negative(self):
        self.assertEqual(scanner.bucket_bounds("81-82F"), (81.0, 82.0))
        self.assertEqual(scanner.bucket_bounds("81-82°F"), (81.0, 82.0))

    def test_bucket_bounds_single_celsius(self):
        lo, hi = scanner.bucket_bounds("19°C")
        self.assertAlmostEqual(lo, 65.3, places=1)
        self.assertAlmostEqual(hi, 67.1, places=1)

    def test_market_family_and_ease(self):
        family = scanner.market_family("Highest temperature in London on May 8?")
        self.assertEqual(family, "daily_temperature")
        score = scanner.ease_score(family, "high", 0.02, "clob_book")
        self.assertGreaterEqual(score, 8.0)

    def test_signal_requires_edge_over_spread_and_uncertainty(self):
        args = argparse.Namespace(edge_threshold=0.02, max_spread=0.10, max_entry=0.95)
        sig, reason = scanner.classify_signal(
            edge=0.03,
            entry_price=0.44,
            source_conf="high",
            spread=0.04,
            threshold_status="unknown",
            args=args,
            uncertainty_margin=0.02,
            market_family_name="daily_temperature",
            ease=9.0,
        )
        self.assertEqual(sig, "watch")
        self.assertIn("required_edge", reason)

    def test_touched_threshold_signal_class(self):
        args = argparse.Namespace(edge_threshold=0.02, max_spread=0.10, max_entry=0.95)
        sig, reason = scanner.classify_signal(
            edge=0.20,
            entry_price=0.70,
            source_conf="high",
            spread=0.01,
            threshold_status="already_touched",
            args=args,
            uncertainty_margin=0.02,
            market_family_name="daily_temperature",
            ease=9.0,
        )
        self.assertEqual(sig, "paper_buy_touched_threshold")

    def test_ladder_diagnostics_flags_gaps(self):
        rows = [
            {"outcome": "80F", "model_prob": 0.20, "entry_price": 0.20},
            {"outcome": "81F", "model_prob": 0.35, "entry_price": 0.10},
            {"outcome": "82F", "model_prob": 0.25, "entry_price": 0.25},
        ]
        diag = scanner.ladder_diagnostics(rows)
        self.assertIn("underpriced_bucket", diag)

    def test_parser_unicode_negative_and_tail_buckets(self):
        negative = scanner.parse_bucket("−5–−1°F")
        self.assertIsNotNone(negative)
        self.assertEqual((negative.low_int, negative.high_int), (-5, -1))
        self.assertAlmostEqual(negative.lo, -5.5)
        self.assertAlmostEqual(negative.hi, -0.5)

        celsius = scanner.parse_bucket("≤ −10℃")
        self.assertIsNotNone(celsius)
        self.assertEqual(celsius.unit, "C")
        self.assertEqual(celsius.kind, "open_below_inclusive")

        tail = scanner.parse_bucket("90°F+")
        self.assertIsNotNone(tail)
        self.assertEqual(tail.kind, "open_above_inclusive")
        self.assertEqual(tail.low_int, 90)
        self.assertTrue(math.isinf(tail.hi))

    def test_portfolio_ledger_records_simulated_fill(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                max_position_pct=0.02,
                max_city_date_pct=0.10,
                max_open_exposure_pct=0.50,
                min_fill_shares=1.0,
            )
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.40,
                "ask": 0.40,
                "depth": 10.0,
                "depth_sufficient": True,
                "raw_status": "ok",
            }
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=1,
                created_at="2026-05-09T00:00:00+00:00",
                m={"market_id": "m1", "title": "Highest temperature in Austin on May 9?"},
                outcome="90°F+",
                token_id="token-1",
                city="Austin",
                target_date="2026-05-09",
                signal_type="paper_buy_forecast_distribution",
                quote=quote,
            )
            metrics = scanner.portfolio_metrics(db)
            self.assertAlmostEqual(metrics["cash"], 998.0)
            self.assertAlmostEqual(metrics["open_exposure"], 2.0)
            self.assertEqual(metrics["unresolved_positions"], 1)
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 1)

    def test_training_row_export_writes_csv(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            db.execute(
                """
                insert into training_rows(created_at, run_id, signal_id, market_id, title, outcome, signal_type, features_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("2026-05-09T00:00:00+00:00", 7, 8, "m1", "Weather market", "90°F+", "watch", "{}"),
            )
            out = os.path.join(td, "training.csv")
            count = scanner.export_training_rows(db, out)
            self.assertEqual(count, 1)
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["market_id"], "m1")
            self.assertEqual(rows[0]["outcome"], "90°F+")

    def test_training_features_preserve_no_lookahead_and_no_labels(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.42,
                "bid": 0.40,
                "ask": 0.42,
                "spread": 0.02,
                "depth": 5.0,
                "depth_sufficient": True,
                "raw_status": "ok",
            }
            bucket = scanner.parse_bucket("80°F+")
            row_id = scanner.record_training_row(
                db,
                1,
                1,
                "2026-05-09T12:00:00+00:00",
                {
                    "market_id": "m1",
                    "title": "Highest temperature in Denver on May 9?",
                    "source_url": "https://example.com",
                    "source_host": "example.com",
                    "station_id": "KDEN",
                    "forecast_high_f": 83.0,
                    "observed_high_f": 78.0,
                },
                "80°F+",
                "token-1",
                "Denver",
                "2026-05-09",
                "KDEN",
                "station_registry",
                1,
                1,
                1,
                bucket,
                "still_possible",
                quote,
                0.40,
                0.58,
                0.16,
                "paper_buy_forecast_distribution",
                "passes paper filters",
                "high",
                "daily_temperature",
                "clean_station",
                9.0,
                0.03,
                0.08,
            )
            payload = json.loads(db.execute("select features_json from training_rows where id=?", (row_id,)).fetchone()[0])
            self.assertTrue(payload["paper_only"])
            self.assertTrue(payload["no_lookahead_enforced"])
            self.assertEqual(payload["excluded_future_source_count"], 0)
            self.assertNotIn("label_value", json.dumps(payload))
            self.assertEqual(payload["settlement_station_id_normalized"], "KDEN")

    def test_goal_tuning_scaffold_guardrails(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "goal.yaml")
            self.assertTrue(scanner.write_default_goal(path))
            text, exists = scanner.load_goal_text(path)
            self.assertTrue(exists)
            self.assertTrue(scanner.goal_guardrails_ok(text))
            self.assertIn("promotion: propose_only", text)

    def test_tune_command_appends_safe_iteration_log(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            goal_path = os.path.join(td, "goal.yaml")
            log_path = os.path.join(td, "iterations.jsonl")
            db = scanner.init_db(db_path)
            db.close()
            self.assertTrue(scanner.write_default_goal(goal_path))

            args = argparse.Namespace(
                db=db_path,
                goal=goal_path,
                init_goal=False,
                overwrite_goal=False,
                iteration_log=log_path,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                scanner.tune(args)

            with open(log_path, encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "insufficient_data")
            self.assertEqual(records[0]["evidence_counts"]["training_rows"], 0)
            self.assertTrue(records[0]["safety"]["paper_only"])
            self.assertFalse(records[0]["safety"]["live_trading"])
            self.assertFalse(records[0]["approval"]["order_placement"])

    def test_no_live_trading_guard_blocks_prohibited_options(self):
        self.assertTrue(scanner.ensure_paper_only_guard(argparse.Namespace()))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(live_trading=True))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(private_key="not-allowed"))


if __name__ == "__main__":
    unittest.main()
