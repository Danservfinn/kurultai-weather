# Weather Engine Real Edge Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Convert the paper-only Polymarket weather engine from raw/fake edge detection into a calibrated, settlement-source-aware edge discovery system that only promotes strategy slices with final-outcome, executable-price evidence.

**Architecture:** Keep the existing scanner and clean paper-account safety boundary intact. Add a read-only/derived evaluation layer that quarantines fake-edge families, slices historical evidence by settlement/source/execution dimensions, computes calibrated executable expected value, and surfaces only narrow candidate slices for continued paper observation. Official fills remain disabled unless a slice passes explicit survival gates.

**Tech Stack:** Python stdlib, SQLite, existing `scanner.py`, `edge_validation.py`, `render_brain_performance.py`, `tests/unittest`, repo-local dashboard artifacts. No wallet, live order placement, private keys, paid data providers, or production trading.

---

## Baseline Evidence

Live checks before this plan showed:

- `python3 scanner.py health` → `status=ok`, `mode=paper_only`, `live_trading=false`, `wallet=false`, `order_placement=false`.
- Clean paper account `paper_account_v2_clean_post_gate` remains at `$1000.00` equity and `$0.00` exposure.
- `python3 scanner.py evaluate --all-signals --limit 8` showed `8/8` top raw positive-edge resolved samples were losses.
- The broader prior check showed `20/20` top positive-edge resolved samples were losses.
- Dominant bad family: `paper_buy_ladder_inconsistency` with apparent `edge≈+98%`, `model≈100%`, `entry≈2%`, but resolved losses.

Conclusion: raw edge is not alpha. The first implementation priority is to make fake edge non-promotable and turn it into diagnostic signal.

---

## Non-Goals / Safety Boundaries

- No live trading.
- No wallet integration.
- No private keys or signing.
- No Polymarket order placement.
- No paid provider credentials.
- No loosening official clean-account fill gates.
- No treating mark-to-market as proof of edge.
- No promotion from a strategy family alone; promotion must be slice-specific and final-outcome validated.

---

## Task 0: Premortem, Baseline Snapshot, and Rollback Gate

**Objective:** Freeze the current mechanical baseline and prove this plan starts from a paper-only, safe state.
**Blast Radius:** local_additive
**Reviewer:** Kublai

**Files:**
- Create: `docs/receipts/weather_real_edge_baseline_2026-06-09.md`
- Read-only: `scanner.py`, `edge_validation.py`, `paper_weather.sqlite3`, `runtime_tunables.env`

**Verification:**

```bash
cd /Users/kublai/polymarket-weather-edge
python3 -m py_compile scanner.py edge_validation.py render_brain_performance.py
python3 -m unittest tests.test_edge_validation tests.test_weather_edge -v
python3 scanner.py health
python3 scanner.py evaluate --all-signals --limit 20
python3 scanner.py portfolio --positions 10
```

Expected:
- Compile succeeds.
- Tests pass or any pre-existing failures are recorded before code changes.
- Health says `paper_only`, `live_trading=false`, `wallet=false`, `order_placement=false`.
- Portfolio exposure is `0.00` before any implementation.

**Implementation notes:**
- Save exact command outputs in the receipt.
- Also record `git status --short` and `git rev-parse --short HEAD`.
- Rollback for all later tasks is `git revert` or restore files from git; no DB destructive migration is allowed.

---

## Task 1: Add a Deterministic Fake-Edge Quarantine Rule

**Objective:** Make repeated resolved losses from high raw-edge ladder inconsistency explicitly non-promotable and visible as a fake-edge class.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `edge_validation.py`
- Test: `tests/test_edge_validation.py`

**Step 1: Add failing tests**

Add tests asserting:

1. A family with high apparent edge, high model probability, low entry price, and repeated resolved losses gets `KILL_OR_DISABLE`.
2. The result row includes a diagnostic field such as `fake_edge_rate` or `raw_edge_loss_rate`.
3. `ladder_inconsistency` remains disabled even if raw edge decile persistence looks high.

Suggested test shape in `tests/test_edge_validation.py`:

