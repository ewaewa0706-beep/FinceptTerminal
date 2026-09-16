"""On-demand Korean research packet assembly and multi-stage AI analysis."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from .llm import Llm
from .models import (
    FundamentalSnapshot,
    InvestorFlowSnapshot,
    MacroSnapshot,
    MarketSnapshot,
    NewsItem,
    QuantCandidate,
    ResearchPacket,
    ResearchResult,
    to_jsonable,
)


class MarketProvider(Protocol):
    def daily_bars(self, instrument, as_of, lookback_days: int = 120) -> MarketSnapshot: ...


class FlowProvider(Protocol):
    def investor_flow(self, instrument, as_of) -> InvestorFlowSnapshot: ...


class FundamentalProvider(Protocol):
    def fundamentals(self, instrument, as_of) -> FundamentalSnapshot: ...


class NewsProvider(Protocol):
    def news(self, instrument, as_of, count: int = 20) -> tuple[NewsItem, ...]: ...


class MacroProvider(Protocol):
    def macro(self, as_of) -> MacroSnapshot: ...


class ResearchEngine:
    """Fincept-native orchestration with market data as the only hard dependency."""

    def __init__(
        self,
        *,
        market: MarketProvider,
        llm: Llm,
        flow: FlowProvider | None = None,
        fundamentals: FundamentalProvider | None = None,
        news: NewsProvider | None = None,
        macro: MacroProvider | None = None,
    ) -> None:
        self.market = market
        self.flow = flow
        self.fundamentals = fundamentals
        self.news = news
        self.macro = macro
        self.llm = llm

    def packet(self, candidate: QuantCandidate, *, lookback_days: int = 120) -> ResearchPacket:
        market = self.market.daily_bars(candidate.instrument, candidate.analysis_date, lookback_days)
        unavailable: list[str] = []

        def optional(name: str, fn):
            try:
                return fn()
            except Exception:
                unavailable.append(name)
                return None

        flow = (
            optional("investor_flow", lambda: self.flow.investor_flow(candidate.instrument, candidate.analysis_date))
            if self.flow
            else None
        )
        if self.flow is None:
            unavailable.append("investor_flow")
        fundamentals = (
            optional("fundamentals", lambda: self.fundamentals.fundamentals(candidate.instrument, candidate.analysis_date))
            if self.fundamentals
            else None
        )
        if self.fundamentals is None:
            unavailable.append("fundamentals")
        news_items = (
            optional("news", lambda: self.news.news(candidate.instrument, candidate.analysis_date, 20))
            if self.news
            else None
        )
        if self.news is None:
            unavailable.append("news")
        macro = optional("macro", lambda: self.macro.macro(candidate.analysis_date)) if self.macro else None
        if self.macro is None:
            unavailable.append("macro")
        return ResearchPacket(
            candidate=candidate,
            market=market,
            fundamentals=fundamentals,
            flow=flow,
            news=tuple(news_items or ()),
            macro=macro,
            unavailable=tuple(dict.fromkeys(unavailable)),
        )

    def analyze(self, candidate: QuantCandidate) -> ResearchResult:
        packet = self.packet(candidate)
        compact = json.dumps(to_jsonable(packet), ensure_ascii=False, separators=(",", ":"))
        identity = f"{candidate.instrument.name}({candidate.instrument.ticker}, {candidate.instrument.market})"
        cutoff = candidate.analysis_date.isoformat()
        base_rule = (
            f"분석 기준일은 {cutoff}이다. 기준일 이후 정보는 사용하지 말고, 결측 데이터는 추정하지 말고 명시하라. "
            "한국 주식시장 맥락과 원화 기준을 우선한다."
        )

        market_report = self.llm.complete(
            "당신은 한국 주식시장 Market Analyst다. 가격/거래량/외국인·기관 수급을 객관적으로 분석한다. " + base_rule,
            f"대상: {identity}\nResearchPacket JSON:\n{compact}\n시장/수급 보고서를 한국어로 작성하라.",
        )
        fundamentals_report = self.llm.complete(
            "당신은 DART 공시 중심의 Fundamentals Analyst다. 공시 접수일을 정보 가용시점으로 취급한다. " + base_rule,
            f"대상: {identity}\nResearchPacket JSON:\n{compact}\n재무/공시 보고서를 한국어로 작성하라.",
        )
        news_macro_report = self.llm.complete(
            "당신은 한국 기업 뉴스 및 거시환경 Analyst다. Naver 뉴스와 한국은행 거시지표를 구분해 해석한다. "
            + base_rule,
            f"대상: {identity}\nResearchPacket JSON:\n{compact}\n뉴스/거시 보고서를 한국어로 작성하라.",
        )
        evidence = "\n\n".join(
            [
                "[Market]\n" + market_report,
                "[Fundamentals]\n" + fundamentals_report,
                "[News/Macro]\n" + news_macro_report,
            ]
        )
        bull_case = self.llm.complete(
            "당신은 Bull Researcher다. 제공된 증거 안에서만 상승 논리를 구성하고 반증 조건을 제시한다. " + base_rule,
            f"대상: {identity}\n{evidence}",
        )
        bear_case = self.llm.complete(
            "당신은 Bear Researcher다. 제공된 증거 안에서만 하락/실패 논리를 구성하고 리스크 촉발조건을 제시한다. "
            + base_rule,
            f"대상: {identity}\n{evidence}",
        )
        debate = evidence + "\n\n[Bull]\n" + bull_case + "\n\n[Bear]\n" + bear_case
        research_manager = self.llm.complete(
            "당신은 Research Manager다. Bull/Bear를 균형있게 검토해 핵심 쟁점, 추가확인사항, 잠정판단을 정리한다. "
            + base_rule,
            f"대상: {identity}\n{debate}",
        )
        trader = self.llm.complete(
            "당신은 Trader다. Research Manager의 판단을 실행 관점으로 번역하되 실제 주문은 제출하지 않는다. " + base_rule,
            f"대상: {identity}\nResearch Manager:\n{research_manager}\n가격/수급:\n{market_report}",
        )
        risk_manager = self.llm.complete(
            "당신은 Risk Manager다. 최대 손실 요인, 데이터 결측, 이벤트 리스크, 포지션 위험을 검토한다. " + base_rule,
            f"대상: {identity}\n{debate}\nTrader:\n{trader}",
        )
        portfolio_manager = self.llm.complete(
            "당신은 Portfolio Manager다. research_only 모드다. 실제 주문을 내리지 말고 최종 연구 신호만 결정한다. "
            "마지막 줄은 반드시 SIGNAL: BUY, SIGNAL: HOLD, SIGNAL: SELL 중 하나다. "
            + base_rule,
            f"대상: {identity}\nResearch Manager:\n{research_manager}\nTrader:\n{trader}\nRisk:\n{risk_manager}",
        )
        signal = _parse_signal(portfolio_manager)
        llm_provider = str(getattr(self.llm, "provider", self.llm.__class__.__name__)).lower()
        llm_model = str(getattr(self.llm, "model", ""))
        return ResearchResult(
            candidate=candidate,
            signal=signal,
            market_report=market_report,
            fundamentals_report=fundamentals_report,
            news_macro_report=news_macro_report,
            bull_case=bull_case,
            bear_case=bear_case,
            research_manager=research_manager,
            trader=trader,
            risk_manager=risk_manager,
            portfolio_manager=portfolio_manager,
            unavailable=packet.unavailable,
            generated_at=datetime.now(timezone.utc),
            evidence=to_jsonable(packet),
            llm_provider=llm_provider,
            llm_model_id=llm_model,
            workflow_version="personal-kr-v1",
        )

    def analyze_many(self, candidates: list[QuantCandidate]) -> tuple[list[ResearchResult], dict[str, str]]:
        results: list[ResearchResult] = []
        errors: dict[str, str] = {}
        for candidate in candidates:
            try:
                results.append(self.analyze(candidate))
            except Exception as exc:  # isolate one bad Top-N name from the rest
                errors[candidate.instrument.ticker] = str(exc)
        return results, errors


def with_decision_id(result: ResearchResult, decision_id: str) -> ResearchResult:
    return replace(result, decision_id=decision_id)


def _parse_signal(text: str) -> str:
    import re

    # Portfolio Manager is required to put the final signal on the last line.
    # Use the last explicit marker rather than checking BUY first: a rationale
    # can legitimately mention rejected alternatives ("not SIGNAL: BUY") before
    # ending with SIGNAL: SELL.
    matches = re.findall(r"(?im)^\s*SIGNAL\s*:\s*(BUY|HOLD|SELL)\s*$", text)
    if matches:
        return matches[-1].title()
    return "Hold"
