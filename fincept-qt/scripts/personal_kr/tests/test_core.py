from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_kr.engine import ResearchEngine, _parse_signal
from personal_kr.evaluation import calculate_forward_return
from personal_kr.http import HttpResponse, RetryHttpClient
from personal_kr.llm import ScriptedLlm
from personal_kr.models import (
    FundamentalSnapshot,
    Instrument,
    InvestorFlowSnapshot,
    MacroSnapshot,
    MarketSnapshot,
    NewsItem,
    OHLCVBar,
    QuantCandidate,
    ResearchResult,
)
from personal_kr.persistence import DecisionStore
from personal_kr.ranking import select_top_candidates, select_top_candidates_isolated


def bars(step: float = 1.0, *, days: int = 12):
    return tuple(
        OHLCVBar(
            date(2026, 8, 20) + timedelta(days=i),
            100 + step * i,
            101 + step * i,
            99 + step * i,
            100 + step * i,
            1000,
        )
        for i in range(days)
    )


class FakeProviders:
    def __init__(self, *, fail_news=False):
        self.fail_news = fail_news

    def daily_bars(self, instrument, as_of, lookback_days=120):
        history = tuple(bar for bar in bars() if bar.trade_date <= as_of)
        return MarketSnapshot(instrument, as_of, history, "KIS")

    def investor_flow(self, instrument, as_of):
        return InvestorFlowSnapshot(as_of, "KIS", 1200, 900)

    def fundamentals(self, instrument, as_of):
        return FundamentalSnapshot(as_of, "DART", revenue=1000, operating_profit=100)

    def news(self, instrument, as_of, count=20):
        if self.fail_news:
            raise RuntimeError("naver down")
        return (NewsItem(datetime.combine(as_of, datetime.min.time(), timezone.utc), "news", "https://x"),)

    def macro(self, as_of):
        return MacroSnapshot(as_of, "ECOS", {"bok_base_rate": 2.5})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("005930", "삼성전자", "KOSPI")
        self.candidate = QuantCandidate(self.instrument, date(2026, 8, 20), 90, 1, {"flow": 80})

    def test_ranking_dedupes_and_selects_top_n(self):
        selected = select_top_candidates(
            [
                {"ticker": "005930", "name": "삼성전자", "market": "KOSPI", "score": 91},
                {"ticker": "000660", "name": "SK하이닉스", "market": "KOSPI", "score": 89},
                {"ticker": "005930", "name": "삼성전자", "market": "KOSPI", "score": 90},
            ],
            date(2026, 8, 20),
            1,
        )
        self.assertEqual([c.instrument.ticker for c in selected], ["005930"])
        self.assertEqual(selected[0].rank, 1)

    def test_ranking_isolates_malformed_external_rows_for_batch(self):
        selected, errors = select_top_candidates_isolated(
            [
                {"ticker": "005930", "name": "삼성전자", "market": "KOSPI", "score": 91},
                {"name": "missing ticker", "score": 50},
                {"ticker": "000660", "name": "SK하이닉스", "market": "NYSE", "score": 89},
            ],
            date(2026, 8, 20),
            5,
        )
        self.assertEqual([c.instrument.ticker for c in selected], ["005930"])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("ticker" in message for message in errors.values()))
        self.assertTrue(any("KOSPI or KOSDAQ" in message for message in errors.values()))

    def test_zero_ticker_rejected(self):
        with self.assertRaises(ValueError):
            Instrument("000000", "invalid")

    def test_market_future_bar_rejected(self):
        with self.assertRaises(ValueError):
            MarketSnapshot(
                self.instrument,
                date(2026, 8, 20),
                (OHLCVBar(date(2026, 8, 21), 1, 1, 1, 1, 1),),
            )

    def test_invalid_ohlc_is_rejected(self):
        with self.assertRaises(ValueError):
            OHLCVBar(date(2026, 8, 20), 100, 99, 90, 100, 1)
        with self.assertRaises(ValueError):
            OHLCVBar(date(2026, 8, 20), 0, 1, 1, 1, 1)

    def test_signal_parser_uses_last_explicit_signal_line(self):
        text = "Rejected alternative:\nSIGNAL: BUY\nFinal decision:\nSIGNAL: SELL"
        self.assertEqual(_parse_signal(text), "Sell")

    def test_partial_data_continues_when_news_fails(self):
        providers = FakeProviders(fail_news=True)
        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=ScriptedLlm(),
        )
        packet = engine.packet(self.candidate)
        self.assertIn("news", packet.unavailable)
        self.assertEqual(packet.news, ())

    def test_full_agent_chain_runs_and_returns_hold(self):
        providers = FakeProviders()
        llm = ScriptedLlm(["ok"] * 8 + ["portfolio\nSIGNAL: BUY"])
        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=llm,
        )
        result = engine.analyze(self.candidate)
        self.assertEqual(result.signal, "Buy")
        self.assertEqual(len(llm.calls), 9)

    def test_batch_isolates_one_failure(self):
        class SometimesBad(FakeProviders):
            def daily_bars(self, instrument, as_of, lookback_days=120):
                if instrument.ticker == "000660":
                    raise RuntimeError("KIS unavailable")
                return super().daily_bars(instrument, as_of, lookback_days)

        providers = SometimesBad()
        engine = ResearchEngine(market=providers, llm=ScriptedLlm())
        other = QuantCandidate(Instrument("000660", "SK하이닉스"), self.candidate.analysis_date, 80, 2)
        results, errors = engine.analyze_many([self.candidate, other])
        self.assertEqual([r.candidate.instrument.ticker for r in results], ["005930"])
        self.assertIn("000660", errors)

    def test_http_retries_500_then_succeeds(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if len(calls) < 3:
                return HttpResponse(500, b'{"error":"temporary"}', {})
            return HttpResponse(200, b'{"ok":true}', {})

        client = RetryHttpClient(attempts=3, backoff=0, transport=transport, sleep=lambda _: None)
        self.assertEqual(client.get_json("https://example.invalid"), {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_evaluation_uses_common_dates_and_alpha(self):
        stock = bars(1.0, days=10)
        benchmark = bars(0.5, days=10)
        out = calculate_forward_return("d1", stock, date(2026, 8, 20), 5, benchmark)
        expected_raw = (105 - 101) / 101
        expected_bench = (102.5 - 100.5) / 100.5
        self.assertAlmostEqual(out.raw_return, expected_raw)
        self.assertAlmostEqual(out.benchmark_return or 0, expected_bench)
        self.assertAlmostEqual(out.alpha_return or 0, expected_raw - expected_bench)
        self.assertEqual(out.start_date, date(2026, 8, 21))

    def test_outcome_first_write_wins(self):
        from personal_kr.evaluation import Outcome

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            decision = store.record_decision(
                ResearchResult(
                    candidate=self.candidate,
                    signal="Hold",
                    market_report="m",
                    fundamentals_report="f",
                    news_macro_report="n",
                    bull_case="b+",
                    bear_case="b-",
                    research_manager="r",
                    trader="t",
                    risk_manager="risk",
                    portfolio_manager="p",
                )
            )
            first = Outcome(
                decision.decision_id or "",
                5,
                date(2026, 8, 20),
                date(2026, 8, 25),
                0.05,
                0.02,
                0.03,
                0.06,
                -0.01,
            )
            second = Outcome(
                decision.decision_id or "",
                5,
                date(2026, 8, 20),
                date(2026, 8, 25),
                0.50,
                0.20,
                0.30,
                0.60,
                -0.10,
            )
            stored_first = store.record_outcome(first)
            stored_second = store.record_outcome(second)
            self.assertEqual(stored_first.raw_return, 0.05)
            self.assertEqual(stored_second.raw_return, 0.05)

    def test_outcome_requires_matching_decision_provenance(self):
        from personal_kr.evaluation import Outcome

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            with self.assertRaisesRegex(ValueError, "decision not found"):
                store.record_outcome(
                    Outcome(
                        "missing",
                        5,
                        date(2026, 8, 20),
                        date(2026, 8, 25),
                        0.01,
                        0.0,
                        0.01,
                        0.02,
                        -0.01,
                        stock_ticker="005930",
                    )
                )

    def test_decision_first_write_wins_and_paper_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db", initial_cash=1000)
            paper_date = datetime.now(timezone(timedelta(hours=9))).date()
            base = ResearchResult(
                candidate=self.candidate,
                signal="Hold",
                market_report="m",
                fundamentals_report="f",
                news_macro_report="n",
                bull_case="b+",
                bear_case="b-",
                research_manager="r",
                trader="t",
                risk_manager="risk",
                portfolio_manager="p",
            )
            first = store.record_decision(base)
            second = store.record_decision(ResearchResult(**{**base.__dict__, "signal": "Buy"}))
            self.assertEqual(second.decision_id, first.decision_id)
            self.assertEqual(second.signal, "Hold")
            trade_id = store.add_paper_trade(
                trade_date=paper_date,
                ticker="005930",
                side="BUY",
                quantity=2,
                price=100,
                decision_id=first.decision_id,
                client_trade_id="decision-buy-1",
            )
            cash, positions = store.paper_summary()
            self.assertEqual(cash, 800)
            self.assertEqual(positions["005930"], 2)
            replay_id = store.add_paper_trade(
                trade_date=paper_date,
                ticker="005930",
                side="BUY",
                quantity=2,
                price=100,
                decision_id=first.decision_id,
                client_trade_id="decision-buy-1",
            )
            self.assertEqual(replay_id, trade_id)
            replay_cash, replay_positions = store.paper_summary()
            self.assertEqual((replay_cash, replay_positions), (cash, positions))
            with self.assertRaisesRegex(ValueError, "replay conflicts"):
                store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="BUY",
                    quantity=1,
                    price=100,
                    decision_id=first.decision_id,
                    client_trade_id="decision-buy-1",
                )
            with self.assertRaises(ValueError):
                store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="SELL",
                    quantity=3,
                    price=100,
                    decision_id=first.decision_id,
                    client_trade_id="oversell-1",
                )
            with self.assertRaisesRegex(ValueError, "decision_id is required"):
                store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="BUY",
                    quantity=1,
                    price=100,
                    client_trade_id="missing-decision",
                )
            with self.assertRaisesRegex(ValueError, "client_trade_id is required"):
                store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="BUY",
                    quantity=1,
                    price=100,
                    decision_id=first.decision_id,
                )
            with self.assertRaisesRegex(ValueError, "finite"):
                store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="BUY",
                    quantity=1,
                    price=float("nan"),
                    decision_id=first.decision_id,
                    client_trade_id="nan-price",
                )

            # The ledger is append-only in execution chronology. A later insert
            # cannot backdate itself and borrow shares/cash from a future trade.
            store.add_paper_trade(
                trade_date=paper_date + timedelta(days=2),
                ticker="005930",
                side="BUY",
                quantity=1,
                price=100,
                decision_id=first.decision_id,
                client_trade_id="later-buy",
            )
            with self.assertRaisesRegex(ValueError, "backdated"):
                store.add_paper_trade(
                    trade_date=paper_date + timedelta(days=1),
                    ticker="005930",
                    side="SELL",
                    quantity=1,
                    price=100,
                    decision_id=first.decision_id,
                    client_trade_id="backdated-sell",
                )

    def test_decision_first_write_is_scoped_by_strategy_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            base = ResearchResult(
                candidate=self.candidate,
                signal="Hold",
                market_report="m",
                fundamentals_report="f",
                news_macro_report="n",
                bull_case="b+",
                bear_case="b-",
                research_manager="r",
                trader="t",
                risk_manager="risk",
                portfolio_manager="p",
            )
            manual = store.record_decision(base, strategy_id="personal-kr-ui")
            quant = store.record_decision(
                ResearchResult(**{**base.__dict__, "signal": "Buy"}),
                strategy_id="personal-kr-quant",
            )
            self.assertNotEqual(manual.decision_id, quant.decision_id)
            self.assertEqual(manual.strategy_id, "personal-kr-ui")
            self.assertEqual(quant.strategy_id, "personal-kr-quant")
            self.assertEqual(quant.signal, "Buy")

    def test_concurrent_decision_and_paper_writes_remain_first_write_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db", initial_cash=10_000)
            base = ResearchResult(
                candidate=self.candidate,
                signal="Hold",
                market_report="m",
                fundamentals_report="f",
                news_macro_report="n",
                bull_case="b+",
                bear_case="b-",
                research_manager="r",
                trader="t",
                risk_manager="risk",
                portfolio_manager="p",
            )

            def write_decision(index: int):
                candidate = ResearchResult(**{**base.__dict__, "signal": "Buy" if index % 2 else "Hold"})
                return store.record_decision(candidate, strategy_id="concurrent")

            with ThreadPoolExecutor(max_workers=8) as pool:
                decisions = list(pool.map(write_decision, range(16)))
            ids = {item.decision_id for item in decisions}
            signals = {item.signal for item in decisions}
            self.assertEqual(len(ids), 1)
            self.assertEqual(len(signals), 1)
            decision_id = next(iter(ids))
            paper_date = datetime.now(timezone(timedelta(hours=9))).date()

            def write_paper(_: int):
                return store.add_paper_trade(
                    trade_date=paper_date,
                    ticker="005930",
                    side="BUY",
                    quantity=1,
                    price=100,
                    decision_id=decision_id,
                    client_trade_id="concurrent-paper-1",
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                trade_ids = list(pool.map(write_paper, range(16)))
            self.assertEqual(len(set(trade_ids)), 1)
            cash, positions = store.paper_summary()
            self.assertEqual(cash, 9_900)
            self.assertEqual(positions, {"005930": 1})

    def test_legacy_paper_rows_without_provenance_are_quarantined_on_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE kr_decisions(
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL, signal TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(strategy_id,ticker,analysis_date)
                );
                CREATE TABLE kr_outcomes(
                    decision_id TEXT NOT NULL, horizon INTEGER NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(decision_id,horizon)
                );
                CREATE TABLE kr_paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, client_trade_id TEXT, decision_id TEXT,
                    trade_date TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
                    quantity INTEGER NOT NULL, price REAL NOT NULL, fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                );
                INSERT INTO kr_paper_trades(client_trade_id,decision_id,trade_date,ticker,side,quantity,price)
                VALUES(NULL,NULL,'2026-08-20','005930','BUY',10,100);
                """
            )
            conn.commit()
            conn.close()

            store = DecisionStore(path, initial_cash=10_000)
            cash, positions = store.paper_summary()
            self.assertEqual(cash, 10_000)
            self.assertEqual(positions, {})
            check = sqlite3.connect(path)
            quarantine = check.execute("SELECT COUNT(*) FROM kr_paper_trade_quarantine").fetchone()[0]
            active = check.execute("SELECT COUNT(*) FROM kr_paper_trades").fetchone()[0]
            info = {row[1]: row for row in check.execute("PRAGMA table_info(kr_paper_trades)")}
            check.close()
            self.assertEqual((quarantine, active), (1, 0))
            self.assertEqual(info["client_trade_id"][3], 1)
            self.assertEqual(info["decision_id"][3], 1)

    def test_json_contract_is_valid(self):
        payload = {"success": True, "data": {"ticker": self.instrument.ticker}, "error": None}
        self.assertEqual(json.loads(json.dumps(payload))["data"]["ticker"], "005930")


if __name__ == "__main__":
    unittest.main()
