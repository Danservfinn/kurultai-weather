"""Canonical weather label truth-tier definitions.

This module is the single source of truth for the label-quality tiers used by
scanner, calibration, tuning, and dashboard reporting.  Outcome/status strings
from older DB rows are intentionally mapped into these tiers so ambiguous legacy
names such as ``final_proxy_consensus`` never appear as an unexplained count.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class _CompatStrEnum(str, Enum):
    """Python 3.9-compatible subset of enum.StrEnum used by this project."""

    def __str__(self) -> str:
        return self.value


class TruthTier(_CompatStrEnum):
    """Auditable label provenance tiers, from weakest to strongest evidence."""

    RAW = "raw"
    PROVISIONAL = "provisional"
    SINGLE_PROVIDER_PROXY = "single_provider_proxy"
    MULTI_PROVIDER_PROXY_CONSENSUS = "multi_provider_proxy_consensus"
    OFFICIAL_NCEI = "official_ncei"
    PENDING = "pending"
    SKIPPED = "skipped"
    ERROR = "error"
    UNKNOWN = "unknown"


# Persisted label_attempts/training_rows statuses.  The first three names are
# legacy/compatibility statuses that still exist in historical DBs.
FINAL_LABEL_STATUS = "final"
OFFICIAL_FINAL_LABEL_STATUS = "final_official"
PROXY_FINAL_LABEL_STATUS = "final_proxy_consensus"
MULTI_PROVIDER_PROXY_CONSENSUS_LABEL_STATUS = TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value
SINGLE_PROVIDER_PROXY_LABEL_STATUS = TruthTier.SINGLE_PROVIDER_PROXY.value
PROVISIONAL_LABEL_STATUS = TruthTier.PROVISIONAL.value
PENDING_LABEL_STATUS = TruthTier.PENDING.value
SKIPPED_LABEL_STATUS = TruthTier.SKIPPED.value
ERROR_LABEL_STATUS = TruthTier.ERROR.value

FINAL_LABEL_OUTCOME_STATUSES = {
    FINAL_LABEL_STATUS,
    OFFICIAL_FINAL_LABEL_STATUS,
    PROXY_FINAL_LABEL_STATUS,
    MULTI_PROVIDER_PROXY_CONSENSUS_LABEL_STATUS,
}

LABEL_OUTCOME_STATUSES = {
    *FINAL_LABEL_OUTCOME_STATUSES,
    SINGLE_PROVIDER_PROXY_LABEL_STATUS,
    PROVISIONAL_LABEL_STATUS,
    PENDING_LABEL_STATUS,
    SKIPPED_LABEL_STATUS,
    ERROR_LABEL_STATUS,
}

# Dashboard/reporting order.  Pending/skipped/error are process buckets rather
# than truth evidence, but they remain here so every label attempt is accounted.
TRUTH_TIER_ORDER = (
    TruthTier.OFFICIAL_NCEI.value,
    TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value,
    TruthTier.SINGLE_PROVIDER_PROXY.value,
    TruthTier.PROVISIONAL.value,
    TruthTier.RAW.value,
    TruthTier.PENDING.value,
    TruthTier.SKIPPED.value,
    TruthTier.ERROR.value,
    TruthTier.UNKNOWN.value,
)

# Compatibility labels for existing dashboards/tests; values are explicit aliases
# to canonical tiers, not independent categories.
LEGACY_TRUTH_TIER_ALIASES = {
    "official_final": TruthTier.OFFICIAL_NCEI.value,
    FINAL_LABEL_STATUS: TruthTier.OFFICIAL_NCEI.value,
    OFFICIAL_FINAL_LABEL_STATUS: TruthTier.OFFICIAL_NCEI.value,
    PROXY_FINAL_LABEL_STATUS: TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value,
}


def normalized_provider_set(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw: list[Any] = []
        for part in values.replace(",", "+").split("+"):
            raw.append(part)
    else:
        raw = list(values)
    providers: list[str] = []
    for value in raw:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        for prefix in ("consensus:", "proxy_consensus:"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        if text and text not in providers:
            providers.append(text)
    return providers


def provider_count(provider_set: Any) -> int:
    return len(normalized_provider_set(provider_set))


def tier_for_label(outcome_status: str | None, provider_set: Any = "") -> str:
    status = str(outcome_status or "").strip().lower()
    status = LEGACY_TRUTH_TIER_ALIASES.get(status, status)
    providers = provider_count(provider_set)
    if status == TruthTier.OFFICIAL_NCEI.value:
        return TruthTier.OFFICIAL_NCEI.value
    if status == TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value:
        return TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value if providers >= 2 else TruthTier.SINGLE_PROVIDER_PROXY.value
    if status == TruthTier.SINGLE_PROVIDER_PROXY.value:
        return TruthTier.SINGLE_PROVIDER_PROXY.value
    if status == TruthTier.PROVISIONAL.value:
        return TruthTier.PROVISIONAL.value
    if status == TruthTier.RAW.value:
        return TruthTier.RAW.value
    if status == TruthTier.PENDING.value:
        return TruthTier.PENDING.value
    if status == TruthTier.SKIPPED.value:
        return TruthTier.SKIPPED.value
    if status == TruthTier.ERROR.value:
        return TruthTier.ERROR.value
    return TruthTier.UNKNOWN.value


def tier_counts() -> dict[str, int]:
    return {tier: 0 for tier in TRUTH_TIER_ORDER if tier != TruthTier.UNKNOWN.value}


def is_clean_calibration_tier(tier: str) -> bool:
    return tier in {TruthTier.OFFICIAL_NCEI.value, TruthTier.MULTI_PROVIDER_PROXY_CONSENSUS.value}
