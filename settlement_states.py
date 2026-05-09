#!/usr/bin/env python3
"""Settlement-state helpers for paper weather contracts.

The helpers model daily high-temperature contracts from the observed maximum
temperature so far and whether the station-local target day is complete. They
do not fetch data, trade, or touch secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


CONTRACT_THRESHOLD = "threshold"
CONTRACT_EXACT = "exact"
CONTRACT_RANGE = "range"
CONTRACT_UNKNOWN = "unknown"

SIDE_YES = "yes"
SIDE_NO = "no"

STATE_STILL_POSSIBLE = "still_possible"
STATE_SOURCE_MISSING = "source_missing"
STATE_AMBIGUOUS = "ambiguous"
STATE_YES_CERTAIN = "yes_certain"
STATE_NO_CERTAIN = "no_certain"
STATE_YES_IMPOSSIBLE = "yes_impossible"
STATE_NO_IMPOSSIBLE = "no_impossible"
STATE_FINAL_YES = "final_yes"
STATE_FINAL_NO = "final_no"
STATE_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ContractSpec:
    contract_type: str
    side: str = SIDE_YES
    low_f: float | None = None
    high_f: float | None = None
    threshold_f: float | None = None
    threshold_direction: str | None = None
    label: str = ""
    bucket_kind: str | None = None


@dataclass(frozen=True)
class SettlementState:
    contract_type: str
    side: str
    early_state: str
    final_state: str
    payout: float | None
    absorbing: bool
    reason: str

    @property
    def state(self) -> str:
        return self.final_state if self.final_state != STATE_UNRESOLVED else self.early_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_type": self.contract_type,
            "side": self.side,
            "early_state": self.early_state,
            "final_state": self.final_state,
            "settlement_state": self.state,
            "payout": self.payout,
            "absorbing": self.absorbing,
            "reason": self.reason,
        }


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def classify_contract(
    bucket_kind: str | None,
    low_f: float | None,
    high_f: float | None,
    *,
    side: str = SIDE_YES,
    label: str = "",
) -> ContractSpec:
    """Classify parsed bucket bounds into threshold, exact, range, or unknown."""
    kind = str(bucket_kind or "").strip().lower()
    side_norm = SIDE_NO if str(side or "").strip().lower() in {"no", "n"} else SIDE_YES
    low = float(low_f) if low_f is not None else None
    high = float(high_f) if high_f is not None else None

    if kind.startswith("open_above") or (low is not None and (high is None or high == math.inf)):
        return ContractSpec(CONTRACT_THRESHOLD, side_norm, low, high, low, "gte", label, kind)
    if kind.startswith("open_below") or (high is not None and (low is None or low == -math.inf)):
        return ContractSpec(CONTRACT_THRESHOLD, side_norm, low, high, high, "lte", label, kind)
    if kind in {"single_integer", "point"}:
        threshold = (low + high) / 2.0 if _finite(low) and _finite(high) else None
        return ContractSpec(CONTRACT_EXACT, side_norm, low, high, threshold, "eq", label, kind)
    if kind == "closed_range" or (_finite(low) and _finite(high) and low != high):
        return ContractSpec(CONTRACT_RANGE, side_norm, low, high, None, "range", label, kind)
    return ContractSpec(CONTRACT_UNKNOWN, side_norm, low, high, None, None, label, kind or None)


def yes_payout_for_high(spec: ContractSpec, final_high_f: float | None) -> float | None:
    if final_high_f is None or spec.contract_type == CONTRACT_UNKNOWN:
        return None
    high = float(final_high_f)
    if spec.contract_type == CONTRACT_THRESHOLD:
        if spec.threshold_direction == "gte":
            return 1.0 if spec.low_f is not None and high >= spec.low_f else 0.0
        if spec.threshold_direction == "lte":
            return 1.0 if spec.high_f is not None and high <= spec.high_f else 0.0
        return None
    if spec.low_f is None or spec.high_f is None:
        return None
    return 1.0 if spec.low_f <= high <= spec.high_f else 0.0


def payout_for_high(spec: ContractSpec, final_high_f: float | None) -> float | None:
    yes = yes_payout_for_high(spec, final_high_f)
    if yes is None:
        return None
    return 1.0 - yes if spec.side == SIDE_NO else yes


def _invert_early_state(state: str) -> str:
    return {
        STATE_YES_CERTAIN: STATE_NO_IMPOSSIBLE,
        STATE_YES_IMPOSSIBLE: STATE_NO_CERTAIN,
        STATE_NO_CERTAIN: STATE_YES_IMPOSSIBLE,
        STATE_NO_IMPOSSIBLE: STATE_YES_CERTAIN,
    }.get(state, state)


def _invert_final_state(state: str) -> str:
    if state == STATE_FINAL_YES:
        return STATE_FINAL_NO
    if state == STATE_FINAL_NO:
        return STATE_FINAL_YES
    return state


def _yes_early_state(spec: ContractSpec, observed_high_f: float | None, local_day_complete: bool) -> tuple[str, str]:
    if spec.contract_type == CONTRACT_UNKNOWN:
        return STATE_AMBIGUOUS, "contract_unclassified"
    if observed_high_f is None:
        return STATE_SOURCE_MISSING, "observed_high_missing"

    observed = float(observed_high_f)
    if spec.contract_type == CONTRACT_THRESHOLD:
        if spec.threshold_direction == "gte":
            if spec.low_f is None:
                return STATE_AMBIGUOUS, "threshold_missing"
            if observed >= spec.low_f:
                return STATE_YES_CERTAIN, "observed_high_touched_threshold"
            return STATE_STILL_POSSIBLE, "threshold_not_yet_touched"
        if spec.threshold_direction == "lte":
            if spec.high_f is None:
                return STATE_AMBIGUOUS, "threshold_missing"
            if observed > spec.high_f:
                return STATE_YES_IMPOSSIBLE, "observed_high_exceeded_upper_threshold"
            return STATE_STILL_POSSIBLE, "below_threshold_possible_until_local_close"

    if spec.contract_type in {CONTRACT_EXACT, CONTRACT_RANGE}:
        if spec.high_f is None:
            return STATE_AMBIGUOUS, "upper_bound_missing"
        if observed > spec.high_f:
            return STATE_YES_IMPOSSIBLE, "observed_high_exceeded_upper_bound"
        if local_day_complete:
            payout = yes_payout_for_high(spec, observed)
            return (STATE_YES_CERTAIN if payout == 1.0 else STATE_YES_IMPOSSIBLE), "local_day_complete"
        if spec.contract_type == CONTRACT_EXACT and spec.low_f is not None and observed >= spec.low_f:
            return STATE_STILL_POSSIBLE, "exact_high_touched_but_not_final"
        return STATE_STILL_POSSIBLE, "range_possible_until_local_close"

    return STATE_AMBIGUOUS, "contract_unclassified"


def settlement_state(
    spec: ContractSpec,
    observed_high_f: float | None,
    *,
    local_day_complete: bool = False,
    final_high_f: float | None = None,
) -> SettlementState:
    """Return side-specific early/final state for a high-temperature contract."""
    observed = final_high_f if final_high_f is not None else observed_high_f
    yes_early, reason = _yes_early_state(spec, observed, bool(local_day_complete or final_high_f is not None))
    payout = payout_for_high(spec, observed) if (local_day_complete or final_high_f is not None) else None

    final_state = STATE_UNRESOLVED
    if payout is not None and (local_day_complete or final_high_f is not None):
        final_state = STATE_FINAL_YES if payout == 1.0 else STATE_FINAL_NO

    early_state = _invert_early_state(yes_early) if spec.side == SIDE_NO else yes_early

    absorbing = early_state in {STATE_YES_CERTAIN, STATE_NO_CERTAIN, STATE_YES_IMPOSSIBLE, STATE_NO_IMPOSSIBLE} or final_state != STATE_UNRESOLVED
    return SettlementState(
        contract_type=spec.contract_type,
        side=spec.side,
        early_state=early_state,
        final_state=final_state,
        payout=payout,
        absorbing=absorbing,
        reason=reason,
    )


def legacy_bucket_state(state: SettlementState) -> str:
    """Map richer YES-side settlement states to the scanner's legacy names."""
    if state.early_state == STATE_SOURCE_MISSING:
        return "source_missing"
    if state.early_state == STATE_AMBIGUOUS:
        return "ambiguous"
    if state.final_state == STATE_FINAL_YES or state.early_state == STATE_YES_CERTAIN:
        return "already_won"
    if state.final_state == STATE_FINAL_NO or state.early_state == STATE_YES_IMPOSSIBLE:
        return "already_lost"
    return "still_possible"
