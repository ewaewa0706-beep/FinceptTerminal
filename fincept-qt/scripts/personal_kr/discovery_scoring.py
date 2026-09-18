"""Deterministic cross-sectional scoring for whole-market KR discovery.

The discovery layer intentionally avoids per-symbol API fan-out across the
entire KOSPI/KOSDAQ universe.  It scores only fields already frozen inside the
PIT universe snapshot, keeping discovery cheap, reproducible and safe to replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Iterable

from .models import QuantCandidate
from .universe import UniverseEntry


DISCOVERY_PROFILE_NAMES = ("balanced", "liquidity", "large_cap", "active")


@dataclass(frozen=True)
class DiscoveryWeights:
    """Relative weights for normalized 0..100 discovery factors."""

    liquidity: float = 0.50
    size: float = 0.25
    turnover: float = 0.15
    volume: float = 0.10

    def normalized(self) -> dict[str, float]:
        values = {
            "liquidity_score": float(self.liquidity),
            "size_score": float(self.size),
            "turnover_score": float(self.turnover),
            "volume_score": float(self.volume),
        }
        if any(not isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("discovery weights must be finite and >= 0")
        total = sum(values.values())
        if total <= 0:
            raise ValueError("at least one discovery weight must be positive")
        return {name: value / total for name, value in values.items()}


def weights_for_profile(name: str) -> DiscoveryWeights:
    profile = str(name).strip().lower()
    profiles = {
        "balanced": DiscoveryWeights(0.50, 0.25, 0.15, 0.10),
        "liquidity": DiscoveryWeights(0.70, 0.15, 0.05, 0.10),
        "large_cap": DiscoveryWeights(0.35, 0.50, 0.05, 0.10),
        "active": DiscoveryWeights(0.35, 0.10, 0.35, 0.20),
    }
    if profile not in profiles:
        raise ValueError(f"unknown discovery profile: {name}")
    return profiles[profile]


def score_universe_entries(
    entries: Iterable[UniverseEntry],
    analysis_date: date,
    *,
    limit: int = 20,
    weights: DiscoveryWeights | None = None,
) -> list[QuantCandidate]:
    """Rank frozen universe entries by cross-sectional market tradability.

    Factors are percentile-normalized over the same eligible universe:

    - liquidity: trading value (largest weight)
    - size: market capitalization
    - turnover: trading value / market cap, a simple activity proxy
    - volume: share volume

    Missing fields stay missing. Available factor weights are renormalized per
    stock instead of turning missing data into an artificial zero. Final ties are
    broken by ticker so replay is deterministic.
    """

    if limit < 1:
        raise ValueError("limit must be >= 1")
    items = list(entries)
    if not items:
        return []
    for entry in items:
        if entry.as_of != analysis_date:
            raise ValueError("universe entry date must match discovery analysis_date")

    raw: list[dict[str, float | None]] = []
    for entry in items:
        trading_value = _positive_or_none(entry.trading_value_krw)
        market_cap = _positive_or_none(entry.market_cap_krw)
        volume = _positive_or_none(entry.volume)
        turnover = None
        if trading_value is not None and market_cap is not None:
            turnover = trading_value / market_cap
        raw.append(
            {
                "trading_value_krw": trading_value,
                "market_cap_krw": market_cap,
                "turnover_ratio": turnover,
                "previous_volume": volume,
            }
        )

    percentiles = {
        "liquidity_score": _percentiles([row["trading_value_krw"] for row in raw]),
        "size_score": _percentiles([row["market_cap_krw"] for row in raw]),
        "turnover_score": _percentiles([row["turnover_ratio"] for row in raw]),
        "volume_score": _percentiles([row["previous_volume"] for row in raw]),
    }
    normalized_weights = (weights or DiscoveryWeights()).normalized()

    scored: list[tuple[float, str, QuantCandidate]] = []
    for index, entry in enumerate(items):
        factor_scores = {
            name: scores[index]
            for name, scores in percentiles.items()
            if scores[index] is not None
        }
        used_weight = sum(normalized_weights[name] for name in factor_scores)
        if used_weight <= 0:
            continue
        score = sum(
            factor_scores[name] * normalized_weights[name] for name in factor_scores
        ) / used_weight

        factors: dict[str, float] = {
            name: round(value, 6) for name, value in factor_scores.items()
        }
        for key, value in raw[index].items():
            if value is not None:
                factors[key] = float(value)
        candidate = QuantCandidate(
            instrument=entry.instrument,
            analysis_date=analysis_date,
            score=round(score, 6),
            factors=factors,
        )
        scored.append((candidate.score, candidate.instrument.ticker, candidate))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        QuantCandidate(
            instrument=candidate.instrument,
            analysis_date=candidate.analysis_date,
            score=candidate.score,
            rank=rank,
            factors=candidate.factors,
        )
        for rank, (_, _, candidate) in enumerate(scored[:limit], start=1)
    ]


def _positive_or_none(value: int | float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not isfinite(numeric) or numeric <= 0:
        return None
    return numeric


def _percentiles(values: list[float | None]) -> list[float | None]:
    indexed = [(index, value) for index, value in enumerate(values) if value is not None]
    scores: list[float | None] = [None] * len(values)
    if not indexed:
        return scores
    if len(indexed) == 1:
        scores[indexed[0][0]] = 50.0
        return scores

    ordered = sorted(indexed, key=lambda item: item[1])
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_rank = (cursor + end - 1) / 2.0
        percentile = average_rank / (len(ordered) - 1) * 100.0
        for position in range(cursor, end):
            scores[ordered[position][0]] = percentile
        cursor = end
    return scores
