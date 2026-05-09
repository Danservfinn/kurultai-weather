from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import edge_validation


SCHEMA = """
create table training_rows (
  id integer primary key,
  signal_id integer,
  created_at text,
  market_id text,
  outcome text,
  market_prob real,
  model_prob real,
  entry_price real,
  edge real,
  depth_sufficient integer,
  label_value real,
  event_key text,
  candidate_key text,
  strategy_family text,
  eligibility_class text,
  source_confidence text,
  settlement_state text,
  quote_age_seconds real,
  stale_book_flag integer
);
create table signals (
  id integer primary key,
  created_at text,
  market_id text,
  outcome text,
  signal_type text,
  market_prob real,
  model_prob real,
  edge real,
  entry_price real,
  depth_sufficient integer,
  event_key text,
  candidate_key text,
  strategy_family text,
  quote_age_seconds real,
  stale_book_flag integer
);
create table paper_orders (
  id integer primary key,
  signal_id integer,
  market_id text,
  outcome text,
  signal_type text,
  status text,
  estimated_cost real,
  event_key text,
  candidate_key text,
  strategy_family text
);
create table paper_fills (
  id integer primary key,
  order_id integer,
  shares real,
  price real,
  cost real,
  slippage real,
  source text,
  raw_status text,
  event_key text,
  candidate_key text,
  strategy_family text
);
"""


def make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def insert_family(path: str, family: str, *, n: int, days: int, good: bool, filled_every: int = 1, ambiguous_every: int = 0) -> None:
    con = sqlite3.connect(path)
    for i in range(n):
        day = (i % days) + 1
        edge = (i % 10) / 100.0 + (0.02 if good else -0.02)
        market_prob = 0.42 if good else 0.58
        model_prob = 0.68 if good else 0.70
        label = 1.0 if (good or i % 3 == 0) else 0.0
        entry = 0.50 if good else 0.70
        ambiguous = ambiguous_every and i % ambiguous_every == 0
        candidate_key = f"{family}-{i}"
        created_at = f"2026-01-{day:02d}T00:00:00+00:00"
        con.execute(
            """
            insert into training_rows(
              signal_id, created_at, market_id, outcome, market_prob, model_prob, entry_price, edge,
              depth_sufficient, label_value, event_key, candidate_key, strategy_family,
              eligibility_class, source_confidence, settlement_state, quote_age_seconds, stale_book_flag
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                i + 1, created_at, f"m{i}", "yes", market_prob, model_prob, entry, edge,
                1, label, f"e{i}", candidate_key, family,
                "ambiguous_resolution" if ambiguous else "clean_station",
                "low" if ambiguous else "high",
                "unknown", 60.0, 0,
            ),
        )
        con.execute(
            """
            insert into signals(id, created_at, market_id, outcome, signal_type, market_prob, model_prob, edge,
              entry_price, depth_sufficient, event_key, candidate_key, strategy_family, quote_age_seconds, stale_book_flag)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (i + 1, created_at, f"m{i}", "yes", "paper_buy_forecast_distribution", market_prob, model_prob, edge, entry, 1, f"e{i}", candidate_key, family, 60.0, 0),
        )
        if i % filled_every == 0:
            con.execute(
                "insert into paper_orders(id, signal_id, market_id, outcome, signal_type, status, estimated_cost, event_key, candidate_key, strategy_family) values(?,?,?,?,?,?,?,?,?,?)",
                (i + 1, i + 1, f"m{i}", "yes", "paper_buy_forecast_distribution", "filled", entry, f"e{i}", candidate_key, family),
            )
            con.execute(
                "insert into paper_fills(order_id, shares, price, cost, slippage, source, raw_status, event_key, candidate_key, strategy_family) values(?,?,?,?,?,?,?,?,?,?)",
                (i + 1, 1.0, entry, entry, 0.0, "clob_book", "ok", f"e{i}", candidate_key, family),
            )
    con.commit()
    con.close()


class EdgeValidationTests(unittest.TestCase):
    def test_promote_family_has_positive_pnl_brier_persistence_and_persists_deciles(self) -> None:
        path = make_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_family(path, "forecast_distribution_directional", n=320, days=16, good=True)

        rows = edge_validation.evaluate_strategy_families(path, persist=True)
        row = rows[0]

        self.assertEqual(row["strategy_family"], "forecast_distribution_directional")
        self.assertEqual(row["resolved_count"], 320)
        self.assertGreater(row["realized_pnl"], 0)
        self.assertGreater(row["brier_delta"], 0)
        self.assertGreaterEqual(row["edge_decile_persistence"], 0.5)
        self.assertEqual(row["verdict"], edge_validation.PROMOTE_PAPER_SIZE)

        con = sqlite3.connect(path)
        deciles = con.execute("select count(*) from strategy_family_edge_deciles").fetchone()[0]
        survival = con.execute("select verdict from strategy_family_survival where strategy_family=?", ("forecast_distribution_directional",)).fetchone()[0]
        con.close()
        self.assertGreaterEqual(deciles, 10)
        self.assertEqual(survival, edge_validation.PROMOTE_PAPER_SIZE)

    def test_bad_resolved_family_is_killed(self) -> None:
        path = make_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_family(path, "ladder_inconsistency", n=120, days=15, good=False)

        row = edge_validation.evaluate_strategy_families(path)[0]

        self.assertEqual(row["strategy_family"], "ladder_inconsistency")
        self.assertLess(row["realized_pnl"], 0)
        self.assertEqual(row["verdict"], edge_validation.KILL_OR_DISABLE)

    def test_small_sample_stays_inconclusive_even_if_profitable(self) -> None:
        path = make_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_family(path, "latency_absorbing_state", n=12, days=4, good=True)

        row = edge_validation.evaluate_strategy_families(path)[0]

        self.assertEqual(row["resolved_count"], 12)
        self.assertEqual(row["verdict"], edge_validation.INCONCLUSIVE)

    def test_ambiguous_family_is_disabled(self) -> None:
        path = make_db()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        insert_family(path, "settlement_source_edge", n=100, days=15, good=True, ambiguous_every=1)

        row = edge_validation.evaluate_strategy_families(path)[0]

        self.assertLess(row["ambiguity_control"], 0.5)
        self.assertEqual(row["verdict"], edge_validation.KILL_OR_DISABLE)


if __name__ == "__main__":
    unittest.main()
