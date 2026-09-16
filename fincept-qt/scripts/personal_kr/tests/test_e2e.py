from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_kr.engine import ResearchEngine
from personal_kr.evaluation import calculate_forward_return
from personal_kr.llm import ScriptedLlm
from personal_kr.models import (
    FundamentalSnapshot,
    InvestorFlowSnapshot,
    MacroSnapshot,
    MarketSnapshot,
    NewsItem,
    OHLCVBar,
)
from personal_kr.persistence import DecisionStore
from personal_kr.ranking import select_top_candidates


def history(step: float, count: int = 25):
    return tuple(
        OHLCVBar(
            date(2026, 8, 20) + timedelta(days=i),
            100 + step * i,
            101 + step * i,
            99 + step * i,
            100 + step * i,
            1000,
        )
        for i in range(count)
    )


class FullProviders:
    def __init__(self):
        self.flow_called = False

    def daily_bars(self, instrument, as_of, lookback_days=120):
        eligible = tuple(bar for bar in history(1) if bar.trade_date <= as_of)
        return MarketSnapshot(instrument, as_of, eligible, "KIS")

    def investor_flow(self, instrument, as_of):
        self.flow_called = True
        return InvestorFlowSnapshot(as_of, "KIS", foreign_net_buy=5000, institution_net_buy=3000)

    def fundamentals(self, instrument, as_of):
        return FundamentalSnapshot(as_of, "DART", revenue=10_000, operating_profit=2_000, net_income=1_500)

    def news(self, instrument, as_of, count=20):
        return (
            NewsItem(
                datetime.combine(as_of, datetime.min.time(), timezone.utc),
                "삼성전자 테스트 뉴스",
                "https://example.test/news",
            ),
        )

    def macro(self, as_of):
        return MacroSnapshot(as_of, "ECOS", {"bok_base_rate": 2.5, "usdkrw": 1350.0})


class PersonalKrTerminalE2ETests(unittest.TestCase):
    def test_external_quant_to_research_decision_outcome_and_paper(self):
        rows = [
            {
                "ticker": "005930",
                "name": "삼성전자",
                "market": "KOSPI",
                "score": 92.5,
                "technical": 90,
                "flow": 95,
                "fundamental": 88,
            },
            {
                "ticker": "000660",
                "name": "SK하이닉스",
                "market": "KOSPI",
                "score": 89.0,
                "technical": 91,
                "flow": 84,
                "fundamental": 86,
            },
        ]
        candidates = select_top_candidates(rows, date(2026, 8, 20), limit=1)
        self.assertEqual(candidates[0].instrument.ticker, "005930")
        self.assertEqual(candidates[0].factors["flow"], 95)

        providers = FullProviders()
        llm = ScriptedLlm(
            [
                "시장/외국인/기관 수급",
                "DART 재무",
                "Naver/ECOS",
                "Bull case",
                "Bear case",
                "Research Manager",
                "Trader",
                "Risk Manager",
                "Portfolio Manager\nSIGNAL: HOLD",
            ]
        )
        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=llm,
        )
        result = engine.analyze(candidates[0])
        self.assertTrue(providers.flow_called)
        self.assertEqual(result.signal, "Hold")
        self.assertEqual(result.unavailable, ())
        self.assertEqual(len(llm.calls), 9)
        self.assertIn("외국인·기관 수급", llm.calls[0][0])
        self.assertIsNotNone(result.generated_at)
        self.assertEqual(result.evidence["flow"]["foreign_net_buy"], 5000)
        self.assertEqual(result.evidence["fundamentals"]["source"], "DART")
        self.assertEqual(result.evidence["news"][0]["title"], "삼성전자 테스트 뉴스")
        self.assertEqual(result.llm_provider, "scripted")
        self.assertEqual(result.llm_model_id, "deterministic-test-double")
        self.assertEqual(result.workflow_version, "personal-kr-v1")

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "research.db", initial_cash=1_000_000)
            stored = store.record_decision(result)
            self.assertIsNotNone(stored.decision_id)

            stock = history(1.0, 25)
            benchmark = history(0.5, 25)
            outcome = calculate_forward_return(
                stored.decision_id or "",
                stock,
                candidates[0].analysis_date,
                5,
                benchmark,
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="original",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
            )
            frozen = store.record_outcome(outcome)
            expected_raw = (105 - 101) / 101
            expected_benchmark = (102.5 - 100.5) / 100.5
            self.assertAlmostEqual(frozen.alpha_return or 0, expected_raw - expected_benchmark)

            store.add_paper_trade(
                decision_id=stored.decision_id,
                client_trade_id="e2e-buy-1",
                trade_date=datetime.now(timezone(timedelta(hours=9))).date(),
                ticker="005930",
                side="BUY",
                quantity=1,
                price=100_000,
            )
            cash, positions = store.paper_summary()
            self.assertEqual(cash, 900_000)
            self.assertEqual(positions, {"005930": 1})

        # The KR workflow intentionally exposes no live-order stage; its final
        # AI action is a research signal and the only execution exercised above
        # is the isolated paper ledger.
        self.assertEqual(result.signal, "Hold")


if __name__ == "__main__":
    unittest.main()
