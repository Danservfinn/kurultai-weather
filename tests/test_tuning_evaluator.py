import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scanner
import tuning_evaluator
import truth_tiers


GOAL_TEXT = """name: paper_weather_edge_v1
mode: paper_only
paper_only: true
primary_metric: realized_return_pct
secondary_metrics: brier_score, max_drawdown, unresolved_rate
minimum_training_rows: 3
minimum_labeled_rows: 3
minimum_calendar_days: 3
validation_method: date_walk_forward
promotion: propose_only
guardrails:
  live_trading: false
  wallet_required: false
  order_placement: false
  live_money_deployment: false
  max_drawdown: 0.20
allowed_tunables:
  edge_threshold: [0.06, 0.08]
"""


class TuningEvaluatorTests(unittest.TestCase):
    def test_final_label_where_counts_proxy_consensus_and_official_final_statuses(self):
        statuses_sql = tuning_evaluator.FINAL_LABEL_STATUSES_SQL
        for status in truth_tiers.FINAL_LABEL_OUTCOME_STATUSES:
            self.assertIn(repr(status), statuses_sql)
        self.assertIn("label_status in", tuning_evaluator.FINAL_LABEL_WHERE)
        self.assertIn("label_value in (0, 1)", tuning_evaluator.FINAL_LABEL_WHERE)

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            rows = [
                ("final", 1.0),
                ("final_official", 0.0),
                ("final_proxy_consensus", 1.0),
                ("multi_provider_proxy_consensus", 0.0),
                ("single_provider_proxy", 1.0),
                (None, None),
            ]
            for idx, (status, value) in enumerate(rows, start=1):
                db.execute(
                    """
                    insert into training_rows(created_at, market_id, outcome, target_date, label_status, label_value)
                    values(?,?,?,?,?,?)
                    """,
                    (f"2026-05-{idx:02d}T00:00:00+00:00", f"m{idx}", "Yes", f"2026-05-{idx:02d}", status, value),
                )
            db.commit()
            labeled_rows = db.execute(f"select count(*) from training_rows where {tuning_evaluator.FINAL_LABEL_WHERE}").fetchone()[0]
            db.close()

        self.assertEqual(labeled_rows, 4)

    def test_post_label_paper_forward_gate_never_approves_live_trading(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            goal_path = os.path.join(td, "goal.yaml")
            runtime_path = os.path.join(td, "runtime.env")
            with open(goal_path, "w", encoding="utf-8") as f:
                f.write(GOAL_TEXT)
            with open(runtime_path, "w", encoding="utf-8") as f:
                f.write("ENABLE_WU=0\nENABLE_NWS=1\nFORECAST_CACHE_TTL_SECONDS=1800\n")

            db = scanner.init_db(db_path)
            for index, day in enumerate(("2026-05-01", "2026-05-02", "2026-05-03"), start=1):
                db.execute(
                    """
                    insert into training_rows(
                      created_at, run_id, signal_id, market_id, title, outcome, token_id,
                      city, target_date, station_id, station_source, source_url, provider,
                      forecast_snapshot_id, observation_id, orderbook_snapshot_id, market_family,
                      eligibility_class, source_confidence, bucket_lo_f, bucket_hi_f, bucket_kind,
                      bucket_state, market_prob, model_prob, entry_price, bid, ask, spread, depth,
                      depth_sufficient, edge, required_edge, uncertainty_margin, ease_score,
                      signal_type, reason, features_json, label_status, label_value, label_source, labeled_at
                    )
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"{day}T12:00:00+00:00",
                        index,
                        index,
                        f"m{index}",
                        "Denver high temperature",
                        "80F+",
                        f"t{index}",
                        "Denver",
                        day,
                        "KDEN",
                        "station_registry",
                        "https://example.com/rules",
                        "open_meteo",
                        index,
                        index,
                        index,
                        "daily_temperature",
                        "clean_station",
                        "high",
                        79.5,
                        None,
                        "open_above_inclusive",
                        "still_possible",
                        0.40,
                        0.55,
                        0.44,
                        0.41,
                        0.44,
                        0.03,
                        8.0,
                        1,
                        0.11,
                        0.08,
                        0.03,
                        9.0,
                        "paper_buy_forecast_distribution",
                        "passes paper filters",
                        '{"source_quality_score": 1, "no_lookahead_enforced": true}',
                        "final",
                        1.0,
                        "ncei_daily",
                        f"{day}T23:00:00+00:00",
                    ),
                )
                db.execute(
                    "insert into signals(run_id, market_id, title, outcome, created_at, ladder_diagnostic) values(?,?,?,?,?,?)",
                    (index, f"m{index}", "Denver high temperature", "80F+", f"{day}T12:00:00+00:00", "ladder_ok"),
                )
            db.execute(
                """
                insert into paper_account_snapshots(account_id, run_id, captured_at, cash, open_exposure, realized_pnl, unrealized_pnl, equity, return_pct, drawdown, unresolved_positions)
                values(1,1,'2026-05-03T12:00:00+00:00',1000,0,0,0,1000,0,0,0)
                """
            )
            db.commit()
            db.close()

            state = tuning_evaluator.evaluate_tuning_state(db_path, goal_path, runtime_path)

            self.assertEqual(state["status"], "approved_for_paper_forward_test")
            self.assertTrue(state["post_labels"]["approved_for_paper_forward_test"])
            self.assertFalse(state["post_labels"]["live_trading_approval"])
            self.assertFalse(state["safety"]["order_placement"])
            active_sources = {row["key"] for row in state["source_families"] if row["status"] == "active"}
            optional_sources = {row["key"] for row in state["source_families"] if row["status"] == "optional"}
            self.assertIn("ncei_daily_labels", active_sources)
            self.assertIn("nws", optional_sources)
            self.assertIn("metric_readiness", state)
            self.assertTrue(state["metric_readiness"]["paper_forward_test"]["ready"])

    def test_runtime_tunables_parse_source_flags(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("ENABLE_NWS=1\nENABLE_WU=0\nALLOW_PAID_PROVIDER_FEATURES=false\nHTTP_TIMEOUT_SECONDS=6\n")
            path = f.name
        try:
            values = tuning_evaluator.load_runtime_tunables(path)
        finally:
            os.unlink(path)

        self.assertTrue(values["enable_nws"])
        self.assertFalse(values["enable_wu"])
        self.assertFalse(values["allow_paid_provider_features"])
        self.assertEqual(values["http_timeout_seconds"], 6)

    def test_bounded_training_text_match_count_samples_large_feature_json_scans(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            for index, payload in enumerate(
                (
                    '{"source_quality_score": 1}',
                    '{"safe": true}',
                    '{"label_value": 1}',
                ),
                start=1,
            ):
                db.execute(
                    """
                    insert into training_rows(
                      created_at, run_id, signal_id, market_id, title, outcome, token_id,
                      city, target_date, features_json
                    ) values(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"2026-05-0{index}T12:00:00+00:00",
                        index,
                        index,
                        f"m{index}",
                        "Denver high temperature",
                        "80F+",
                        f"t{index}",
                        "Denver",
                        f"2026-05-0{index}",
                        payload,
                    ),
                )
            db.commit()

            count, sampled = tuning_evaluator.bounded_training_text_match_count(
                db,
                "coalesce(features_json,'') like '%label_value%'",
                total_rows=3,
                exact_row_limit=1,
                sample_rows=2,
            )
            db.close()

            self.assertTrue(sampled)
            self.assertEqual(count, 1)

    def test_iteration_jsonl_records_insufficient_data_and_sanitizes_unsafe_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            goal_path = os.path.join(td, "goal.yaml")
            runtime_path = os.path.join(td, "runtime.env")
            log_path = os.path.join(td, "iterations.jsonl")
            with open(goal_path, "w", encoding="utf-8") as f:
                f.write(GOAL_TEXT)
            with open(runtime_path, "w", encoding="utf-8") as f:
                f.write("EDGE_THRESHOLD=0.08\n")
            db = scanner.init_db(db_path)
            db.close()

            state = tuning_evaluator.evaluate_tuning_state(db_path, goal_path, runtime_path)
            record = tuning_evaluator.record_tuning_iteration(
                state,
                log_path,
                timestamp="2026-05-09T00:00:00+00:00",
            )

            self.assertEqual(record["status"], "insufficient_data")
            self.assertEqual(record["evidence_counts"]["labeled_rows"], 0)
            self.assertEqual(record["proposed_tunables"], {})
            self.assertIn("edge_threshold", record["current_tunables"])
            self.assertIn("available_performance_metrics", record)
            self.assertIn("labeling", record)
            self.assertFalse(record["labeling"]["labels_available"])
            self.assertEqual(record["labeling"]["paper_settlements"], 0)
            self.assertFalse(record["approval"]["live_trading_approval"])
            self.assertFalse(record["approval"]["order_placement"])
            self.assertTrue(record["safety_ok"])

            unsafe = dict(record)
            unsafe["id"] = "unsafe"
            unsafe["safety"] = dict(record["safety"], live_trading=True)
            unsafe["approval"] = dict(record["approval"], order_placement=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(unsafe, sort_keys=True) + "\n")

            loaded = tuning_evaluator.load_tuning_iterations(log_path, limit=10)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["status"], "insufficient_data")
            self.assertEqual(loaded[1]["status"], "rejected_unsafe_log_entry")
            self.assertFalse(loaded[1]["safety"]["live_trading"])
            self.assertFalse(loaded[1]["approval"]["order_placement"])
            self.assertFalse(loaded[1]["safety_ok"])


if __name__ == "__main__":
    unittest.main()
