"""Strict data contracts for the personal Korean-market workflow."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


def _date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _finite(value: float | int, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def validate_ticker(value: str) -> str:
    ticker = str(value).strip()
    if len(ticker) != 6 or not ticker.isdigit() or ticker == "000000":
        raise ValueError("ticker must be a non-zero 6-digit Korean listing code")
    return ticker


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    market: str = "KOSPI"
    currency: str = "KRW"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", validate_ticker(self.ticker))
        market = self.market.upper().strip()
        if market not in {"KOSPI", "KOSDAQ"}:
            raise ValueError("market must be KOSPI or KOSDAQ")
        object.__setattr__(self, "market", market)
        if not self.name.strip():
            raise ValueError("instrument name is required")

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.ticker}.{'KS' if self.market == 'KOSPI' else 'KQ'}"

    @property
    def benchmark_symbol(self) -> str:
        return "^KS11" if self.market == "KOSPI" else "^KQ11"


@dataclass(frozen=True)
class OHLCVBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_date", _date(self.trade_date))
        for name in ("open", "high", "low", "close"):
            value = _finite(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} must be > 0")
            object.__setattr__(self, name, value)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below another OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above another OHLC value")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True)
class MarketSnapshot:
    instrument: Instrument
    as_of: date
    bars: tuple[OHLCVBar, ...]
    source: str = "KIS"

    def __post_init__(self) -> None:
        as_of = _date(self.as_of)
        object.__setattr__(self, "as_of", as_of)
        bars = tuple(self.bars)
        if any(bar.trade_date > as_of for bar in bars):
            raise ValueError("market snapshot contains future bars")
        object.__setattr__(self, "bars", tuple(sorted(bars, key=lambda b: b.trade_date)))


@dataclass(frozen=True)
class FundamentalSnapshot:
    as_of: date
    source: str
    revenue: float | None = None
    operating_profit: float | None = None
    net_income: float | None = None
    assets: float | None = None
    liabilities: float | None = None
    equity: float | None = None
    filing_receipt_no: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _date(self.as_of))


@dataclass(frozen=True)
class InvestorFlowSnapshot:
    as_of: date
    source: str
    foreign_net_buy: float | None = None
    institution_net_buy: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _date(self.as_of))


@dataclass(frozen=True)
class NewsItem:
    published_at: datetime
    title: str
    link: str
    source: str = "Naver"


@dataclass(frozen=True)
class MacroSnapshot:
    as_of: date
    source: str
    indicators: dict[str, float | str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _date(self.as_of))


@dataclass(frozen=True)
class QuantCandidate:
    instrument: Instrument
    analysis_date: date
    score: float
    rank: int | None = None
    factors: dict[str, float] = field(default_factory=dict)
    ranking_source: str = ""
    ranking_generated_at: datetime | None = None
    ranking_payload_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "analysis_date", _date(self.analysis_date))
        object.__setattr__(self, "score", _finite(self.score, "score"))
        if self.rank is not None and self.rank < 1:
            raise ValueError("rank must be >= 1")
        cleaned: dict[str, float] = {}
        for key, value in self.factors.items():
            cleaned[str(key)] = _finite(value, f"factor {key}")
        object.__setattr__(self, "factors", cleaned)
        if self.ranking_generated_at is not None and self.ranking_generated_at.tzinfo is None:
            raise ValueError("ranking_generated_at must include a timezone")


@dataclass(frozen=True)
class ResearchPacket:
    candidate: QuantCandidate
    market: MarketSnapshot
    fundamentals: FundamentalSnapshot | None = None
    flow: InvestorFlowSnapshot | None = None
    news: tuple[NewsItem, ...] = ()
    macro: MacroSnapshot | None = None
    unavailable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cutoff = self.candidate.analysis_date
        if self.market.as_of > cutoff:
            raise ValueError("market snapshot is newer than analysis_date")
        if self.fundamentals and self.fundamentals.as_of > cutoff:
            raise ValueError("fundamentals are newer than analysis_date")
        if self.flow and self.flow.as_of > cutoff:
            raise ValueError("investor flow is newer than analysis_date")
        if self.macro and self.macro.as_of > cutoff:
            raise ValueError("macro snapshot is newer than analysis_date")
        if any(item.published_at.date() > cutoff for item in self.news):
            raise ValueError("news contains future articles")


@dataclass(frozen=True)
class ResearchResult:
    candidate: QuantCandidate
    signal: str
    market_report: str
    fundamentals_report: str
    news_macro_report: str
    bull_case: str
    bear_case: str
    research_manager: str
    trader: str
    risk_manager: str
    portfolio_manager: str
    unavailable: tuple[str, ...] = ()
    decision_id: str | None = None
    strategy_id: str = "personal-kr"
    generated_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    llm_provider: str = ""
    llm_model_id: str = ""
    workflow_version: str = "personal-kr-v1"


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/dates/tuples into JSON-safe values."""

    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value
