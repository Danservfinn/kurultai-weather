from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import sqlite3
from collections import defaultdict
from typing import Any

PROMOTE_PAPER_SIZE = "PROMOTE_PAPER_SIZE"
CONTINUE_OBSERVING = "CONTINUE_OBSERVING"
INCONCLUSIVE = "INCONCLUSIVE"
KILL_OR_DISABLE = "KILL_OR_DISABLE"
RESOLVED_TARGET = 300
DAY_TARGET = 14
FRESH_QUOTE_SECONDS = 900
FAMILY_FRESH_QUOTE_SECONDS = {
    "complement_arb": 10,
    "latency_absorbing_state": 60,
    "ladder_inconsistency": 120,
    "diurnal_nowcast": 600,
    "settlement_source_edge": 1800,
    "forecast_distribution_directional": 3600,
    "unknown": 300,
}
MIN_RESOLVED_FOR_KILL = 50
MIN_FILLS_FOR_PROMOTION = 30
MIN_CLOB_FILL_RATE_FOR_PROMOTION = 0.50
MAX_DISPLAYED_PRICE_FILL_RATE_FOR_PROMOTION = 0.10
SURVIVAL_WEIGHTS = {
    "pnl_quality": 0.25,
    "calibration_advantage": 0.20,
    "edge_decile_persistence": 0.20,
    "execution_realism": 0.15,
    "sample_adequacy": 0.10,
    "ambiguity_control": 0.10,
}
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_weather.sqlite3")
SQLITE_BUSY_TIMEOUT_MS = 5000