```python
def test_high_raw_edge_resolved_losses_are_fake_edge_and_killed(self) -> None:
    path = make_db()
    self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
    con = sqlite3.connect(path)
    for i in range(80):
        candidate_key = f"fake-ladder-{i}"
        con.execute(
            """
            insert into training_rows(
              signal_id, created_at, market_id, outcome, market_prob, model_prob,
              entry_price, edge, depth_sufficient, label_value, event_key,
              candidate_key, strategy_family, eligibility_class, source_confidence,
              settlement_state, quote_age_seconds, stale_book_flag
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                i + 1,
                f"2026-02-{(i % 16) + 1:02d}T00:00:00+00:00",
                f"m{i}",
                "No",
                0.02,
                1.0,
                0.02,
                0.98,
                1,
                0.0,
                f"e{i}",
                candidate_key,
                "ladder_inconsistency",
                "clean_station",
                "high",
                "final",
                60.0,
                0,
            ),
        )
        con.execute(
            "insert into signals(id, created_at, market_id, outcome, signal_type, market_prob, model_prob, edge, entry_price, depth_sufficient, event_key, candidate_key, strategy_family, quote_age_seconds, stale_book_flag) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, f"2026-02-{(i % 16) + 1:02d}T00:00:00+00:00", f"m{i}", "No", "paper_buy_ladder_inconsistency", 0.02, 1.0, 0.98, 0.02, 1, f"e{i}", candidate_key, "ladder_inconsistency", 60.0, 0),
        )
        con.execute(
            "insert into paper_orders(id, signal_id, market_id, outcome, signal_type, status, estimated_cost, event_key, candidate_key, strategy_family) values(?,?,?,?,?,?,?,?,?,?)",
            (i + 1, i + 1, f"m{i}", "No", "paper_buy_ladder_inconsistency", "filled", 0.02, f"e{i}", candidate_key, "ladder_inconsistency"),
        )
        con.execute(
            "insert into paper_fills(order_id, shares, price, cost, slippage, source, raw_status, event_key, candidate_key, strategy_family) values(?,?,?,?,?,?,?,?,?,?)",
            (i + 1, 1.0, 0.02, 0.02, 0.0, "clob_book", "ok", f"e{i}", candidate_key, "ladder_inconsistency"),
        )
    con.commit()
    con.close()

    row = edge_validation.evaluate_strategy_families(path)[0]

    self.assertEqual(row["strategy_family"], "ladder_inconsistency")
    self.assertEqual(row["verdict"], edge_validation.KILL_OR_DISABLE)
    self.assertGreaterEqual(row["fake_edge_rate"], 0.9)
```

**Step 2: Run failing test**

```bash
python3 -m unittest tests.test_edge_validation.EdgeValidationTests.test_high_raw_edge_resolved_losses_are_fake_edge_and_killed -v
```

Expected: FAIL because `fake_edge_rate` does not exist yet.

**Step 3: Implement minimal metrics**

In `edge_validation.py`, add helper logic inside `evaluate_strategy_families`:

- Count resolved rows where `edge >= 0.50`, `model_prob >= 0.90`, `entry_price <= 0.10`, and `label_value == 0`.
- Compute `fake_edge_rate = fake_edge_losses / high_raw_edge_rows`.
- Add `fake_edge_rate` to the result row and `metrics_json`.
- Update `verdict_for` to kill when:
  - `resolved >= MIN_RESOLVED_FOR_KILL`, and
  - `fake_edge_rate >= 0.50`, and
  - high-raw-edge sample count is meaningful, e.g. `>= 20`.

**Step 4: Run tests**

```bash
python3 -m unittest tests.test_edge_validation -v
```

Expected: all edge validation tests pass.

**Step 5: Persist/display metric compatibility**

Do not add new table columns unless necessary. The existing `metrics_json` can carry the new fields. If dashboard needs it later, add renderer support in a separate task.

---

## Task 2: Build Slice Evaluation Data Structures

**Objective:** Evaluate edge by narrow slice instead of only broad strategy family.
**Blast Radius:** local_additive
**Reviewer:** Kublai

**Files:**
- Create: `edge_slices.py`
- Create: `tests/test_edge_slices.py`

**Slice keys:**

Start with deterministic, low-cardinality dimensions available in existing rows:

