import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import render_brain_performance
import scanner
import tuning_evaluator


class RenderBrainPerformanceTests(unittest.TestCase):
    def test_render_writes_live_html_and_json_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            db.execute(
                "insert into runs(started_at, source_url, markets_seen, signals_seen) values (?, ?, ?, ?)",
                ("2026-05-09T00:00:00+00:00", "https://polymarket.com/climate-science/weather", 1, 2),
            )
            db.commit()
            db.close()

            html_path = os.path.join(td, "dashboard.html")
            json_path = os.path.join(td, "dashboard.json")
            iteration_log = os.path.join(td, "tuning_iterations.jsonl")
            state = tuning_evaluator.evaluate_tuning_state(db_path=db_path)
            tuning_evaluator.record_tuning_iteration(
                state,
                iteration_log,
                timestamp="2026-05-09T00:10:00+00:00",
            )
            html_out, json_out = render_brain_performance.render(db_path, html_path, json_path, iteration_log)

            self.assertEqual(html_out, html_path)
            self.assertEqual(json_out, json_path)
            self.assertTrue(os.path.exists(html_path))
            self.assertTrue(os.path.exists(json_path))

            with open(json_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            self.assertEqual(snapshot["schema_version"], render_brain_performance.SNAPSHOT_SCHEMA_VERSION)
            self.assertEqual(snapshot["poll_interval_ms"], 30_000)
            self.assertEqual(snapshot["json_file"], "dashboard.json")
            self.assertEqual(snapshot["counts"]["markets"], 0)
            self.assertEqual(snapshot["latest_runs"][0]["markets_seen"], 1)
            self.assertTrue(snapshot["safety"]["paper_only"])
            self.assertFalse(snapshot["safety"]["wallet"])
            self.assertFalse(snapshot["safety"]["live_trading"])
            self.assertFalse(snapshot["safety"]["order_placement"])
            self.assertIn("tuning_performance", snapshot)
            tuning_state = snapshot["tuning_performance"]["state"]
            self.assertEqual(tuning_state["status"], "insufficient_data")
            self.assertTrue(tuning_state["proposal_only"])
            self.assertTrue(tuning_state["safety"]["paper_only"])
            self.assertFalse(tuning_state["safety"]["wallet"])
            self.assertFalse(tuning_state["safety"]["live_trading"])
            self.assertFalse(tuning_state["safety"]["order_placement"])
            self.assertEqual(tuning_state["evidence"]["training_rows"], 0)
            self.assertEqual(tuning_state["evidence"]["labeled_rows"], 0)
            self.assertEqual(tuning_state["evidence"]["paper_buy_rows"], 0)
            self.assertEqual(tuning_state["evidence"]["calendar_days"], 0)
            self.assertIn("training_rows", tuning_state["blocked_reasons"])
            self.assertIn("labeled_rows", tuning_state["blocked_reasons"])
            self.assertIn("calendar_days", tuning_state["blocked_reasons"])
            self.assertIn("edge_threshold", tuning_state["current_tunables"])
            self.assertIn("edge_threshold", tuning_state["allowed_tunables"])
            self.assertIn("latest_runs", snapshot["tuning_performance"]["traces"])
            self.assertIn("data_sources_feature_tuning", snapshot)
            self.assertIn("source_families", snapshot["data_sources_feature_tuning"])
            self.assertIn("feature_families", snapshot["data_sources_feature_tuning"])
            self.assertIn("post_labels", snapshot["data_sources_feature_tuning"])
            self.assertIn("labeling_settlement", snapshot)
            self.assertEqual(snapshot["labeling_settlement"]["labeled_rows"], 0)
            self.assertEqual(snapshot["labeling_settlement"]["attempts"], 0)
            self.assertIn("research_metrics", snapshot)
            self.assertIn("strategy_pnl", snapshot["research_metrics"])
            self.assertIn("edge_buckets", snapshot["research_metrics"])
            self.assertIn("calibration_by_contract", snapshot["research_metrics"])
            self.assertIn("fill_realism", snapshot["research_metrics"])
            self.assertIn("label_delay_histogram", snapshot["research_metrics"])
            self.assertIn("reaction_lag_stale_quote", snapshot["research_metrics"])
            self.assertIn("ladder_violations", snapshot["research_metrics"])
            self.assertIn("station_source_disagreement", snapshot["research_metrics"])
            self.assertIn("time_to_local_close", snapshot["research_metrics"])
            self.assertIn("rule_ambiguity_loss", snapshot["research_metrics"])
            self.assertIn("lifecycle_funnel", snapshot["research_metrics"])
            self.assertIn("event_exposure_latent_summary", snapshot["research_metrics"])
            self.assertIn("bankroll_stats", snapshot["fragments"])
            self.assertIn("tuning_section", snapshot["fragments"])
            self.assertIn("tuning_iterations_section", snapshot["fragments"])
            self.assertIn("data_sources_feature_tuning_panel", snapshot["fragments"])
            self.assertIn("labeling_settlement_panel", snapshot["fragments"])
            self.assertIn("research_metrics_panel", snapshot["fragments"])
            self.assertIn("tunables_table", snapshot["fragments"])
            self.assertIn("tuning_iterations", snapshot)
            self.assertEqual(snapshot["tuning_iterations"]["count"], 1)
            iteration = snapshot["tuning_iterations"]["iterations"][0]
            self.assertEqual(iteration["status"], "insufficient_data")
            self.assertTrue(iteration["persisted"])
            self.assertTrue(iteration["safety_ok"])
            self.assertFalse(iteration["safety"]["live_trading"])
            self.assertFalse(iteration["approval"]["order_placement"])
            self.assertEqual(iteration["evidence_counts"]["labeled_rows"], 0)
            self.assertEqual(iteration["proposed_tunables"], {})
            self.assertIn("available_performance_metrics", iteration)

            with open(html_path, encoding="utf-8") as f:
                html = f.read()
            self.assertIn('id="initial-dashboard-data"', html)
            self.assertIn('window.location.protocol === "file:"', html)
            self.assertIn("Embedded snapshot (file mode)", html)
            self.assertIn("window.setInterval(refreshSnapshot, pollMs)", html)
            self.assertIn("No live trading", html)
            self.assertIn("No order placement", html)
            self.assertIn("Tuning Readiness and Performance Trace", html)
            self.assertIn("Tuning Iteration Performance", html)
            self.assertIn("Labeling and Settlement Progress", html)
            self.assertIn("Research Metrics", html)
            self.assertIn("Calibration By Contract", html)
            self.assertIn("Logged locally as JSONL", html)
            self.assertIn("Paper-only, proposal-only", html)
            self.assertIn("Runtime Tunables", html)
            self.assertIn("Data Sources &amp; Feature Tuning", html)
            self.assertIn("Read-only and cached", html)
            self.assertIn("approved-for-paper-forward-test", html)
            self.assertIn("Allowed Proposal Values", html)
            self.assertIn("Insufficient data", html)
            self.assertIn("insufficient_data", html)
            self.assertIn("live=false", html)
            self.assertIn("safety_ok=true", html)
            self.assertIn("dashboard.json", html)

    def test_render_uses_readonly_connection_while_writer_is_active(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            writer = scanner.init_db(db_path)
            writer.execute(
                "insert into runs(started_at, source_url, markets_seen, signals_seen) values (?, ?, ?, ?)",
                ("2026-05-09T00:00:00+00:00", "https://polymarket.com/climate-science/weather", 1, 2),
            )
            writer.execute("begin immediate")
            try:
                html_path = os.path.join(td, "dashboard.html")
                json_path = os.path.join(td, "dashboard.json")
                html_out, json_out = render_brain_performance.render(db_path, html_path, json_path, os.path.join(td, "iterations.jsonl"))
            finally:
                writer.execute("rollback")
                writer.close()

            self.assertEqual(html_out, html_path)
            self.assertEqual(json_out, json_path)
            with open(json_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            self.assertEqual(snapshot["latest_runs"][0]["markets_seen"], 1)

    def test_dashboard_counts_source_status_reasons(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            for provider, status in (("nws", "error"), ("iem_metar", "missing_daily_high"), ("noaa_ncei", "ok")):
                scanner.record_source_observation_snapshot(
                    db,
                    run_id=1,
                    market_id="m-source",
                    event_key="event-source",
                    provider=provider,
                    family="observation",
                    status=status,
                    station_id="KDEN",
                    fetched_at="2026-05-10T12:00:00+00:00",
                    error="simulated" if status == "error" else None,
                )
            db.close()

            html_path = os.path.join(td, "dashboard.html")
            json_path = os.path.join(td, "dashboard.json")
            render_brain_performance.render(db_path, html_path, json_path, os.path.join(td, "iterations.jsonl"))
            with open(json_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            rows = snapshot["research_metrics"]["source_adapter_status"]["by_status"]
            self.assertEqual({(row["source_provider"], row["status"]) for row in rows}, {("nws", "error"), ("iem_metar", "missing_daily_high"), ("noaa_ncei", "ok")})


if __name__ == "__main__":
    unittest.main()