def connect_db(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    timeout_seconds = SQLITE_BUSY_TIMEOUT_MS / 1000.0
    if readonly and os.path.exists(db_path):
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        db = sqlite3.connect(uri, timeout=timeout_seconds, uri=True, isolation_level=None)
    else:
        db = sqlite3.connect(db_path, timeout=timeout_seconds, isolation_level=None)
        db.execute("pragma journal_mode=WAL")
    db.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    db.row_factory = sqlite3.Row
    return db


def clamp(value: float | None, lo: float = 0.0, hi: float = 1.0) -> float:
    if value is None or math.isnan(float(value)) or math.isinf(float(value)):
        return lo
    return max(lo, min(hi, float(value)))


def safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if abs(float(den)) > 1e-12 else 0.0


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def get_value(row: sqlite3.Row | dict[str, Any], *names: str, default: Any = None) -> Any:
    keys = row.keys()
    for name in names:
        if name in keys:
            return row[name]
    return default


def family_of(row: sqlite3.Row | dict[str, Any]) -> str:
    value = get_value(row, "strategy_family")
    return str(value or "unknown").strip() or "unknown"


def read_all(db: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not table_exists(db, table):
        return []
    db.row_factory = sqlite3.Row
    return list(db.execute(f"select * from {table}"))


def ambiguous(row: sqlite3.Row | dict[str, Any]) -> bool:
    eligibility = str(get_value(row, "eligibility_class", default="") or "").lower()
    source_conf = str(get_value(row, "source_confidence", default="") or "").lower()
    settlement_state = str(get_value(row, "settlement_state", default="") or "").lower()
    label_status = str(get_value(row, "label_status", default="") or "").lower()
    return "ambiguous" in eligibility or "unclear" in eligibility or source_conf == "low" or "ambiguous" in settlement_state or "ambiguous" in label_status

def brier(prob: float | None, label: float | None) -> float | None:
    if prob is None or label is None:
        return None
    return (clamp(float(prob)) - clamp(float(label))) ** 2

def quote_is_fresh(row: sqlite3.Row | dict[str, Any], family: str | None = None) -> bool:
    quote_age = get_value(row, "quote_age_seconds")
    if quote_age is None:
        return False
    try:
        age = float(quote_age)
    except (TypeError, ValueError):
        return False
    execution_source = str(get_value(row, "execution_source", default="clob_book") or "clob_book")
    strategy_family = family or family_of(row)
    max_age = FAMILY_FRESH_QUOTE_SECONDS.get(strategy_family or "unknown", FAMILY_FRESH_QUOTE_SECONDS["unknown"])
    return not bool(get_value(row, "stale_book_flag", default=0)) and age <= max_age and execution_source == "clob_book"


def read_fills_by_candidate(db: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    if not (table_exists(db, "paper_fills") and table_exists(db, "paper_orders")):
        return {}
    db.row_factory = sqlite3.Row
    rows = db.execute("""select f.id fill_id, f.order_id, f.shares, f.price, f.cost, f.slippage, f.source, f.raw_status, coalesce(f.candidate_key, o.candidate_key) candidate_key, coalesce(f.event_key, o.event_key) event_key, coalesce(f.strategy_family, o.strategy_family) strategy_family, o.signal_id, o.status order_status from paper_fills f left join paper_orders o on o.id=f.order_id""").fetchall()
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = str(row["candidate_key"] or "")
        if key:
            out[key].append(row)
    return out

def read_fills_by_family(db: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    if not table_exists(db, "paper_fills"):
        return {}
    db.row_factory = sqlite3.Row
    if table_exists(db, "paper_orders"):
        rows = db.execute("select f.*, coalesce(f.strategy_family, o.strategy_family, 'unknown') family from paper_fills f left join paper_orders o on o.id=f.order_id").fetchall()
    else:
        rows = db.execute("select *, coalesce(strategy_family, 'unknown') family from paper_fills").fetchall()
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        out[str(row["family"] or "unknown")].append(row)
    return out

def count_orders(db: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"orders": 0, "filled_orders": 0})
    if not table_exists(db, "paper_orders"):
        return out
    db.row_factory = sqlite3.Row
    for row in db.execute("select coalesce(strategy_family, 'unknown') family, status, count(*) n from paper_orders group by family, status"):
        family = str(row["family"] or "unknown")
        n = int(row["n"] or 0)
        out[family]["orders"] += n
        if str(row["status"] or "").lower() == "filled":
            out[family]["filled_orders"] += n
    return out


def edge_deciles(rows: list[sqlite3.Row]) -> tuple[list[dict[str, Any]], float]:
    usable = [r for r in rows if get_value(r, "edge") is not None and get_value(r, "label_value") is not None]
    if not usable:
        return [], 0.0
    usable.sort(key=lambda r: float(get_value(r, "edge") or 0.0))
    deciles: list[dict[str, Any]] = []
    for idx in range(10):
        group = usable[math.floor(idx * len(usable) / 10):math.floor((idx + 1) * len(usable) / 10)]
        if not group:
            continue
        returns: list[float] = []
        edges: list[float] = []
        for row in group:
            entry = get_value(row, "entry_price", "market_prob")
            label = get_value(row, "label_value")
            if entry is None or label is None:
                continue
            entry_f = float(entry)
            returns.append(safe_div(float(label) - entry_f, entry_f))
            edges.append(float(get_value(row, "edge") or 0.0))
        deciles.append({"decile": idx + 1, "rows": len(group), "mean_edge": sum(edges) / len(edges) if edges else 0.0, "realized_return": sum(returns) / len(returns) if returns else 0.0, "win_rate": sum(1 for r in group if float(get_value(r, "label_value") or 0.0) >= 0.5) / len(group)})
    if len(deciles) < 3:
        return deciles, 0.0
    pairs = list(zip(deciles, deciles[1:]))
    return deciles, safe_div(sum(1 for a, b in pairs if float(b["realized_return"]) + 1e-12 >= float(a["realized_return"])), len(pairs))

def compute_realized_from_fills(rows: list[sqlite3.Row], fills_by_candidate: dict[str, list[sqlite3.Row]]) -> tuple[float, float, int]:
    pnl = 0.0
    cost = 0.0
    fills = 0
    for row in rows:
        label = get_value(row, "label_value")
        if label is None:
            continue
        for fill in fills_by_candidate.get(str(get_value(row, "candidate_key", default="") or ""), []):
            shares = float(fill["shares"] or 0.0)
            fill_cost = float(fill["cost"] or 0.0)
            pnl += shares * float(label) - fill_cost
            cost += fill_cost
            fills += 1
    return pnl, cost, fills

def verdict_for(row: dict[str, Any]) -> str:
    resolved = int(row.get("resolved_count") or 0)
    sample_days = int(row.get("sample_days") or 0)
    pnl = float(row.get("realized_pnl") or 0.0)
    brier_delta = float(row.get("brier_delta") or 0.0)
    persistence = float(row.get("edge_decile_persistence") or 0.0)
    ambiguity_control = float(row.get("ambiguity_control") or 0.0)
    execution_realism = float(row.get("execution_realism") or 0.0)
    fills = int(row.get("fills") or 0)
    clob_fill_rate = float(row.get("clob_fill_rate") or 0.0)
    displayed_price_fill_rate = float(row.get("displayed_price_fill_rate") or 0.0)
    score = float(row.get("survival_score") or 0.0)
    if (
        resolved >= RESOLVED_TARGET
        and sample_days >= DAY_TARGET
        and fills >= MIN_FILLS_FOR_PROMOTION
        and clob_fill_rate >= MIN_CLOB_FILL_RATE_FOR_PROMOTION
        and displayed_price_fill_rate <= MAX_DISPLAYED_PRICE_FILL_RATE_FOR_PROMOTION
        and pnl > 0.0
        and brier_delta > 0.0
        and persistence >= 0.50
        and ambiguity_control >= 0.80
        and score >= 0.60
    ):
        return PROMOTE_PAPER_SIZE
    if resolved < MIN_RESOLVED_FOR_KILL:
        return INCONCLUSIVE
    if ambiguity_control < 0.50 or execution_realism < 0.25 or (pnl < 0.0 and brier_delta <= 0.0):
        return KILL_OR_DISABLE
    return CONTINUE_OBSERVING


def evaluate_strategy_families(db_path: str = DEFAULT_DB_PATH, *, persist: bool = False) -> list[dict[str, Any]]:
    db = connect_db(db_path, readonly=not persist)
    training = read_all(db, "training_rows")
    signals = read_all(db, "signals")
    fills_by_candidate = read_fills_by_candidate(db)
    fills_by_family = read_fills_by_family(db)
    order_counts = count_orders(db)
    families = sorted({family_of(r) for r in training} | {family_of(r) for r in signals} | set(fills_by_family))
    results: list[dict[str, Any]] = []
    decile_records: dict[str, list[dict[str, Any]]] = {}
    for family in families:
        family_training = [r for r in training if family_of(r) == family]
        family_signals = [r for r in signals if family_of(r) == family]
        resolved_rows = [r for r in family_training if get_value(r, "label_value") is not None]
        dates = {str(get_value(r, "created_at"))[:10] for r in family_training if get_value(r, "created_at")}
        pnl, cost_basis, _ = compute_realized_from_fills(resolved_rows, fills_by_candidate)
        if not cost_basis and family in fills_by_family:
            cost_basis = sum(float(r["cost"] or 0.0) for r in fills_by_family[family])
        model_briers = [v for v in (brier(get_value(r, "model_prob"), get_value(r, "label_value")) for r in resolved_rows) if v is not None]
        market_briers = [v for v in (brier(get_value(r, "market_prob"), get_value(r, "label_value")) for r in resolved_rows) if v is not None]
        entry_price_briers = [v for v in (brier(get_value(r, "entry_price"), get_value(r, "label_value")) for r in resolved_rows) if v is not None]
        model_brier = sum(model_briers) / len(model_briers) if model_briers else 0.0
        market_brier = sum(market_briers) / len(market_briers) if market_briers else 0.0
        entry_price_brier_diagnostic = sum(entry_price_briers) / len(entry_price_briers) if entry_price_briers else 0.0
        brier_delta = market_brier - model_brier
        deciles, persistence = edge_deciles(resolved_rows)
        decile_records[family] = deciles
        filled_orders = int(order_counts[family]["filled_orders"])
        orders = int(order_counts[family]["orders"])
        fills = len(fills_by_family.get(family, []))
        clob_fills = sum(1 for r in fills_by_family.get(family, []) if str(r["source"] or "") == "clob_book")
        displayed_price_fills = sum(1 for r in fills_by_family.get(family, []) if str(r["source"] or "") != "clob_book")
        clob_fill_rate = clamp(safe_div(clob_fills, fills)) if fills else 0.0
        displayed_price_fill_rate = clamp(safe_div(displayed_price_fills, fills)) if fills else 0.0
        signal_count = len(family_signals)
        fill_den = orders or signal_count or len(family_training)
        fill_rate = clamp(safe_div(filled_orders or fills, fill_den))
        quote_rows = family_signals or family_training
        fresh = [r for r in quote_rows if quote_is_fresh(r)]
        fresh_quote_rate = clamp(safe_div(len(fresh), len(quote_rows))) if quote_rows else 0.0
        depth_ok = [r for r in quote_rows if bool(get_value(r, "depth_sufficient", default=0))]
        depth_sufficient_rate = clamp(safe_div(len(depth_ok), len(quote_rows))) if quote_rows else 0.0
        execution_realism = fill_rate * fresh_quote_rate * depth_sufficient_rate
        ambiguous_count = sum(1 for r in resolved_rows if ambiguous(r))
        ambiguity_control = 1.0 - clamp(safe_div(ambiguous_count, len(resolved_rows))) if resolved_rows else 0.0
        sample_adequacy = clamp(safe_div(len(resolved_rows), RESOLVED_TARGET))
        roi = safe_div(pnl, cost_basis)
        row = {
            "strategy_family": family, "candidates": len(family_training), "signals": signal_count, "fills": fills,
            "resolved_count": len(resolved_rows), "sample_days": len(dates), "realized_pnl": pnl,
            "cost_basis": cost_basis, "roi": roi, "model_brier": model_brier, "market_brier": market_brier,
            "entry_price_brier_diagnostic": entry_price_brier_diagnostic,
            "brier_delta": brier_delta, "edge_decile_persistence": persistence, "execution_realism": execution_realism,
            "fill_rate": fill_rate, "clob_fill_rate": clob_fill_rate, "displayed_price_fill_rate": displayed_price_fill_rate,
            "fresh_quote_rate": fresh_quote_rate, "depth_sufficient_rate": depth_sufficient_rate,
            "sample_adequacy": sample_adequacy, "ambiguity_control": ambiguity_control,
            "pnl_quality": clamp(roi), "calibration_advantage": clamp(brier_delta * 4.0),
        }
        row["survival_score"] = sum(SURVIVAL_WEIGHTS[k] * row[k] for k in SURVIVAL_WEIGHTS)
        row["verdict"] = verdict_for(row)
        results.append(row)
    results.sort(key=lambda r: r["survival_score"], reverse=True)
    if persist:
        persist_results(db, results, decile_records)
        db.commit()
    db.close()
    return results

def persist_results(db: sqlite3.Connection, rows: list[dict[str, Any]], deciles: dict[str, list[dict[str, Any]]]) -> None:
    db.executescript("""
    create table if not exists strategy_family_survival (
      strategy_family text primary key, evaluated_at text not null, candidates integer, signals integer, fills integer,
      resolved_count integer, sample_days integer, realized_pnl real, cost_basis real, roi real, model_brier real,
      market_brier real, brier_delta real, edge_decile_persistence real, execution_realism real, sample_adequacy real,
      ambiguity_control real, survival_score real, verdict text not null, metrics_json text not null
    );
    create table if not exists strategy_family_edge_deciles (
      strategy_family text not null, evaluated_at text not null, decile integer not null, rows integer,
      mean_edge real, realized_return real, win_rate real, primary key(strategy_family, decile)
    );
    """)
    evaluated_at = utc_now_iso()
    for row in rows:
        family = str(row["strategy_family"])
        db.execute("""
        insert into strategy_family_survival values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(strategy_family) do update set evaluated_at=excluded.evaluated_at, candidates=excluded.candidates,
        signals=excluded.signals, fills=excluded.fills, resolved_count=excluded.resolved_count, sample_days=excluded.sample_days,
        realized_pnl=excluded.realized_pnl, cost_basis=excluded.cost_basis, roi=excluded.roi, model_brier=excluded.model_brier,
        market_brier=excluded.market_brier, brier_delta=excluded.brier_delta, edge_decile_persistence=excluded.edge_decile_persistence,
        execution_realism=excluded.execution_realism, sample_adequacy=excluded.sample_adequacy,
        ambiguity_control=excluded.ambiguity_control, survival_score=excluded.survival_score, verdict=excluded.verdict,
        metrics_json=excluded.metrics_json
        """, (family, evaluated_at, row["candidates"], row["signals"], row["fills"], row["resolved_count"], row["sample_days"], row["realized_pnl"], row["cost_basis"], row["roi"], row["model_brier"], row["market_brier"], row["brier_delta"], row["edge_decile_persistence"], row["execution_realism"], row["sample_adequacy"], row["ambiguity_control"], row["survival_score"], row["verdict"], json.dumps(row, sort_keys=True)))
        db.execute("delete from strategy_family_edge_deciles where strategy_family=?", (family,))
        for decile in deciles.get(family, []):
            db.execute("insert into strategy_family_edge_deciles values(?,?,?,?,?,?,?)", (family, evaluated_at, decile["decile"], decile["rows"], decile["mean_edge"], decile["realized_return"], decile["win_rate"]))


def disabled_families(db_path: str = DEFAULT_DB_PATH, *, strict: bool = False) -> set[str]:
    # This helper runs inside the scanner's paper-order path while the scan
    # already owns the SQLite write connection. Keep it read-only to avoid
    # self-locking the paper DB; dashboard/CLI calls can persist snapshots.
    if strict:
        return {str(r["strategy_family"]) for r in evaluate_strategy_families(db_path, persist=False) if r["verdict"] != PROMOTE_PAPER_SIZE}
    return {str(r["strategy_family"]) for r in evaluate_strategy_families(db_path, persist=False) if r["verdict"] == KILL_OR_DISABLE}

def json_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "generated_at": utc_now_iso(), "mode": "strategy_family_survival", "thresholds": {"resolved_target": RESOLVED_TARGET, "day_target": DAY_TARGET}, "weights": SURVIVAL_WEIGHTS, "rows": rows}

def render_html(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for r in rows:
        vals = [r["strategy_family"], r["verdict"], f'{r["survival_score"]:.3f}', r["resolved_count"], r["sample_days"], f'{r["realized_pnl"]:.2f}', f'{r["roi"]:.2%}', f'{r["brier_delta"]:.4f}', f'{r["edge_decile_persistence"]:.2f}', f'{r["execution_realism"]:.2f}', f'{r["ambiguity_control"]:.2f}']
        body.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in vals) + "</tr>")
    return "<!doctype html><html><head><meta charset='utf-8'><title>Strategy Family Survival</title></head><body><h1>Strategy Family Survival Scoreboard</h1><table><thead><tr><th>Family</th><th>Verdict</th><th>Score</th><th>Resolved</th><th>Days</th><th>PnL</th><th>ROI</th><th>Brier delta</th><th>Deciles</th><th>Execution</th><th>Ambiguity</th></tr></thead><tbody>" + "".join(body) + "</tbody></table></body></html>"

def write_outputs(rows: list[dict[str, Any]], *, json_path: str | None = None, html_path: str | None = None) -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_payload(rows), f, indent=2, sort_keys=True)
            f.write("\n")
    if html_path:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(rows) + "\n")

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evaluate weather strategy-family survival.")
    p.add_argument("--db", default=DEFAULT_DB_PATH)
    p.add_argument("--persist", action="store_true")
    p.add_argument("--json", default="")
    p.add_argument("--html", default="")
    return p

def main() -> None:
    args = build_parser().parse_args()
    rows = evaluate_strategy_families(args.db, persist=args.persist)
    write_outputs(rows, json_path=args.json or None, html_path=args.html or None)
    print(json.dumps(json_payload(rows), indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