- `strategy_family`
- `city`
- `target_metric` (`daily_high`, `daily_low`, `unknown`)
- `source_confidence`
- `bucket_kind`
- `contract_type`
- `time_to_settlement_bucket` if present; otherwise `unknown`
- `entry_price_bucket`: `dust`, `cheap`, `mid`, `expensive`
- `execution_source`: `clob_book` vs displayed/other

**Data class:**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class SliceKey:
    strategy_family: str
    city: str
    target_metric: str
    source_confidence: str
    bucket_kind: str
    contract_type: str
    time_to_settlement_bucket: str
    entry_price_bucket: str
    execution_source: str

@dataclass
class SliceMetrics:
    key: SliceKey
    rows: int
    resolved: int
    sample_days: int
    wins: int
    losses: int
    realized_pnl: float
    cost_basis: float
    roi: float
    model_brier: float
    market_brier: float
    brier_delta: float
    fake_edge_rate: float
    verdict: str
```

**Tests:**

- `entry_price_bucket(0.001)` → `dust`
- `entry_price_bucket(0.02)` → `cheap`
- missing dimensions normalize to `unknown`
- repeated event keys dedupe to one event-level row

**Verification:**

```bash
python3 -m unittest tests.test_edge_slices -v
```

Expected: new tests pass.

---

## Task 3: Compute Final-Outcome Slice Metrics from Existing DB

**Objective:** Add a read-only slice evaluator that computes realized/final metrics from current `training_rows`, `signals`, `paper_orders`, and `paper_fills`.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `edge_slices.py`
- Test: `tests/test_edge_slices.py`

**Implementation:**

Add:

```python
def evaluate_slices(db_path: str, *, min_resolved: int = 20) -> list[SliceMetrics]:
    ...
```

Rules:

- Use `edge_validation.connect_db(..., readonly=True)`.
- Use `edge_validation.read_all` and `edge_validation.dedupe_event_samples` where possible.
- Only count rows with final label evidence: `label_value is not null`.
- PnL should be based on executable fills when available; otherwise keep `cost_basis=0` and do not promote.
- Verdicts:
  - `SLICE_PROMOTE_SHADOW`: enough samples, positive PnL, positive brier delta, low fake edge, executable fills.
  - `SLICE_CONTINUE_OBSERVING`: insufficient samples or mixed signal.
  - `SLICE_KILL`: fake-edge dominated, negative PnL with enough samples, ambiguous/source-low dominated.

**Threshold defaults:**

- `min_resolved = 20` for slice diagnostics.
- `promotion_min_resolved = 100` before official paper fill consideration.
- `min_sample_days = 7` for slice diagnostics; `14` for promotion.
- `fake_edge_rate < 0.10` for promotion.
- `displayed_price_fill_rate <= 0.10` for promotion.

**Verification:**

```bash
python3 -m unittest tests.test_edge_slices -v
python3 - <<'PY'
import edge_slices
rows = edge_slices.evaluate_slices('/Users/kublai/polymarket-weather-edge/paper_weather.sqlite3', min_resolved=20)
print(len(rows))
print(rows[:5])
PY
```

Expected:
- Tests pass.
- Evaluator runs read-only against live DB.
- No DB mutations.

---

## Task 4: Add CLI Command for Slice Evaluation

**Objective:** Expose slice evaluation through `scanner.py` without changing scan behavior.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `scanner.py`
- Test: `tests/test_weather_edge.py` or new `tests/test_scanner_slice_cli.py`

**Implementation:**

Add subcommand:

```bash
python3 scanner.py evaluate-slices --min-resolved 20 --limit 25
python3 scanner.py evaluate-slices --json /tmp/weather_slices.json --html /tmp/weather_slices.html
```

CLI output should include:

- total slices
- counts by verdict
- top `SLICE_PROMOTE_SHADOW` candidates if any
- top killed fake-edge slices
- warning that output is paper-only and not tradable

**Test:**

Use a temp DB fixture and call the parser/handler directly if scanner has handler functions; otherwise call subprocess with the test DB flag if available. If no DB flag exists for subcommands, add `--db` to the new command only.

**Verification:**

```bash
python3 -m unittest tests.test_weather_edge -v
python3 scanner.py evaluate-slices --min-resolved 20 --limit 10
```

Expected:
- Command exits 0.
- Output names `paper_only`.
- No wallet/order placement paths appear.

---

## Task 5: Add Calibrated Executable EV Ranking

**Objective:** Replace raw `model_prob - entry_price` ranking with calibrated, executable expected value for slice diagnostics.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `edge_slices.py`
- Test: `tests/test_edge_slices.py`

**Implementation:**

Add function:

```python
def calibrated_probability(raw_model_prob: float, slice_metrics: SliceMetrics) -> float:
    ...
