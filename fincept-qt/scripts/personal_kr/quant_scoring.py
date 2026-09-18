"""Bounded KIS feature ranking for Personal-KR.

Whole-market discovery stays cheap and snapshot-based.  This module is the next
stage: only a small liquidity-prefiltered slice fans out to per-symbol KIS daily
bars and investor flow, then receives deterministic cross-sectional scores.
No LLM or order path is involved here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt
from statistics import fmean, pstdev
from typing import Iterable

from .models import FundamentalSnapshot, Instrument, InvestorFlowSnapshot, MarketSnapshot, QuantCandidate


QUANT_PROFILE_NAMES = ("balanced", "momentum", "flow", "defensive")


@dataclass(frozen=True)
class QuantWeights:
    momentum: float = 0.25
    flow: float = 0.25
    fundamental: float = 0.25
    liquidity: float = 0.15
    risk: float = 0.10

    def normalized(self) -> dict[str, float]:
        values = {
            "momentum_score": float(self.momentum),
            "flow_score": float(self.flow),
            "fundamental_score": float(self.fundamental),
            "liquidity_score": float(self.liquidity),
            "risk_score": float(self.risk),
        }
        if any(not isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("quant weights must be finite and >= 0")
        total = sum(values.values())
        if total <= 0:
            raise ValueError("at least one quant weight must be positive")
        return {name: value / total for name, value in values.items()}


@dataclass(frozen=True)
class QuantFeatureRecord:
    instrument: Instrument
    analysis_date: date
    market: MarketSnapshot
    flow: InvestorFlowSnapshot | None = None
    fundamentals: FundamentalSnapshot | None = None

    def __post_init__(self) -> None:
        if self.market.instrument != self.instrument:
            raise ValueError("market instrument does not match quant feature record")
        if self.market.as_of > self.analysis_date:
            raise ValueError("market snapshot is newer than quant analysis_date")
        if self.flow is not None and self.flow.as_of > self.analysis_date:
            raise ValueError("investor flow is newer than quant analysis_date")
        if self.fundamentals is not None and self.fundamentals.as_of > self.analysis_date:
            raise ValueError("fundamentals are newer than quant analysis_date")


def weights_for_quant_profile(name: str) -> QuantWeights:
    profile = str(name).strip().lower()
    profiles = {
        "balanced": QuantWeights(0.25, 0.25, 0.25, 0.15, 0.10),
        "momentum": QuantWeights(0.50, 0.15, 0.15, 0.10, 0.10),
        "flow": QuantWeights(0.15, 0.50, 0.15, 0.10, 0.10),
        "defensive": QuantWeights(0.15, 0.15, 0.30, 0.10, 0.30),
    }
    if profile not in profiles:
        raise ValueError(f"unknown quant profile: {name}")
    return profiles[profile]


def score_quant_records(
    records: Iterable[QuantFeatureRecord],
    analysis_date: date,
    *,
    limit: int = 10,
    weights: QuantWeights | None = None,
) -> tuple[list[QuantCandidate], list[dict[str, float | str]]]:
    """Extract, normalize and rank a bounded set of KIS feature records.

    Raw inputs are retained in the returned rows so the caller can freeze them
    into the ranking payload hash. Missing metrics remain missing and available
    weights are renormalized per stock rather than silently becoming zero.
    """

    if limit < 1:
        raise ValueError("quant ranking limit must be >= 1")
    items = list(records)
    if not items:
        return [], []
    if any(item.analysis_date != analysis_date for item in items):
        raise ValueError("quant feature records must share analysis_date")

    raw_rows = [_extract_raw(item) for item in items]
    metrics = {
        "momentum_20d_score": _percentiles(raw_rows, "momentum_20d"),
        "momentum_60d_score": _percentiles(raw_rows, "momentum_60d"),
        "foreign_flow_score": _percentiles(raw_rows, "foreign_flow_ratio"),
        "institution_flow_score": _percentiles(raw_rows, "institution_flow_ratio"),
        "operating_margin_score": _percentiles(raw_rows, "operating_margin"),
        "net_margin_score": _percentiles(raw_rows, "net_margin"),
        "equity_ratio_score": _percentiles(raw_rows, "equity_ratio"),
        "liquidity_score": _percentiles(raw_rows, "average_trading_value_20d"),
        "risk_score": _percentiles(raw_rows, "volatility_20d", higher_is_better=False),
    }
    normalized_weights = (weights or QuantWeights()).normalized()

    feature_rows: list[dict[str, float | str]] = []
    scored: list[tuple[float, str, QuantCandidate]] = []
    for index, item in enumerate(items):
        row: dict[str, float | str] = dict(raw_rows[index])
        component_scores: dict[str, float] = {}

        momentum_parts = [
            scores[index]
            for name, scores in metrics.items()
            if name in {"momentum_20d_score", "momentum_60d_score"}
            and scores[index] is not None
        ]
        if momentum_parts:
            component_scores["momentum_score"] = fmean(momentum_parts)

        flow_parts = [
            scores[index]
            for name, scores in metrics.items()
            if name in {"foreign_flow_score", "institution_flow_score"}
            and scores[index] is not None
        ]
        if flow_parts:
            component_scores["flow_score"] = fmean(flow_parts)

        fundamental_parts = [
            scores[index]
            for name, scores in metrics.items()
            if name in {"operating_margin_score", "net_margin_score", "equity_ratio_score"}
            and scores[index] is not None
        ]
        if fundamental_parts:
            component_scores["fundamental_score"] = fmean(fundamental_parts)

        for name in ("liquidity_score", "risk_score"):
            value = metrics[name][index]
            if value is not None:
                component_scores[name] = value

        used_weight = sum(normalized_weights[name] for name in component_scores)
        if used_weight <= 0:
            continue
        score = sum(
            component_scores[name] * normalized_weights[name] for name in component_scores
        ) / used_weight

        for name, value in component_scores.items():
            row[name] = round(value, 6)
        for name, scores in metrics.items():
            value = scores[index]
            if value is not None and name not in row:
                row[name] = round(value, 6)
        row["score"] = round(score, 6)
        feature_rows.append(row)

        factors = {
            key: float(value)
            for key, value in row.items()
            if key not in {"ticker", "name", "market", "score"}
            and isinstance(value, (int, float))
        }
        scored.append(
            (
                score,
                item.instrument.ticker,
                QuantCandidate(
                    instrument=item.instrument,
                    analysis_date=analysis_date,
                    score=round(score, 6),
                    factors=factors,
                ),
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [
        QuantCandidate(
            instrument=candidate.instrument,
            analysis_date=candidate.analysis_date,
            score=candidate.score,
            rank=rank,
            factors=candidate.factors,
        )
        for rank, (_, _, candidate) in enumerate(scored[:limit], start=1)
    ]
    rank_by_ticker = {candidate.instrument.ticker: candidate.rank for candidate in selected}
    feature_rows.sort(
        key=lambda row: (
            rank_by_ticker.get(str(row["ticker"]), 10**9),
            -float(row["score"]),
            str(row["ticker"]),
        )
    )
    return selected, feature_rows


def _extract_raw(record: QuantFeatureRecord) -> dict[str, float | str]:
    bars = tuple(record.market.bars)
    closes = [bar.close for bar in bars]
    trading_values = [bar.close * bar.volume for bar in bars]
    row: dict[str, float | str] = {
        "ticker": record.instrument.ticker,
        "name": record.instrument.name,
        "market": record.instrument.market,
    }
    if bars:
        row["market_data_as_of"] = bars[-1].trade_date.isoformat()
    average_trading_value = _mean_last(trading_values, 20)
    volatility = _annualized_volatility(closes, 20)
    momentum_20d = _period_return(closes, 20)
    momentum_60d = _period_return(closes, 60)
    for key, value in (
        ("average_trading_value_20d", average_trading_value),
        ("volatility_20d", volatility),
        ("momentum_20d", momentum_20d),
        ("momentum_60d", momentum_60d),
    ):
        if value is not None:
            row[key] = value

    flow = record.flow
    if flow is not None:
        aligned_volume = _latest_volume_on_or_before(bars, flow.as_of)
        if aligned_volume > 0:
            if flow.foreign_net_buy is not None:
                row["foreign_flow_ratio"] = float(flow.foreign_net_buy) / aligned_volume
            if flow.institution_net_buy is not None:
                row["institution_flow_ratio"] = float(flow.institution_net_buy) / aligned_volume
        row["flow_data_as_of"] = flow.as_of.isoformat()

    fundamentals = record.fundamentals
    if fundamentals is not None:
        operating_margin = _safe_ratio(fundamentals.operating_profit, fundamentals.revenue)
        net_margin = _safe_ratio(fundamentals.net_income, fundamentals.revenue)
        equity_ratio = _safe_ratio(fundamentals.equity, fundamentals.assets)
        for key, value in (
            ("operating_margin", operating_margin),
            ("net_margin", net_margin),
            ("equity_ratio", equity_ratio),
        ):
            if value is not None:
                row[key] = value
        row["fundamental_data_as_of"] = fundamentals.as_of.isoformat()
    return row


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    result = float(numerator) / float(denominator)
    return result if isfinite(result) else None


def _period_return(closes: list[float], sessions: int) -> float | None:
    if len(closes) <= sessions:
        return None
    base = closes[-(sessions + 1)]
    if base <= 0:
        return None
    result = closes[-1] / base - 1.0
    return result if isfinite(result) else None


def _annualized_volatility(closes: list[float], sessions: int) -> float | None:
    if len(closes) < 3:
        return None
    window = closes[-(sessions + 1) :]
    returns = [
        current / previous - 1.0
        for previous, current in zip(window, window[1:])
        if previous > 0
    ]
    if len(returns) < 2:
        return None
    result = pstdev(returns) * sqrt(252)
    return result if isfinite(result) else None


def _mean_last(values: list[float], sessions: int) -> float | None:
    if not values:
        return None
    result = fmean(values[-sessions:])
    return result if isfinite(result) else None


def _latest_volume_on_or_before(bars, as_of: date) -> float:
    eligible = [bar for bar in bars if bar.trade_date <= as_of]
    if not eligible:
        return 0.0
    return float(eligible[-1].volume)


def _percentiles(
    rows: list[dict[str, float | str]],
    key: str,
    *,
    higher_is_better: bool = True,
) -> list[float | None]:
    indexed: list[tuple[int, float]] = []
    for index, row in enumerate(rows):
        value = row.get(key)
        if not isinstance(value, (int, float)):
            continue
        numeric = float(value)
        if isfinite(numeric):
            indexed.append((index, numeric))
    scores: list[float | None] = [None] * len(rows)
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
        if not higher_is_better:
            percentile = 100.0 - percentile
        for position in range(cursor, end):
            scores[ordered[position][0]] = percentile
        cursor = end
    return scores
