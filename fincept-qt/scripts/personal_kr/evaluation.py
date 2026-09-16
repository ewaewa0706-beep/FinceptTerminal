"""Point-in-time forward return and benchmark-alpha calculations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from .models import OHLCVBar


@dataclass(frozen=True)
class Outcome:
    decision_id: str
    horizon: int
    start_date: date
    end_date: date
    raw_return: float
    benchmark_return: float | None
    alpha_return: float | None
    max_gain: float | None
    max_drawdown: float | None
    stock_ticker: str | None = None
    stock_source: str | None = None
    benchmark_symbol: str | None = None
    benchmark_source: str | None = None
    evaluated_at: datetime | None = None
    stock_input_hash: str | None = None
    benchmark_input_hash: str | None = None


def calculate_forward_return(
    decision_id: str,
    bars: Sequence[OHLCVBar],
    analysis_date: date,
    horizon: int,
    benchmark_bars: Sequence[OHLCVBar] | None = None,
    *,
    stock_ticker: str | None = None,
    stock_source: str | None = None,
    benchmark_symbol: str | None = None,
    benchmark_source: str | None = None,
) -> Outcome:
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    stock = _dedupe(bars)
    bench_by_date = {bar.trade_date: bar for bar in _dedupe(benchmark_bars or ())}
    # A research decision can be created intraday. Reconstructing that day's
    # final close later would leak information that was not available when the
    # decision was frozen. Attribute performance from the NEXT common trading
    # session's open instead; this is observable/executable only after the
    # decision and gives deterministic 1/5/20/60-session outcome semantics.
    starts = [i for i, bar in enumerate(stock) if bar.trade_date > analysis_date]
    if not starts:
        raise ValueError("no future trading session after analysis date")
    start_index = starts[0]
    end_index = start_index + horizon - 1
    if end_index >= len(stock):
        raise ValueError("insufficient future trading sessions")
    start, end = stock[start_index], stock[end_index]
    raw = (end.close - start.open) / start.open
    path = stock[start_index : end_index + 1]
    gain = max((bar.high / start.open - 1 for bar in path), default=None)
    drawdown = min((bar.low / start.open - 1 for bar in path), default=None)
    benchmark_return = None
    alpha = None
    benchmark_input_hash = None
    if benchmark_bars is not None:
        if start.trade_date not in bench_by_date or end.trade_date not in bench_by_date:
            raise ValueError("benchmark is missing the stock horizon start or end trading session")
        b0, b1 = bench_by_date[start.trade_date], bench_by_date[end.trade_date]
        benchmark_return = (b1.close - b0.open) / b0.open
        alpha = raw - benchmark_return
        benchmark_input_hash = _bars_hash((b0, b1))
    return Outcome(
        decision_id=decision_id,
        horizon=horizon,
        start_date=start.trade_date,
        end_date=end.trade_date,
        raw_return=raw,
        benchmark_return=benchmark_return,
        alpha_return=alpha,
        max_gain=gain,
        max_drawdown=drawdown,
        stock_ticker=stock_ticker,
        stock_source=stock_source,
        benchmark_symbol=benchmark_symbol,
        benchmark_source=benchmark_source,
        evaluated_at=datetime.now(timezone.utc),
        stock_input_hash=_bars_hash(path),
        benchmark_input_hash=benchmark_input_hash,
    )


def _dedupe(bars: Sequence[OHLCVBar]) -> list[OHLCVBar]:
    keyed = {bar.trade_date: bar for bar in bars}
    return [keyed[day] for day in sorted(keyed)]


def _bars_hash(bars: Sequence[OHLCVBar]) -> str:
    canonical = [
        {
            "trade_date": bar.trade_date.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