```

Initial YAGNI implementation:

- If slice has enough resolved rows, use empirical win rate blended with raw model probability.
- Suggested blend:
  - `weight = min(0.80, resolved / 300)`
  - `calibrated = weight * empirical_win_rate + (1 - weight) * raw_model_prob`
- If `fake_edge_rate` is high, cap calibrated probability at empirical win rate.

Add:

```python
def executable_ev(calibrated_prob: float, ask: float | None, spread: float | None, slippage_buffer: float = 0.02) -> float:
    if ask is None:
        return float('-inf')
    return calibrated_prob - ask - max(spread or 0.0, 0.0) - slippage_buffer
```

**Tests:**

- A raw `model_prob=1.0` slice with empirical `0/80` wins calibrates near `0`, not `1`.
- EV uses ask price, not mid/entry/displayed price.
- Missing ask produces non-promotable EV.

**Verification:**

```bash
python3 -m unittest tests.test_edge_slices -v
```

Expected: all tests pass.

---

## Task 6: Quarantine Ladder Inconsistency from Official Fill Eligibility

**Objective:** Ensure `ladder_inconsistency` can continue as shadow evidence but cannot create official paper fills until explicitly promoted by slice evidence.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `scanner.py`
- Test: `tests/test_weather_edge.py`

**Current relevant behavior:**
- `classify_strategy_family(... ladder_status="ladder_violation")` maps to `ladder_inconsistency`.
- `paper_buy_survival_gate_disabled` already consults `edge_validation.disabled_families`.

**Implementation:**

Add a single additional guard near the paper-order eligibility path:

```python
ALWAYS_SHADOW_ONLY_FAMILIES = {"ladder_inconsistency"}
```

If `strategy_family in ALWAYS_SHADOW_ONLY_FAMILIES`, official paper order should be skipped with reason like:

```text
strategy_family_shadow_only_ladder_inconsistency_fake_edge_quarantine
```

Do not suppress training rows or shadow rows.

**Test:**

Add/extend a test that constructs a would-be paper buy with `strategy_family="ladder_inconsistency"` and asserts:

- no official paper fill is created;
- skip reason is explicit;
- signal/training evidence still persists if the surrounding test fixture supports persistence.

**Verification:**

```bash
python3 -m unittest tests.test_weather_edge -v
python3 scanner.py scan --pause 0.01 --disable-ledger
python3 scanner.py portfolio --positions 5
```

Expected:
- Tests pass.
- Portfolio official exposure remains `0.00` unless other already-approved families fill.

---

## Task 7: Persist Slice Survival Snapshots

**Objective:** Store slice-level evidence in SQLite so dashboard and future tuning can track improvements over time.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `edge_slices.py`
- Test: `tests/test_edge_slices.py`

**Schema:**

Create table only when `persist=True`:

```sql
create table if not exists strategy_slice_survival (
  slice_key text primary key,
  evaluated_at text not null,
  strategy_family text not null,
  city text,
  target_metric text,
  source_confidence text,
  bucket_kind text,
  contract_type text,
  time_to_settlement_bucket text,
  entry_price_bucket text,
  execution_source text,
  resolved integer,
  sample_days integer,
  wins integer,
  losses integer,
  realized_pnl real,
  cost_basis real,
  roi real,
  model_brier real,
  market_brier real,
  brier_delta real,
  fake_edge_rate real,
  calibrated_ev real,
  verdict text not null,
  metrics_json text not null
);
```

**Implementation:**

Add:

```python
def persist_slice_results(db: sqlite3.Connection, rows: list[SliceMetrics]) -> None:
    ...
