import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import render_brain_performance
import scanner
import tuning_evaluator


class RenderBrainPerformanceTests(unittest.TestCase):
    def test_runtime_tunables_env_selects_clean_account_for_direct_render_use(self):
        old_account_name = os.environ.pop("PAPER_ACCOUNT_NAME", None)
        try:
            self.assertEqual(render_brain_performance.active_paper_account_name(), "paper_account_v2_clean_post_gate")
        finally:
            if old_account_name is not None:
                os.environ["PAPER_ACCOUNT_NAME"] = old_account_name

    def test_dashboard_surfaces_clean_account_idle_reason(self):
        old_account_name = os.environ.get("PAPER_ACCOUNT_NAME")
        os.environ["PAPER_ACCOUNT_NAME"] = "paper_account_v2_clean_post_gate"
        try:
            with tempfile.TemporaryDirectory() as td:
                db_path = os.path.join(td, "paper.sqlite3")
                db = scanner.init_db(db_path)
                clean_account_id = db.execute(
                    "select id from paper_accounts where name=?",
                    ("paper_account_v2_clean_post_gate",),
                ).fetchone()[0]
                db.executemany(
                    """
                    insert into paper_orders(
                      run_id, signal_id, account_id, market_id, token_id, outcome, side,
                      signal_type, status, requested_shares, limit_price, estimated_cost,
                      reason, created_at, strategy_family
                    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (1, 1, clean_account_id, "m-dust", "tok-dust", "Yes", "buy", "paper_buy_edge", "skipped", 5.0, 0.001, 0.005, "below_min_entry_shadow_only", "2026-06-06T00:00:00Z", "latency_absorbing_state"),
                        (1, 2, clean_account_id, "m-shadow", "tok-shadow", "Yes", "buy", "paper_buy_edge", "skipped", 5.0, 0.40, 2.0, "strategy_family_shadow_only", "2026-06-06T00:01:00Z", "ladder_inconsistency"),
                    ],
                )
                db.commit()
                db.close()

                html_path = os.path.join(td, "dashboard.html")
                json_path = os.path.join(td, "dashboard.json")
                render_brain_performance.render(db_path, html_path, json_path, os.path.join(td, "iterations.jsonl"))

                with open(json_path, encoding="utf-8") as f:
                    snapshot = json.load(f)
                idle = snapshot["official_paper_status"]
                self.assertEqual(idle["account_name"], "paper_account_v2_clean_post_gate")
                self.assertTrue(idle["idle"])
                self.assertEqual(idle["fills"], 0)
                self.assertEqual(idle["skipped_reasons"]["below_min_entry_shadow_only"], 1)
                self.assertEqual(idle["skipped_reasons"]["strategy_family_shadow_only"], 1)
                self.assertIn("below min entry", idle["summary"])
                self.assertIn("shadow-only", idle["summary"])
                self.assertIn("Clean account idle", snapshot["fragments"]["official_paper_status_panel"])

                with open(html_path, encoding="utf-8") as f:
                    html = f.read()
                self.assertIn("Clean account idle", html)
                self.assertIn("below min entry", html)
                self.assertIn("shadow-only", html)
        finally:
            if old_account_name is None:
                os.environ.pop("PAPER_ACCOUNT_NAME", None)
            else:
                os.environ["PAPER_ACCOUNT_NAME"] = old_account_name

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

    def test_dashboard_split_preserves_legacy_counts_and_clean_v2_dedupes_proxy_labels(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            db.row_factory = sqlite3.Row
            for idx, event_key in enumerate(("event-a", "event-a", "event-b"), start=1):
                db.execute(
                    """
                    insert into training_rows(
                        run_id, market_id, title, outcome, label_status, label_value,
                        label_source, strategy_family, event_key, candidate_key,
                        model_prob, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1, f"m-{idx}", f"q-{idx}", "yes", "resolved", 1 if idx != 2 else 0,
                        "multi_provider_proxy_consensus", "clean_v2", event_key,
                        f"candidate-{idx}", 0.55,
                        "2026-05-10T12:00:00+00:00",
                    ),
                )
                training_row_id = db.execute("select last_insert_rowid()").fetchone()[0]
                db.execute(
                    """
                    insert into calibration_rows(
                        training_row_id, event_key, strategy_family,
                        prediction_prob, label_value,
                        label_source, label_confidence, brier, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        training_row_id, event_key, "clean_v2",
                        0.55, 1 if idx != 2 else 0,
                        "multi_provider_proxy_consensus", "multi_provider_proxy_consensus",
                        0.2025, "2026-05-10T12:00:00+00:00",
                    ),
                )
                db.execute(
                    """
                    insert into label_attempts(training_row_id, attempted_at, source_provider, outcome_status)
                    values (?, ?, ?, ?)
                    """,
                    (training_row_id, "2026-05-10T12:01:00+00:00", "multi_provider_proxy_consensus", "resolved"),
                )
            db.commit()
            snapshot = render_brain_performance.build_snapshot(db, db_path, json_filename="dashboard.json")
            db.close()

            legacy = snapshot["dashboard_views"]["legacy"]
            clean = snapshot["dashboard_views"]["clean_v2"]
            self.assertEqual(legacy["training_rows"], 3)
            self.assertEqual(legacy["label_attempts"], 3)
            self.assertEqual(clean["canonical_label_rows"], 3)
            self.assertEqual(clean["deduped_events"], 2)
            self.assertEqual(clean["truth_tiers"]["multi_provider_proxy_consensus"], 3)
            self.assertIn("dashboard_views_panel", snapshot["fragments"])

    def test_shadow_proxy_leaderboard_uses_proxy_labels_and_stays_non_operational_without_official_fills(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            db.row_factory = sqlite3.Row
            account_id = db.execute(
                """
                insert into paper_accounts(name, starting_cash, cash, realized_pnl, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                ("default-paper", 1000.0, 1000.0, 0.0,
                 "2026-05-10T12:00:00+00:00", "2026-05-10T12:00:00+00:00"),
            ).lastrowid
            db.execute(
                """
                create table strategy_shadow_fills(
                    id integer primary key,
                    strategy_shadow_order_id integer,
                    filled_at text,
                    shares real,
                    price real,
                    cost real,
                    slippage real,
                    source text,
                    raw_status text,
                    event_key text,
                    candidate_key text,
                    strategy_family text,
                    mode text
                )
                """
            )
            family = "clean_v2_edge"
            for idx in range(1, 4):
                db.execute(
                    """
                    insert into training_rows(
                        run_id, market_id, title, outcome, label_status, label_value,
                        label_source, strategy_family, event_key, candidate_key,
                        model_prob, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, f"m-{idx}", f"q-{idx}", "yes", "resolved", 1,
                     "multi_provider_proxy_consensus", family, f"event-{idx}", f"candidate-{idx}", 0.66,
                     "2026-05-10T12:00:00+00:00"),
                )
                training_row_id = db.execute("select last_insert_rowid()").fetchone()[0]
                db.execute(
                    """
                    insert into calibration_rows(
                        training_row_id, event_key, strategy_family,
                        prediction_prob, label_value,
                        label_source, label_confidence, brier, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (training_row_id, f"event-{idx}", family,
                     0.66, 1,
                     "multi_provider_proxy_consensus", "multi_provider_proxy_consensus",
                     0.1156, "2026-05-10T12:00:00+00:00"),
                )
                db.execute(
                    """
                    insert into shadow_fills(shadow_order_id, strategy_family, event_key, candidate_key,
                                             price, shares, cost, filled_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idx, family, f"event-{idx}", f"candidate-{idx}", 0.5, 2.0, 1.0,
                     "2026-05-10T12:02:00+00:00"),
                )
                order_id = db.execute(
                    """
                    insert into paper_orders(
                        run_id, signal_id, account_id, market_id, token_id, outcome, side,
                        signal_type, status, requested_shares, limit_price, estimated_cost,
                        reason, created_at, event_key, candidate_key, strategy_family
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, idx, account_id, f"m-{idx}", f"token-{idx}", "yes", "buy",
                     "paper_buy", "filled", 2.0, 0.5, 1.0, "legacy_shadow_counterfactual",
                     "2026-05-10T12:01:00+00:00", f"event-{idx}", f"candidate-{idx}", family),
                ).lastrowid
                db.execute(
                    """
                    insert into paper_fills(
                        order_id, filled_at, shares, price, cost, slippage, source,
                        raw_status, event_key, candidate_key, strategy_family
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, "2026-05-10T12:02:00+00:00", 2.0, 0.5, 1.0, 0.0,
                     "shadow", "shadow_counterfactual", f"event-{idx}", f"candidate-{idx}", family),
                )
            db.execute(
                """
                insert into strategy_shadow_fills(
                    strategy_shadow_order_id, strategy_family, event_key, candidate_key,
                    price, shares, cost, filled_at, mode
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (10, family, "event-extra", "candidate-extra", 0.4, 2.0, 0.8, "2026-05-10T12:03:00+00:00", "shadow"),
            )
            db.commit()
            snapshot = render_brain_performance.build_snapshot(db, db_path, json_filename="dashboard.json")
            db.close()

            rows = snapshot["shadow_proxy_leaderboard"]["rows"]
            self.assertEqual(rows[0]["strategy_family"], "clean_v2_edge")
            self.assertEqual(rows[0]["proxy_labeled_events"], 3)
            self.assertEqual(rows[0]["shadow_fills"], 4)
            self.assertEqual(rows[0]["official_fills"], 0)
            self.assertEqual(rows[0]["operational_verdict"], "SHADOW_ONLY")
            self.assertIn("shadow_proxy_leaderboard_panel", snapshot["fragments"])

    def test_shadow_proxy_leaderboard_includes_strategy_lab_families_without_proxy_evidence_as_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            db.row_factory = sqlite3.Row
            db.execute(
                """
                create table strategy_candidates(
                    id integer primary key,
                    strategy_family text
                )
                """
            )
            families = [
                "skip",
                "watch",
                "ladder_inconsistency",
                "diurnal_nowcast",
                "settlement_source_edge",
                "diurnal_tail_collapse",
            ]
            for idx, family in enumerate(families, start=1):
                if family == "diurnal_tail_collapse":
                    db.execute("insert into strategy_candidates(strategy_family) values (?)", (family,))
                    continue
                db.execute(
                    """
                    insert into training_rows(
                        run_id, market_id, title, outcome, label_status, label_value,
                        label_source, strategy_family, event_key, candidate_key,
                        model_prob, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        f"m-{idx}",
                        f"q-{idx}",
                        "yes",
                        "resolved",
                        1,
                        "multi_provider_proxy_consensus" if idx <= 3 else "pending_strategy_lab_review",
                        family,
                        f"event-{idx}",
                        f"candidate-{idx}",
                        0.66,
                        "2026-05-10T12:00:00+00:00",
                    ),
                )
                training_row_id = db.execute("select last_insert_rowid()").fetchone()[0]
                if idx <= 3:
                    db.execute(
                        """
                        insert into calibration_rows(
                            training_row_id, event_key, strategy_family,
                            prediction_prob, label_value,
                            label_source, label_confidence, brier, created_at
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            training_row_id,
                            f"event-{idx}",
                            family,
                            0.66,
                            1,
                            "multi_provider_proxy_consensus",
                            "multi_provider_proxy_consensus",
                            0.1156,
                            "2026-05-10T12:00:00+00:00",
                        ),
                    )
            db.commit()
            snapshot = render_brain_performance.build_snapshot(db, db_path, json_filename="dashboard.json")
            db.close()

            rows = snapshot["shadow_proxy_leaderboard"]["rows"]
            self.assertGreaterEqual(len(rows), 5)
            by_family = {row["strategy_family"]: row for row in rows}
            self.assertEqual(set(families), set(by_family))
            self.assertEqual(by_family["skip"]["operational_verdict"], "UNDER_REVIEW")
            for family in families[3:]:
                self.assertEqual(by_family[family]["proxy_labeled_events"], 0)
                self.assertEqual(by_family[family]["proxy_label_rows"], 0)
                self.assertEqual(by_family[family]["operational_verdict"], "DISABLED")
                self.assertEqual(by_family[family]["evidence_status"], "NO_PROXY_EVIDENCE")
                self.assertEqual(by_family[family]["strategy_lab_rows"], 1)


if __name__ == "__main__":
    unittest.main()
