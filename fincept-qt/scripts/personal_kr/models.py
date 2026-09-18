"""Strict data contracts for the personal Korean-market workflow."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any


# A deliberately conservative daily-bar finality boundary. Regular KRX trading
# ends at 15:30, but exchange-designated special sessions can close later (for
# example, the annual CSAT schedule has historically shifted the close to 16:30).
# Without a trading-calendar dependency, 17:00 KST keeps date-only KIS/Yahoo bars
# out of immutable research/outcome evidence until even those delayed sessions
# have ended.
KR_DAILY_FINALITY_TIME = time(17, 0)


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
    price_mode: str = "original"

    def __post_init__(self) -> None:
        as_of = _date(self.as_of)
        object.__setattr__(self, "as_of", as_of)
        bars = tuple(self.bars)
        if any(bar.trade_date > as_of for bar in bars):
            raise ValueError("market snapshot contains future bars")
        price_mode = str(self.price_mode).strip().lower()
        if price_mode not in {"original", "adjusted"}:
            raise ValueError("market price_mode must be original or adjusted")
        object.__setattr__(self, "price_mode", price_mode)
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

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            raise ValueError("news published_at must include a timezone")


@dataclass(frozen=True)
class MacroSnapshot:
    as_of: date
    source: str
    indicators: dict[str, float | str | None] = field(default_factory=dict)
    series_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _date(self.as_of))
        object.__setattr__(
            self,
            "series_errors",
            {str(key): str(value) for key, value in self.series_errors.items()},
        )


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
    analysis_cutoff_at: datetime | None = None
    analysis_cutoff_mode: str = ""
    ranking_mode: str = ""
    ranking_data_as_of: date | None = None

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
        ranking_mode = str(self.ranking_mode or "").strip().lower()
        if ranking_mode not in {"", "observed", "historical_reconstruction"}:
            raise ValueError("ranking_mode must be observed or historical_reconstruction")
        object.__setattr__(self, "ranking_mode", ranking_mode)
        ranking_data_as_of = (
            _date(self.ranking_data_as_of) if self.ranking_data_as_of is not None else None
        )
        object.__setattr__(self, "ranking_data_as_of", ranking_data_as_of)
        if ranking_mode == "historical_reconstruction":
            if self.ranking_generated_at is None:
                raise ValueError("historical_reconstruction requires ranking_generated_at")
            if ranking_data_as_of is None:
                raise ValueError("historical_reconstruction requires ranking_data_as_of")
        if ranking_data_as_of is not None and ranking_data_as_of > self.analysis_date:
            raise ValueError("ranking_data_as_of cannot be later than analysis_date")
        if self.analysis_cutoff_at is not None and self.analysis_cutoff_at.tzinfo is None:
            raise ValueError("analysis_cutoff_at must include a timezone")
        cutoff_mode = str(self.analysis_cutoff_mode or "").strip().lower()
        if self.analysis_cutoff_at is None:
            if cutoff_mode not in {"", "date"}:
                raise ValueError("analysis_cutoff_mode requires analysis_cutoff_at")
            cutoff_mode = "date"
        else:
            # Persisted decisions created before cutoff-mode provenance existed
            # are conservatively interpreted as externally frozen PIT cutoffs.
            cutoff_mode = cutoff_mode or "external"
            if cutoff_mode not in {"external", "live_request"}:
                raise ValueError("analysis_cutoff_mode must be external or live_request")
        object.__setattr__(self, "analysis_cutoff_mode", cutoff_mode)
        if self.analysis_cutoff_at is not None:
            kst = timezone(timedelta(hours=9))
            if self.analysis_cutoff_at.astimezone(kst).date() > self.analysis_date:
                raise ValueError("analysis_cutoff_at cannot be later than analysis_date")
            if (
                ranking_data_as_of is not None
                and ranking_data_as_of > self.analysis_cutoff_at.astimezone(kst).date()
            ):
                raise ValueError("ranking_data_as_of cannot be later than analysis_cutoff_at")


@dataclass(frozen=True)
class ResearchPacket:
    candidate: QuantCandidate
    market: MarketSnapshot
    fundamentals: FundamentalSnapshot | None = None
    flow: InvestorFlowSnapshot | None = None
    news: tuple[NewsItem, ...] = ()
    macro: MacroSnapshot | None = None
    unavailable: tuple[str, ...] = ()
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cutoff_date = self.candidate.analysis_date
        cutoff_at = self.candidate.analysis_cutoff_at
        if cutoff_at is not None:
            cutoff_date = min(cutoff_date, cutoff_at.astimezone(timezone(timedelta(hours=9))).date())
        if self.market.as_of > cutoff_date:
            raise ValueError("market snapshot is newer than analysis_date")
        if self.fundamentals and self.fundamentals.as_of > cutoff_date:
            raise ValueError("fundamentals are newer than analysis_date")
        if self.flow and self.flow.as_of > cutoff_date:
            raise ValueError("investor flow is newer than analysis_date")
        if self.macro and self.macro.as_of > cutoff_date:
            raise ValueError("macro snapshot is newer than analysis_date")
        if cutoff_at is not None:
            if any(item.published_at > cutoff_at.astimezone(item.published_at.tzinfo) for item in self.news):
                raise ValueError("news contains articles newer than analysis_cutoff_at")
        elif any(item.published_at.astimezone(timezone(timedelta(hours=9))).date() > cutoff_date for item in self.news):
            raise ValueError("news contains future articles")
        object.__setattr__(
            self,
            "unavailable_reasons",
            {str(key): str(value) for key, value in self.unavailable_reasons.items()},
        )


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
    unavailable_reasons: dict[str, str] = field(default_factory=dict)
    decision_id: str | None = None
    strategy_id: str = "personal-kr"
    generated_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    llm_provider: str = ""
    llm_model_id: str = ""
    workflow_version: str = "personal-kr-v1"

    def __post_init__(self) -> None:
        if self.signal not in {"Buy", "Hold", "Sell"}:
            raise ValueError("signal must be Buy, Hold, or Sell")


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