```

**Tests:**

- `evaluate_slices(..., persist=True)` creates table.
- Upsert does not duplicate rows on second run.
- `metrics_json` includes full slice key and threshold info.

**Verification:**

```bash
python3 -m unittest tests.test_edge_slices -v
python3 scanner.py evaluate-slices --persist --limit 10
sqlite3 paper_weather.sqlite3 'select count(*) from strategy_slice_survival;'
```

Expected:
- Count > 0 after persist.
- No existing tables are dropped or destructively migrated.

---

## Task 8: Render Slice Evidence on the Dashboard

**Objective:** Make the dashboard show the difference between fake raw edge and plausible calibrated slice edge.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `render_brain_performance.py`
- Test: `tests/test_render_brain_performance.py`

**Dashboard sections:**

Add sections:

1. **Fake Edge Quarantine**
   - family/slice
   - resolved count
   - fake edge rate
   - PnL
   - verdict

2. **Calibrated Slice Watchlist**
   - slice key compact label
   - resolved/sample days
   - calibrated EV
   - brier delta
   - execution realism
   - verdict

3. **Promotion Gate Status**
   - no slice promoted
   - continue observing
   - shadow-only candidate
   - disabled/killed

**Tests:**

- Renderer includes `Fake Edge Quarantine` when DB has killed slice rows.
- Renderer includes `Calibrated Slice Watchlist` when DB has slice rows.
- JSON output includes a machine-readable `strategy_slice_survival` block.

**Verification:**

```bash
python3 -m unittest tests.test_render_brain_performance -v
python3 render_brain_performance.py --output /tmp/weather_dashboard.html --json-output /tmp/weather_dashboard.json
python3 -m json.tool /tmp/weather_dashboard.json >/tmp/weather_dashboard.pretty.json
```

Expected:
- HTML and JSON render successfully.
- JSON contains slice fields.

---

## Task 9: Integrate Slice Evaluation into the Existing Scan/Render Wrapper

**Objective:** Refresh slice survival after each scan without changing official fill behavior.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `run_paper_scan_and_render.sh`
- Test: `tests/test_weather_edge.py` if wrapper tests exist; otherwise add a shell smoke receipt.

**Implementation:**

After the existing `edge_validation.py --persist` call, add:

```bash
python3 edge_slices.py --persist --json /tmp/weather_slice_survival.json
```

Or, if only exposed through scanner:

```bash
python3 scanner.py evaluate-slices --persist --json /tmp/weather_slice_survival.json
```

**Verification:**

```bash
bash -n run_paper_scan_and_render.sh
./run_paper_scan_and_render.sh
python3 scanner.py health
python3 scanner.py portfolio --positions 5
```

Expected:
- Wrapper completes.
- Dashboard JSON mtime updates.
- Official clean-account exposure remains safe.

---

## Task 10: Add an Edge Research Report Command

**Objective:** Produce a compact, repeatable “where could edge exist?” report for Danny/Kublai without manually querying SQLite.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `scanner.py`
- Test: `tests/test_weather_edge.py`

**CLI:**

```bash
python3 scanner.py edge-report --limit 20
python3 scanner.py edge-report --json /tmp/edge_report.json
```

**Report should answer:**

- Which raw-edge families are fake and why?
- Which slices have positive final-outcome evidence?
- Which slices are inconclusive due to sample size?
- Which slices are killed due to ambiguity/execution/fake edge?
- What data would most improve the decision?

**Output contract:**

```json
{
  "mode": "paper_only",
  "generated_at": "...",
  "summary": {
    "raw_positive_edge_top_loss_rate": 1.0,
    "official_clean_account_exposure": 0.0
  },
  "fake_edge": [...],
  "candidate_slices": [...],
  "blocked_by": [...],
  "next_actions": [...]
}
```

**Verification:**

```bash
python3 scanner.py edge-report --json /tmp/weather_edge_report.json
python3 -m json.tool /tmp/weather_edge_report.json >/tmp/weather_edge_report.pretty.json
```

Expected: valid JSON and no trading side effects.

---

## Task 11: Add Source-Confidence and Settlement-Source Delta Diagnostics

**Objective:** Move toward the most plausible edge source: settlement-source mismatch/latency instead of raw forecast confidence.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `features.py`
- Modify: `edge_slices.py`
- Test: `tests/test_features.py`
- Test: `tests/test_edge_slices.py`

**Feature fields to preserve/use if already present:**

- `station_source`
- `source_url`
- `provider`
- `source_confidence`
- `settlement_state`
- `target_metric`
- `station_id`
- `label_source`

**New derived diagnostics:**

- `settlement_source_known`: boolean
- `multi_source_consensus`: boolean/count
- `source_disagreement_abs_f`: numeric if available
- `official_vs_proxy_delta_f`: numeric if available
- `station_confidence_bucket`: high/medium/low/unknown

**Tests:**

- High confidence official final labels get favorable source confidence.
- Ambiguous/low confidence source rows cannot be promoted.
- Missing settlement source rows go to `blocked_by: missing_settlement_source`.

**Verification:**

```bash
python3 -m unittest tests.test_features tests.test_edge_slices -v
```

---

## Task 12: Add Late-Day Convergence Shadow Family

**Objective:** Create a focused strategy family for plausible alpha: markets close to settlement where official/proxy observations constrain outcome but market price is stale.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `scanner.py`
- Modify: `features.py`
- Modify: `scripts/strategy_lab_shadow_backfill.py`
- Test: `tests/test_weather_edge.py`
- Test: `tests/test_features.py`

**Family name:**

```text
late_official_convergence
```

**Initial gate:** shadow-only.

**Candidate conditions:**

- source confidence high;
- target date is today or just resolved;
- official/proxy source has fresh observation or strongly bounded current high/low;
- executable ask exists;
- spread <= configured max;
- depth sufficient;
- not a `ladder_inconsistency` raw-edge case;
- calibrated EV > threshold.

**Tests:**

- A late-day high-confidence row classifies as `late_official_convergence`.
- Low-confidence station/source does not classify.
- Family remains shadow-only.

**Verification:**

```bash
python3 -m unittest tests.test_weather_edge tests.test_features -v
python3 scanner.py scan --pause 0.01 --disable-ledger
python3 scanner.py evaluate-slices --limit 20
```

Expected:
- No official clean-account fills from this family yet.
- Slice rows can be observed and later evaluated.

---

## Task 13: Add Promotion Criteria for Slice, Not Family

**Objective:** Ensure only a specific slice can be considered for official paper fill promotion, not an entire broad strategy family.
**Blast Radius:** local_mutating
**Reviewer:** Kublai

**Files:**
- Modify: `edge_slices.py`
- Modify: `scanner.py`
- Test: `tests/test_edge_slices.py`
- Test: `tests/test_weather_edge.py`

**Promotion criteria:**

A slice can become `SLICE_PROMOTE_PAPER_SIZE` only if:

- `resolved >= 100`
- `sample_days >= 14`
- `realized_pnl > 0`
- `roi > 0`
- `brier_delta > 0`
- `fake_edge_rate <= 0.10`
- `ambiguity_rate <= 0.10`
- `clob_fill_rate >= 0.80`
- `displayed_price_fill_rate <= 0.05`
- no single city/date/event dominates more than configured concentration caps

**Important:** This task does not enable live trading. It only allows official paper-account sizing if a slice passes. If in doubt, leave official fills shadow-only and surface the candidate to dashboard.

**Verification:**

```bash
python3 -m unittest tests.test_edge_slices tests.test_weather_edge -v
python3 scanner.py portfolio --positions 10
```

Expected:
- Tests pass.
- Existing fake-edge families do not promote.

---

## Task 14: Add Out-of-Sample Date Walk-Forward Evaluation

**Objective:** Prevent overfitting to the current 34-day sample by evaluating chronological train/test windows.
**Blast Radius:** local_additive
**Reviewer:** Kublai

**Files:**
- Create: `slice_walk_forward.py`
- Create: `tests/test_slice_walk_forward.py`

**Implementation:**

- Input: historical slice rows from `training_rows` / `strategy_slice_survival`.
- Split by date, not random rows.
- Example windows:
  - train first 70%, test next 30%; and/or rolling 14-day train / 7-day test.
- Output per slice:
  - train ROI, test ROI
  - train brier delta, test brier delta
  - stability verdict

**Verification:**

```bash
python3 -m unittest tests.test_slice_walk_forward -v
python3 slice_walk_forward.py --db paper_weather.sqlite3 --json /tmp/weather_walk_forward.json
python3 -m json.tool /tmp/weather_walk_forward.json >/tmp/weather_walk_forward.pretty.json
```

Expected: JSON contains stable/unstable verdicts and uses date-based splits.

---

## Task 15: Update Brain/Repo Documentation

**Objective:** Make the new edge discipline durable for future GPT/Kublai context.
**Blast Radius:** local_additive
**Reviewer:** Kublai

**Files:**
- Create or modify: `brain/edge-research.md`
- Modify: `brain/strategy-families.md` if present
- Modify: `README.md` only if it already documents operation commands

**Content:**

- Raw edge is not alpha.
- `ladder_inconsistency` is quarantined / shadow-only.
- Edge is pursued through calibrated, executable, final-outcome slice evidence.
- The plausible alpha lanes are:
  - settlement-source delta;
  - late official convergence;
  - city/source-specific calibration;
  - executable ladder-sum arbitrage after spread/depth, not raw ladder violation.

**Verification:**

```bash
python3 - <<'PY'
from pathlib import Path
for path in ['brain/edge-research.md']:
    p = Path(path)
    assert p.exists(), path
    text = p.read_text()
    assert 'ladder_inconsistency' in text
    assert 'paper-only' in text.lower()
