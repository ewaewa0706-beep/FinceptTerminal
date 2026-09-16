"""External Quant Ranking -> deterministic Top-N contract."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .models import Instrument, QuantCandidate


_KNOWN = {
    "ticker",
    "symbol",
    "code",
    "name",
    "company_name",
    "market",
    "exchange",
    "score",
    "total_score",
    "rank",
    "analysis_date",
}


def candidate_from_mapping(row: dict[str, Any], analysis_date: date | str) -> QuantCandidate:
    ticker = row.get("ticker") or row.get("symbol") or row.get("code")
    if ticker is None or not str(ticker).strip():
        raise ValueError("ranking row requires ticker/symbol/code")
    name = row.get("name") or row.get("company_name") or str(ticker)
    market = row.get("market") or row.get("exchange") or "KOSPI"
    score = row.get("score", row.get("total_score"))
    if score is None:
        raise ValueError("ranking row requires score/total_score")
    factors: dict[str, float] = {}
    for key, value in row.items():
        if key in _KNOWN or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            factors[str(key)] = float(value)
    row_date = row.get("analysis_date")
    if row_date is not None and date.fromisoformat(str(row_date)) != date.fromisoformat(str(analysis_date)):
        raise ValueError("mixed analysis dates are not allowed")
    rank_value = row.get("rank")
    return QuantCandidate(
        instrument=Instrument(str(ticker).zfill(6), str(name), str(market)),
        analysis_date=date.fromisoformat(str(analysis_date)),
        score=float(score),
        rank=int(rank_value) if rank_value not in (None, "") else None,
        factors=factors,
    )


def select_top_candidates(
    rows: Iterable[dict[str, Any]], analysis_date: date | str, limit: int = 5
) -> list[QuantCandidate]:
    candidates = [candidate_from_mapping(row, analysis_date) for row in rows]
    return _select_candidates(candidates, limit)


def select_top_candidates_isolated(
    rows: Iterable[dict[str, Any]], analysis_date: date | str, limit: int = 5
) -> tuple[list[QuantCandidate], dict[str, str]]:
    """Select Top-N while isolating malformed external ranking rows."""

    candidates: list[QuantCandidate] = []
    errors: dict[str, str] = {}
    for index, row in enumerate(rows):
        try:
            candidates.append(candidate_from_mapping(row, analysis_date))
        except Exception as exc:
            label = str(row.get("ticker") or row.get("symbol") or row.get("code") or f"row:{index}")
            key = label if label not in errors else f"{label}@{index}"
            errors[key] = str(exc)
    return _select_candidates(candidates, limit), errors


def _select_candidates(candidates: Iterable[QuantCandidate], limit: int) -> list[QuantCandidate]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    best: dict[str, QuantCandidate] = {}
    for candidate in candidates:
        previous = best.get(candidate.instrument.ticker)
        if previous is None or candidate.score > previous.score:
            best[candidate.instrument.ticker] = candidate
    ordered = sorted(
        best.values(),
        key=lambda item: (-item.score, item.rank or 10**9, item.instrument.ticker),
    )[:limit]
    return [
        QuantCandidate(
            instrument=item.instrument,
            analysis_date=item.analysis_date,
            score=item.score,
            rank=index,
            factors=item.factors,
        )
        for index, item in enumerate(ordered, start=1)
    ]