print('docs ok')
PY
```

Expected: documentation exists and names safety boundaries.

---

## Task 16: Full Verification and Dashboard Refresh

**Objective:** Prove the complete implementation works and remains paper-only.
**Blast Radius:** local_additive
**Reviewer:** Kublai

**Files:**
- Create: `docs/receipts/weather_real_edge_implementation_receipt_2026-06-09.md`

**Commands:**

```bash
cd /Users/kublai/polymarket-weather-edge
python3 -m py_compile scanner.py edge_validation.py edge_slices.py render_brain_performance.py features.py
python3 -m unittest discover -v
python3 scanner.py health
python3 scanner.py scan --pause 0.01 --disable-ledger
python3 scanner.py evaluate --all-signals --limit 20
python3 scanner.py evaluate-slices --persist --limit 25
python3 scanner.py edge-report --json /tmp/weather_edge_report.json
python3 render_brain_performance.py --output /Users/kublai/brain/projects/polymarket-weather-engine-performance.html --json-output /Users/kublai/brain/projects/polymarket-weather-engine-performance.json
python3 scanner.py portfolio --positions 10
git diff --check
git status --short
```

Expected:

- All tests pass.
- Health remains `paper_only`, `live_trading=false`, `wallet=false`, `order_placement=false`.
- Official clean-account exposure remains bounded and ideally `0.00` unless an explicitly promoted paper slice exists.
- Dashboard refreshes.
- Edge report says fake-edge families are quarantined and candidate slices are shadow/watch only until they pass gates.

---

## Questions Answered / Default Decisions

- **Should we trade the current +98% signals?** No. They are resolved-loss fake edge.
- **Should `ladder_inconsistency` be deleted?** No. Keep it as a diagnostic/shadow family, but block official fills.
- **Should promotion happen at strategy-family level?** No. Promotion must be slice-specific.
- **Should mark-to-market count as edge proof?** No. Final labels and executable fills only.
- **Should we add paid data providers now?** No. Use existing public/local data until a separate credentials/approval gate exists.
- **Should we loosen official fill gates to get more paper action?** No. First improve evidence quality; loosen only if a slice passes gates.
- **Who reviews implementation?** Kublai by default. Danny only if a task crosses live trading, wallet, paid provider, account/KYC, production deploy, or other high-risk boundaries.
- **What is the first likely real edge lane?** Late official/proxy convergence and settlement-source delta, not broad weather forecasting.

---

## Execution Handoff

Plan complete. Recommended implementation mode:

1. Task 0 baseline receipt.
2. Tasks 1–4 in one small branch: fake-edge quarantine + slice evaluator + CLI.
3. Verify against live DB and dashboard.
4. Tasks 5–8 in second branch: calibrated EV + dashboard.
5. Tasks 9–16 after first branch proves no safety regression.

Do not run live trading at any point. Do not mark the weather engine as having a tradable edge until at least one slice passes final-outcome, executable-price, out-of-sample survival gates.
