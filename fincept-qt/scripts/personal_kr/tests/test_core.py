from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import personal_kr.cli as cli
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
    to_jsonable,
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

        selected, errors = select_top_candidates_isolated(
            [
                {"ticker": "005930", "market": "KOSPI", "score": 91},
                {"ticker": "000660", "name": "SK하이닉스", "score": 89},
            ],
            date(2026, 8, 20),
            5,
        )
        self.assertEqual(selected, [])
        self.assertTrue(any("explicit name" in message for message in errors.values()))
        self.assertTrue(any("explicit market" in message for message in errors.values()))

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

    def test_signal_parser_fails_closed_without_explicit_marker(self):
        with self.assertRaisesRegex(ValueError, "explicit SIGNAL"):
            _parse_signal("The portfolio should probably hold for now.")
        with self.assertRaisesRegex(ValueError, "explicit SIGNAL"):
            _parse_signal("SIGNAL: WAIT")
        with self.assertRaisesRegex(ValueError, "explicit SIGNAL"):
            _parse_signal("signal: hold")
        self.assertEqual(_parse_signal("rationale\nSIGNAL: HOLD"), "Hold")
        with self.assertRaisesRegex(ValueError, "must end"):
            _parse_signal("SIGNAL: BUY\ntrailing explanation")

    def test_exact_intraday_cutoff_conservatively_excludes_date_only_same_day_data(self):
        class RecordingProviders(FakeProviders):
            def __init__(self):
                super().__init__()
                self.market_as_of = None
                self.flow_as_of = None
                self.fundamental_as_of = None
                self.news_cutoff = None
                self.macro_called = False

            def daily_bars(self, instrument, as_of, lookback_days=120):
                self.market_as_of = as_of
                return MarketSnapshot(instrument, as_of, (), "KIS")

            def investor_flow(self, instrument, as_of):
                self.flow_as_of = as_of
                return InvestorFlowSnapshot(as_of, "KIS", 1, 1)

            def fundamentals(self, instrument, as_of):
                self.fundamental_as_of = as_of
                return FundamentalSnapshot(as_of, "DART", revenue=1)

            def news(self, instrument, as_of, count=20, *, cutoff_at=None):
                self.news_cutoff = cutoff_at
                return (NewsItem(cutoff_at - timedelta(minutes=1), "known", "https://known"),)

            def macro(self, as_of):
                self.macro_called = True
                return MacroSnapshot(as_of, "ECOS", {})

        kst = timezone(timedelta(hours=9))
        # Still conservatively pre-final even on a delayed-close special session.
        cutoff = datetime(2026, 8, 20, 16, 45, tzinfo=kst)
        candidate = QuantCandidate(
            self.instrument,
            date(2026, 8, 20),
            90,
            1,
            {},
            analysis_cutoff_at=cutoff,
        )
        providers = RecordingProviders()
        packet = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=ScriptedLlm(),
        ).packet(candidate)

        self.assertEqual(providers.market_as_of, date(2026, 8, 19))
        self.assertEqual(providers.flow_as_of, date(2026, 8, 19))
        self.assertEqual(providers.fundamental_as_of, date(2026, 8, 19))
        self.assertEqual(providers.news_cutoff, cutoff)
        self.assertFalse(providers.macro_called)
        self.assertIn("macro", packet.unavailable)
        self.assertIn("exact intraday PIT", packet.unavailable_reasons["macro"])

    def test_manual_today_candidate_freezes_exact_kst_request_cutoff(self):
        kst = timezone(timedelta(hours=9))
        now = datetime(2026, 9, 16, 10, 15, 30, tzinfo=kst)
        payload = {
            "analysis_date": "2026-09-16",
            "instrument": {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
            "score": 0,
        }
        with patch.object(cli, "_korea_now", return_value=now):
            candidate = cli._candidate_from_payload(payload)
        self.assertEqual(candidate.analysis_cutoff_at, now)
        self.assertEqual(candidate.analysis_cutoff_mode, "live_request")

        explicit = datetime(2026, 9, 16, 9, 5, tzinfo=kst)
        payload["analysis_cutoff_at"] = explicit.isoformat()
        with patch.object(cli, "_korea_now", return_value=now):
            candidate = cli._candidate_from_payload(payload)
        self.assertEqual(candidate.analysis_cutoff_at, explicit)
        self.assertEqual(candidate.analysis_cutoff_mode, "external")

    def test_direct_discovery_candidate_preserves_and_validates_ranking_provenance(self):
        kst = timezone(timedelta(hours=9))
        now = datetime(2026, 9, 16, 10, 15, 30, tzinfo=kst)
        cutoff = datetime(2026, 9, 16, 9, 30, tzinfo=kst)
        generated = datetime(2026, 9, 16, 9, 0, tzinfo=kst)
        payload = {
            "analysis_date": "2026-09-16",
            "instrument": {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
            "score": 91.5,
            "rank": 1,
            "factors": {"liquidity_score": 99.0, "size_score": 98.0},
            "ranking_source": "fincept-kis-public-master-cross-sectional-v2/balanced",
            "ranking_generated_at": generated.isoformat(),
            "ranking_payload_hash": "a" * 64,
            "analysis_cutoff_at": cutoff.isoformat(),
            "analysis_cutoff_mode": "external",
        }
        with patch.object(cli, "_korea_now", return_value=now):
            candidate = cli._candidate_from_payload(payload)

        self.assertEqual(candidate.ranking_source, payload["ranking_source"])
        self.assertEqual(candidate.ranking_generated_at, generated)
        self.assertEqual(candidate.ranking_payload_hash, "a" * 64)
        self.assertEqual(candidate.analysis_cutoff_at, cutoff)
        self.assertEqual(candidate.analysis_cutoff_mode, "external")

        incomplete = dict(payload)
        incomplete.pop("ranking_payload_hash")
        with patch.object(cli, "_korea_now", return_value=now):
            with self.assertRaisesRegex(ValueError, "must be supplied together"):
                cli._candidate_from_payload(incomplete)

        after_cutoff = dict(payload)
        after_cutoff["ranking_generated_at"] = datetime(2026, 9, 16, 9, 45, tzinfo=kst).isoformat()
        with patch.object(cli, "_korea_now", return_value=now):
            with self.assertRaisesRegex(ValueError, "analysis_cutoff_at"):
                cli._candidate_from_payload(after_cutoff)

    def test_krx_historical_reconstruction_keeps_actual_generation_time_and_data_cutoff(self):
        kst = timezone(timedelta(hours=9))
        now = datetime(2026, 9, 18, 15, 30, tzinfo=kst)
        cutoff = datetime(2026, 9, 15, 17, 0, tzinfo=kst)
        payload = {
            "analysis_date": "2026-09-15",
            "instrument": {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
            "score": 91.5,
            "rank": 1,
            "factors": {"liquidity_score": 99.0},
            "ranking_source": (
                "fincept-krx-openapi-historical-cross-sectional-v2/balanced"
                ";data_as_of=2026-09-15"
            ),
            "ranking_generated_at": now.isoformat(),
            "ranking_payload_hash": "b" * 64,
            "ranking_mode": "historical_reconstruction",
            "ranking_data_as_of": "2026-09-15",
            "analysis_cutoff_at": cutoff.isoformat(),
            "analysis_cutoff_mode": "external",
        }
        with patch.object(cli, "_korea_now", return_value=now):
            candidate = cli._candidate_from_payload(payload)

        self.assertEqual(candidate.ranking_generated_at, now)
        self.assertEqual(candidate.analysis_cutoff_at, cutoff)
        self.assertEqual(candidate.ranking_mode, "historical_reconstruction")
        self.assertEqual(candidate.ranking_data_as_of, date(2026, 9, 15))
        self.assertIn("data_as_of=2026-09-15", candidate.ranking_source)

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "research.db")
            result = ResearchResult(
                candidate=candidate,
                signal="Hold",
                market_report="m",
                fundamentals_report="f",
                news_macro_report="n",
                bull_case="b+",
                bear_case="b-",
                research_manager="r",
                trader="t",
                risk_manager="risk",
                portfolio_manager="SIGNAL: HOLD",
            )
            stored = store.record_decision(result, strategy_id="krx-reconstruction-roundtrip")
            loaded = store.get_decision(stored.decision_id or "")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.candidate.ranking_mode, "historical_reconstruction")
        self.assertEqual(loaded.candidate.ranking_data_as_of, date(2026, 9, 15))
        self.assertEqual(loaded.candidate.ranking_generated_at, now)

        generic = dict(payload)
        generic["ranking_source"] = "external-ranking-v1"
        generic["ranking_mode"] = "observed"
        with patch.object(cli, "_korea_now", return_value=now):
            with self.assertRaisesRegex(ValueError, "later than analysis_date"):
                cli._candidate_from_payload(generic)

        future_data = dict(payload)
        future_data["ranking_data_as_of"] = "2026-09-16"
        with patch.object(cli, "_korea_now", return_value=now):
            with self.assertRaisesRegex(ValueError, "ranking_data_as_of"):
                cli._candidate_from_payload(future_data)

    def test_discover_uses_krx_for_missing_historical_snapshot_without_backdating_capture(self):
        kst = timezone(timedelta(hours=9))
        today = date(2026, 9, 18)
        as_of = date(2026, 9, 15)
        now = datetime(2026, 9, 18, 15, 30, tzinfo=kst)

        class FakeKrxClient:
            def get_daily_trading(self, market, requested):
                self.asserted = requested
                ticker = "005930" if market == "KOSPI" else "247540"
                value = "300" if market == "KOSPI" else "200"
                return [
                    {
                        "ISU_CD": ticker,
                        "ACC_TRDVOL": "100",
                        "ACC_TRDVAL": value,
                        "MKTCAP": "1000",
                    }
                ]

            def get_base_info(self, market, requested):
                ticker = "005930" if market == "KOSPI" else "247540"
                name = "삼성전자" if market == "KOSPI" else "에코프로비엠"
                return [
                    {
                        "ISU_SRT_CD": ticker,
                        "ISU_ABBRV": name,
                        "SECUGRP_NM": "주권",
                        "KIND_STKCERT_TP_NM": "보통주",
                    }
                ]

        args = SimpleNamespace(
            analysis_date=as_of.isoformat(),
            limit=2,
            min_trading_value_krw=0,
            market=None,
            profile="balanced",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "research.db")
            with (
                patch.dict(
                    cli.os.environ,
                    {"KRX_AUTH_KEY": "test", "KIS_APP_KEY": "", "KIS_APP_SECRET": ""},
                    clear=False,
                ),
                patch.object(cli, "_store", return_value=store),
                patch.object(cli, "_korea_today", return_value=today),
                patch.object(cli, "_korea_now", return_value=now),
                patch.object(cli.KrxClient, "from_env", return_value=FakeKrxClient()),
            ):
                data = cli.cmd_discover(args)

                self.assertIsNone(
                    store.get_universe_snapshot(as_of, markets=["KOSPI", "KOSDAQ"])
                )
                self.assertEqual(data["source"], "KRX OpenAPI historical reconstruction")
                self.assertIsNone(data["snapshot_hash"])
                self.assertIsNone(data["captured_at"])
                self.assertEqual(data["reconstructed_at"], now)
                self.assertEqual(data["resolved_data_date"], as_of)
                self.assertIn("data_as_of=2026-09-15", data["ranking_source"])
                self.assertEqual(data["ranking_generated_at"], now)
                self.assertEqual(data["ranking_mode"], "historical_reconstruction")
                self.assertEqual(data["ranking_data_as_of"], as_of)
                self.assertEqual(
                    data["candidates"][0].analysis_cutoff_at,
                    datetime(2026, 9, 15, 17, 0, tzinfo=kst),
                )

                round_trip = cli._candidate_from_payload(
                    to_jsonable(data["candidates"][0])
                )
                self.assertEqual(round_trip.ranking_generated_at, now)
                self.assertEqual(round_trip.ranking_mode, "historical_reconstruction")
                self.assertEqual(round_trip.ranking_data_as_of, as_of)
                self.assertIn("data_as_of=2026-09-15", round_trip.ranking_source)

    def test_historical_reconstruction_ranking_can_flow_into_batch_without_fake_cutoff(self):
        kst = timezone(timedelta(hours=9))
        analysis_date = date(2026, 9, 15)
        generated_at = datetime(2026, 9, 18, 15, 30, tzinfo=kst)
        payload = {
            "analysis_date": analysis_date.isoformat(),
            "ranking_source": (
                "fincept-krx-openapi-historical-cross-sectional-v2/balanced"
                ";data_as_of=2026-09-15"
            ),
            "ranking_generated_at": generated_at.isoformat(),
            "ranking_mode": "historical_reconstruction",
            "ranking_data_as_of": analysis_date.isoformat(),
            "limit": 1,
            "rows": [
                {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "market": "KOSPI",
                    "score": 90,
                }
            ],
        }

        class RecordingStore:
            def __init__(self):
                self.candidate = None

            def record_decision(self, result, *, strategy_id):
                self.candidate = result.candidate
                return result

        class Engine:
            def analyze(self, candidate):
                return ResearchResult(
                    candidate=candidate,
                    signal="Hold",
                    market_report="m",
                    fundamentals_report="f",
                    news_macro_report="n",
                    bull_case="b+",
                    bear_case="b-",
                    research_manager="r",
                    trader="t",
                    risk_manager="risk",
                    portfolio_manager="SIGNAL: HOLD",
                )

        store = RecordingStore()
        with (
            patch.object(cli, "_input_json", return_value=payload),
            patch.object(cli, "_korea_now", return_value=generated_at),
            patch.object(cli, "_engine", return_value=Engine()),
            patch.object(cli, "_store", return_value=store),
        ):
            result = cli.cmd_batch()

        self.assertEqual(len(result["results"]), 1)
        self.assertIsNotNone(store.candidate)
        self.assertEqual(store.candidate.ranking_mode, "historical_reconstruction")
        self.assertEqual(store.candidate.ranking_data_as_of, analysis_date)
        self.assertEqual(store.candidate.ranking_generated_at, generated_at)
        self.assertEqual(
            store.candidate.analysis_cutoff_at,
            datetime(2026, 9, 15, 17, 0, tzinfo=kst),
        )

    def test_live_request_cutoff_keeps_observed_current_macro(self):
        kst = timezone(timedelta(hours=9))
        cutoff = datetime(2026, 9, 16, 10, 15, tzinfo=kst)
        candidate = QuantCandidate(
            Instrument("005930", "삼성전자", "KOSPI"),
            date(2026, 9, 16),
            0,
            1,
            {},
            analysis_cutoff_at=cutoff,
            analysis_cutoff_mode="live_request",
        )
        providers = FakeProviders()
        packet = ResearchEngine(
            market=providers,
            macro=providers,
            llm=ScriptedLlm(),
        ).packet(candidate)
        self.assertIsNotNone(packet.macro)
        self.assertNotIn("macro", packet.unavailable)

    def test_live_request_cutoff_mode_roundtrips_decision_store(self):
        kst = timezone(timedelta(hours=9))
        candidate = QuantCandidate(
            Instrument("005930", "삼성전자", "KOSPI"),
            date(2026, 9, 16),
            0,
            1,
            {},
            analysis_cutoff_at=datetime(2026, 9, 16, 10, 15, tzinfo=kst),
            analysis_cutoff_mode="live_request",
        )
        result = ResearchResult(
            candidate=candidate,
            signal="Hold",
            market_report="m",
            fundamentals_report="f",
            news_macro_report="n",
            bull_case="b+",
            bear_case="b-",
            research_manager="r",
            trader="t",
            risk_manager="risk",
            portfolio_manager="SIGNAL: HOLD",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            stored = store.record_decision(result, strategy_id="cutoff-roundtrip")
            loaded = store.get_decision(stored.decision_id or "")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.candidate.analysis_cutoff_mode, "live_request")
        self.assertEqual(loaded.candidate.analysis_cutoff_at, candidate.analysis_cutoff_at)

    def test_optional_provider_programming_error_fails_candidate(self):
        class BuggyNews(FakeProviders):
            error_type = TypeError

            def news(self, instrument, as_of, count=20):
                raise self.error_type("developer bug")

        providers = BuggyNews()
        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=ScriptedLlm(),
        )
        for error_type in (TypeError, KeyError, IndexError, ValueError):
            with self.subTest(error_type=error_type.__name__):
                providers.error_type = error_type
                with self.assertRaises(error_type):
                    engine.packet(self.candidate)

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
        self.assertEqual(packet.unavailable_reasons["news"], "RuntimeError: naver down")

    def test_partial_data_reason_is_sanitized_and_persisted(self):
        class LeakyNews(FakeProviders):
            def news(self, instrument, as_of, count=20):
                raise RuntimeError("GET https://example.invalid/path?api_key=secret token=abc123 failed")

        providers = LeakyNews()
        llm = ScriptedLlm(["ok"] * 8 + ["SIGNAL: HOLD"])
        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=llm,
        )
        result = engine.analyze(self.candidate)
        reason = result.unavailable_reasons["news"]
        self.assertIn("RuntimeError", reason)
        self.assertIn("<redacted-url>", reason)
        self.assertNotIn("secret", reason)
        self.assertNotIn("abc123", reason)
        with tempfile.TemporaryDirectory() as tmp:
            stored = DecisionStore(Path(tmp) / "kr.db").record_decision(result)
            loaded = DecisionStore(Path(tmp) / "kr.db").get_decision(stored.decision_id or "")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.unavailable_reasons, result.unavailable_reasons)

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
        engine = ResearchEngine(market=providers, llm=ScriptedLlm(["ok"] * 8 + ["SIGNAL: HOLD"]))
        other = QuantCandidate(Instrument("000660", "SK하이닉스"), self.candidate.analysis_date, 80, 2)
        results, errors = engine.analyze_many([self.candidate, other])
        self.assertEqual([r.candidate.instrument.ticker for r in results], ["005930"])
        self.assertIn("000660", errors)

    def test_batch_isolates_malformed_portfolio_manager_signal(self):
        providers = FakeProviders()
        bad = QuantCandidate(Instrument("000660", "SK하이닉스"), self.candidate.analysis_date, 80, 2)

        class PerTickerLlm:
            provider = "scripted"
            model = "per-ticker"

            def __init__(self):
                self.calls = 0

            def complete(self, system, user):
                self.calls += 1
                ticker = "000660" if "000660" in user else "005930"
                stage = (self.calls - 1) % 9
                if stage == 8:
                    return "no explicit final marker" if ticker == "000660" else "SIGNAL: BUY"
                return "ok"

        engine = ResearchEngine(
            market=providers,
            flow=providers,
            fundamentals=providers,
            news=providers,
            macro=providers,
            llm=PerTickerLlm(),
        )
        results, errors = engine.analyze_many([self.candidate, bad])
        self.assertEqual([r.candidate.instrument.ticker for r in results], ["005930"])
        self.assertRegex(errors["000660"], "explicit SIGNAL")

    def test_cli_batch_checkpoints_each_success_before_analyzing_next_candidate(self):
        payload = {
            "analysis_date": "2026-08-20",
            "ranking_source": "unit-quant-v1",
            "ranking_generated_at": "2026-08-20T16:30:00+09:00",
            "limit": 2,
            "rows": [
                {"ticker": "005930", "name": "삼성전자", "market": "KOSPI", "score": 90},
                {"ticker": "000660", "name": "SK하이닉스", "market": "KOSPI", "score": 80},
            ],
        }

        class RecordingStore:
            def __init__(self):
                self.recorded = []

            def record_decision(self, result, *, strategy_id):
                self.recorded.append(result.candidate.instrument.ticker)
                return result

        store = RecordingStore()

        class CheckpointAwareEngine:
            def analyze(self, candidate):
                if candidate.instrument.ticker == "000660":
                    if store.recorded != ["005930"]:
                        raise RuntimeError("first decision was not checkpointed")
                    raise RuntimeError("second candidate failed")
                return ResearchResult(
                    candidate=candidate,
                    signal="Hold",
                    market_report="m",
                    fundamentals_report="f",
                    news_macro_report="n",
                    bull_case="b+",
                    bear_case="b-",
                    research_manager="r",
                    trader="t",
                    risk_manager="risk",
                    portfolio_manager="SIGNAL: HOLD",
                )

        with patch.object(cli, "_input_json", return_value=payload), patch.object(
            cli, "_engine", return_value=CheckpointAwareEngine()
        ), patch.object(cli, "_store", return_value=store):
            result = cli.cmd_batch()

        self.assertEqual(store.recorded, ["005930"])
        self.assertEqual([item.candidate.instrument.ticker for item in result["results"]], ["005930"])
        self.assertEqual(result["errors"]["000660"], "second candidate failed")

    def test_cli_evaluate_reports_pending_horizons_without_writing_partial_outcome(self):
        decision = ResearchResult(
            candidate=QuantCandidate(Instrument("005930", "삼성전자", "KOSPI"), date(2026, 9, 15), 90, 1),
            signal="Hold",
            market_report="m",
            fundamentals_report="f",
            news_macro_report="n",
            bull_case="b+",
            bear_case="b-",
            research_manager="r",
            trader="t",
            risk_manager="risk",
            portfolio_manager="SIGNAL: HOLD",
            decision_id="decision-1",
        )

        class FakeStore:
            def get_decision(self, decision_id):
                return decision if decision_id == "decision-1" else None

            def record_outcome(self, outcome):
                raise AssertionError("pending horizons must not be persisted")

        class FakeKis:
            def daily_bars(self, instrument, as_of, lookback_days=120, *, price_mode="original"):
                self.price_mode = price_mode
                self.as_of = as_of
                return MarketSnapshot(
                    instrument,
                    as_of,
                    (OHLCVBar(date(2026, 9, 15), 100, 101, 99, 100, 1000),),
                    "KIS",
                    price_mode,
                )

        fake_kis = FakeKis()
        args = SimpleNamespace(decision_id="decision-1", horizons=[1, 5])
        with patch.object(cli, "_store", return_value=FakeStore()), patch.object(
            cli.KisClient, "from_env", return_value=fake_kis
        ), patch.object(cli, "load_yahoo_benchmark", return_value=()), patch.object(
            cli,
            "_korea_now",
            return_value=datetime(2026, 9, 16, 16, 45, tzinfo=timezone(timedelta(hours=9))),
        ):
            result = cli.cmd_evaluate(args)

        self.assertEqual(fake_kis.price_mode, "adjusted")
        self.assertEqual(fake_kis.as_of, date(2026, 9, 15))
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(result["pending_horizons"], [1, 5])

    def test_llm_smoke_uses_supplied_active_profile_over_stdin_contract(self):
        class FakeLlm:
            provider = "openai"
            model = "gpt-test"

            def complete(self, system, user):
                self.call = (system, user)
                return "FINCEPT_KR_LLM_OK"

        fake = FakeLlm()
        config = {"provider": "openai", "model_id": "gpt-test", "api_key": "secret"}
        with patch.object(cli, "_optional_input_json", return_value={"llm": config}), patch.object(
            cli, "llm_from_payload", return_value=fake
        ) as factory:
            result = cli.cmd_llm_smoke()

        factory.assert_called_once_with(config)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-test")
        self.assertEqual(result["source"], "fincept_active_profile")
        self.assertEqual(result["response"], "FINCEPT_KR_LLM_OK")

    def test_llm_smoke_tty_falls_back_without_reading_stdin(self):
        class TtyOnly:
            def isatty(self):
                return True

        class FakeLlm:
            provider = "google"
            model = "gemini-test"

            def complete(self, system, user):
                return "FINCEPT_KR_LLM_OK"

        with patch.object(cli.sys, "stdin", TtyOnly()), patch.object(
            cli, "llm_from_payload", return_value=FakeLlm()
        ) as factory:
            result = cli.cmd_llm_smoke()

        factory.assert_called_once_with(None)
        self.assertEqual(result["source"], "headless_google_env")
        self.assertEqual(result["provider"], "google")

    def test_cli_evaluate_admits_current_daily_bar_only_after_finality_cutoff(self):
        decision = ResearchResult(
            candidate=QuantCandidate(Instrument("005930", "삼성전자", "KOSPI"), date(2026, 9, 15), 90, 1),
            signal="Hold",
            market_report="m",
            fundamentals_report="f",
            news_macro_report="n",
            bull_case="b+",
            bear_case="b-",
            research_manager="r",
            trader="t",
            risk_manager="risk",
            portfolio_manager="SIGNAL: HOLD",
            decision_id="decision-finality",
        )

        class FakeStore:
            def __init__(self):
                self.recorded = []

            def get_decision(self, decision_id):
                return decision if decision_id == "decision-finality" else None

            def record_outcome(self, outcome):
                self.recorded.append(outcome)
                return outcome

        class FakeKis:
            def daily_bars(self, instrument, as_of, lookback_days=120, *, price_mode="original"):
                self.as_of = as_of
                return MarketSnapshot(
                    instrument,
                    as_of,
                    (
                        OHLCVBar(date(2026, 9, 15), 100, 101, 99, 100, 1000),
                        OHLCVBar(date(2026, 9, 16), 101, 103, 100, 102, 1200),
                    ),
                    "KIS",
                    price_mode,
                )

        benchmark = (
            OHLCVBar(date(2026, 9, 15), 100, 101, 99, 100, 1000),
            OHLCVBar(date(2026, 9, 16), 100, 102, 99, 101, 1000),
        )
        store = FakeStore()
        fake_kis = FakeKis()
        benchmark_end = []

        def fake_benchmark(symbol, start, end):
            benchmark_end.append(end)
            return benchmark

        args = SimpleNamespace(decision_id="decision-finality", horizons=[1])
        with patch.object(cli, "_store", return_value=store), patch.object(
            cli.KisClient, "from_env", return_value=fake_kis
        ), patch.object(cli, "load_yahoo_benchmark", side_effect=fake_benchmark), patch.object(
            cli,
            "_korea_now",
            return_value=datetime(2026, 9, 16, 17, 5, tzinfo=timezone(timedelta(hours=9))),
        ):
            result = cli.cmd_evaluate(args)

        self.assertEqual(fake_kis.as_of, date(2026, 9, 16))
        self.assertEqual(benchmark_end, [date(2026, 9, 16)])
        self.assertEqual(result["pending_horizons"], [])
        self.assertEqual(len(result["outcomes"]), 1)
        self.assertEqual(result["outcomes"][0].end_date, date(2026, 9, 16))
        self.assertEqual(len(store.recorded), 1)

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
        self.assertIsNotNone(out.evaluated_at)
        self.assertEqual(len(out.stock_input_hash or ""), 64)
        self.assertEqual(len(out.benchmark_input_hash or ""), 64)

    def test_evaluation_does_not_stretch_stock_horizon_for_benchmark_gaps(self):
        stock = bars(1.0, days=6)
        benchmark = tuple(bar for bar in bars(0.5, days=6) if bar.trade_date != date(2026, 8, 23))
        with self.assertRaisesRegex(ValueError, "benchmark is missing"):
            calculate_forward_return("d1", stock, date(2026, 8, 20), 3, benchmark)

        # A missing interior benchmark session is acceptable when the stock
        # horizon endpoints exist; it must not shift the stock end date.
        benchmark_interior_gap = tuple(
            bar for bar in bars(0.5, days=6) if bar.trade_date != date(2026, 8, 22)
        )
        out = calculate_forward_return("d1", stock, date(2026, 8, 20), 3, benchmark_interior_gap)
        self.assertEqual(out.start_date, date(2026, 8, 21))
        self.assertEqual(out.end_date, date(2026, 8, 23))

    def test_outcome_first_write_wins(self):
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
            first = calculate_forward_return(
                decision.decision_id or "",
                bars(1.0, days=10),
                self.candidate.analysis_date,
                5,
                bars(0.5, days=10),
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="original",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
            )
            second = replace(first, raw_return=0.50, benchmark_return=0.20, alpha_return=0.30)
            stored_first = store.record_outcome(first)
            stored_second = store.record_outcome(second)
            self.assertEqual(stored_second.raw_return, stored_first.raw_return)
            history = store.list_outcomes(decision.decision_id or "")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].horizon, 5)
            self.assertEqual(history[0].raw_return, stored_first.raw_return)

    def test_outcome_rejects_replay_with_different_price_input_fingerprint(self):
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
            first = calculate_forward_return(
                decision.decision_id or "",
                bars(1.0, days=10),
                self.candidate.analysis_date,
                5,
                bars(0.5, days=10),
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="original",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
            )
            changed = calculate_forward_return(
                decision.decision_id or "",
                bars(2.0, days=10),
                self.candidate.analysis_date,
                5,
                bars(0.75, days=10),
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="original",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
            )
            frozen = store.record_outcome(first)
            self.assertEqual(frozen.stock_input_hash, first.stock_input_hash)
            with self.assertRaisesRegex(ValueError, "outcome provenance conflict"):
                store.record_outcome(changed)

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

    def test_outcome_rejects_same_day_or_missing_source_provenance(self):
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            base = dict(
                decision_id=decision.decision_id or "",
                horizon=1,
                start_date=self.candidate.analysis_date,
                end_date=self.candidate.analysis_date + timedelta(days=1),
                raw_return=0.01,
                benchmark_return=0.005,
                alpha_return=0.005,
                max_gain=0.02,
                max_drawdown=-0.01,
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="original",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
                evaluated_at=datetime.now(timezone.utc),
                stock_input_hash="a" * 64,
                benchmark_input_hash="b" * 64,
            )
            with self.assertRaisesRegex(ValueError, "start_date must be after"):
                store.record_outcome(Outcome(**base))

            base["start_date"] = self.candidate.analysis_date + timedelta(days=1)
            base["end_date"] = self.candidate.analysis_date + timedelta(days=1)
            base["stock_source"] = ""
            with self.assertRaisesRegex(ValueError, "stock_source provenance is required"):
                store.record_outcome(Outcome(**base))

    def test_outcome_requires_finalized_daily_endpoint(self):
        from personal_kr.evaluation import Outcome

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            candidate = QuantCandidate(
                Instrument("005930", "삼성전자", "KOSPI"),
                date(2026, 9, 15),
                90,
                1,
            )
            decision = store.record_decision(
                ResearchResult(
                    candidate=candidate,
                    signal="Hold",
                    market_report="m",
                    fundamentals_report="f",
                    news_macro_report="n",
                    bull_case="b+",
                    bear_case="b-",
                    research_manager="r",
                    trader="t",
                    risk_manager="risk",
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            base = dict(
                decision_id=decision.decision_id or "",
                horizon=1,
                start_date=date(2026, 9, 16),
                end_date=date(2026, 9, 16),
                raw_return=0.01,
                benchmark_return=0.005,
                alpha_return=0.005,
                max_gain=0.02,
                max_drawdown=-0.01,
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="adjusted",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
                stock_input_hash="a" * 64,
                benchmark_input_hash="b" * 64,
            )
            kst = timezone(timedelta(hours=9))
            with self.assertRaisesRegex(ValueError, "not finalized"):
                store.record_outcome(
                    Outcome(**base, evaluated_at=datetime(2026, 9, 16, 16, 45, tzinfo=kst))
                )
            with self.assertRaisesRegex(ValueError, "later than evaluated_at"):
                store.record_outcome(
                    Outcome(**base, evaluated_at=datetime(2026, 9, 15, 17, 0, tzinfo=kst))
                )

            recorded = store.record_outcome(
                Outcome(**base, evaluated_at=datetime(2026, 9, 16, 17, 5, tzinfo=kst))
            )
            self.assertEqual(recorded.end_date, date(2026, 9, 16))

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

    def test_decision_rejects_same_day_quant_rerun_with_different_ranking_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            generated = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)
            first_candidate = QuantCandidate(
                self.instrument,
                self.candidate.analysis_date,
                90,
                1,
                {},
                "quant-v1",
                generated,
                "hash-a",
            )
            second_candidate = QuantCandidate(
                self.instrument,
                self.candidate.analysis_date,
                95,
                1,
                {},
                "quant-v2",
                generated + timedelta(minutes=5),
                "hash-b",
            )

            def result(candidate):
                return ResearchResult(
                    candidate=candidate,
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

            frozen = store.record_decision(result(first_candidate), strategy_id="personal-kr-quant")
            replay = store.record_decision(result(first_candidate), strategy_id="personal-kr-quant")
            self.assertEqual(replay.decision_id, frozen.decision_id)
            with self.assertRaisesRegex(ValueError, "provenance conflict"):
                store.record_decision(result(second_candidate), strategy_id="personal-kr-quant")

    def test_decision_rejects_same_day_rerun_with_different_cutoff_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "kr.db")
            kst = timezone(timedelta(hours=9))
            first_candidate = QuantCandidate(
                self.instrument,
                self.candidate.analysis_date,
                0,
                1,
                {},
                analysis_cutoff_at=datetime(2026, 8, 20, 10, 0, tzinfo=kst),
                analysis_cutoff_mode="live_request",
            )
            later_candidate = replace(
                first_candidate,
                analysis_cutoff_at=datetime(2026, 8, 20, 11, 0, tzinfo=kst),
            )

            def result(candidate):
                return ResearchResult(
                    candidate=candidate,
                    signal="Hold",
                    market_report="m",
                    fundamentals_report="f",
                    news_macro_report="n",
                    bull_case="b+",
                    bear_case="b-",
                    research_manager="r",
                    trader="t",
                    risk_manager="risk",
                    portfolio_manager="SIGNAL: HOLD",
                    llm_provider="scripted",
                    llm_model_id="deterministic-test-double",
                )

            frozen = store.record_decision(result(first_candidate), strategy_id="personal-kr-ui")
            replay = store.record_decision(result(first_candidate), strategy_id="personal-kr-ui")
            self.assertEqual(replay.decision_id, frozen.decision_id)
            equivalent_utc = replace(
                first_candidate,
                analysis_cutoff_at=datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
            )
            equivalent_replay = store.record_decision(result(equivalent_utc), strategy_id="personal-kr-ui")
            self.assertEqual(equivalent_replay.decision_id, frozen.decision_id)
            engine_shaped = replace(
                result(first_candidate),
                evidence={"candidate": to_jsonable(first_candidate), "market": {"source": "KIS"}},
            )
            equivalent_engine_shaped = replace(
                result(equivalent_utc),
                evidence={"candidate": to_jsonable(equivalent_utc), "market": {"source": "KIS"}},
            )
            with tempfile.TemporaryDirectory() as shaped_tmp:
                shaped_store = DecisionStore(Path(shaped_tmp) / "kr.db")
                shaped_frozen = shaped_store.record_decision(engine_shaped, strategy_id="personal-kr-ui")
                shaped_replay = shaped_store.record_decision(
                    equivalent_engine_shaped, strategy_id="personal-kr-ui"
                )
            self.assertEqual(shaped_replay.decision_id, shaped_frozen.decision_id)
            with self.assertRaisesRegex(ValueError, "provenance conflict"):
                store.record_decision(result(later_candidate), strategy_id="personal-kr-ui")
            changed_model = replace(result(first_candidate), llm_model_id="different-model")
            with self.assertRaisesRegex(ValueError, "provenance conflict"):
                store.record_decision(changed_model, strategy_id="personal-kr-ui")
            changed_evidence = replace(result(first_candidate), evidence={"market": {"source": "restated"}})
            with self.assertRaisesRegex(ValueError, "provenance conflict"):
                store.record_decision(changed_evidence, strategy_id="personal-kr-ui")

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

    def test_legacy_paper_migration_quarantines_duplicate_and_malformed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-paper-invalid.db"
            seed = DecisionStore(path, initial_cash=10_000)
            decision = seed.record_decision(
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            decision_id = decision.decision_id or ""
            paper_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()

            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE kr_paper_trades")
            conn.execute(
                """
                CREATE TABLE kr_paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_trade_id TEXT,
                    decision_id TEXT,
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO kr_paper_trades
                    (client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [
                    ("legacy-valid", decision_id, paper_date, "005930", "BUY", 1, 100, 0, 0),
                    ("legacy-valid", decision_id, paper_date, "005930", "BUY", 1, 100, 0, 0),
                    ("legacy-bad-side", decision_id, paper_date, "005930", "HOLD", 1, 100, 0, 0),
                    ("legacy-bad-qty", decision_id, paper_date, "005930", "BUY", 1.5, 100, 0, 0),
                ],
            )
            conn.commit()
            conn.close()

            upgraded = DecisionStore(path, initial_cash=10_000)
            cash, positions = upgraded.paper_summary()
            self.assertEqual(cash, 9_900)
            self.assertEqual(positions, {"005930": 1})

            check = sqlite3.connect(path)
            active = check.execute("SELECT COUNT(*) FROM kr_paper_trades").fetchone()[0]
            reasons = [row[0] for row in check.execute("SELECT reason FROM kr_paper_trade_quarantine ORDER BY quarantine_id")]
            stale = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kr_paper_trades_strict'"
            ).fetchone()[0]
            info = {row[1]: row for row in check.execute("PRAGMA table_info(kr_paper_trades)")}
            check.close()
            self.assertEqual(active, 1)
            self.assertEqual(len(reasons), 3)
            self.assertTrue(any("duplicate client_trade_id" in reason for reason in reasons))
            self.assertTrue(any("invalid paper trade" in reason for reason in reasons))
            self.assertEqual(stale, 0)
            self.assertEqual(info["client_trade_id"][3], 1)
            self.assertEqual(info["decision_id"][3], 1)

            reopened = DecisionStore(path, initial_cash=10_000)
            self.assertEqual(reopened.paper_summary(), (9_900, {"005930": 1}))

    def test_legacy_paper_migration_recovers_stranded_strict_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-paper-stranded.db"
            seed = DecisionStore(path, initial_cash=10_000)
            decision = seed.record_decision(
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            decision_id = decision.decision_id or ""
            paper_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()

            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE kr_paper_trades")
            conn.execute(
                """
                CREATE TABLE kr_paper_trades_strict(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_trade_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price REAL NOT NULL CHECK(price > 0),
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO kr_paper_trades_strict
                    (client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                ("stranded-1", decision_id, paper_date, "005930", "BUY", 1, 100, 0, 0),
            )
            conn.commit()
            conn.close()

            recovered = DecisionStore(path, initial_cash=10_000)
            self.assertEqual(recovered.paper_summary(), (9_900, {"005930": 1}))
            check = sqlite3.connect(path)
            stale = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kr_paper_trades_strict'"
            ).fetchone()[0]
            active = check.execute("SELECT COUNT(*) FROM kr_paper_trades").fetchone()[0]
            check.close()
            self.assertEqual((stale, active), (0, 1))
            self.assertEqual(DecisionStore(path, initial_cash=10_000).paper_summary(), (9_900, {"005930": 1}))

    def test_strict_paper_ledger_reopen_does_not_revalidate_history_against_new_initial_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strict-reopen.db"
            store = DecisionStore(path, initial_cash=1_000)
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            paper_date = datetime.now(timezone(timedelta(hours=9))).date()
            store.add_paper_trade(
                trade_date=paper_date,
                ticker="005930",
                side="BUY",
                quantity=1,
                price=900,
                decision_id=decision.decision_id,
                client_trade_id="strict-history-1",
            )

            reopened = DecisionStore(path, initial_cash=500)
            self.assertEqual(reopened.paper_summary(), (-400, {"005930": 1}))
            check = sqlite3.connect(path)
            active = check.execute("SELECT COUNT(*) FROM kr_paper_trades").fetchone()[0]
            quarantine = check.execute("SELECT COUNT(*) FROM kr_paper_trade_quarantine").fetchone()[0]
            check.close()
            self.assertEqual((active, quarantine), (1, 0))

    def test_concurrent_legacy_paper_initialization_is_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-concurrent.db"
            seed = DecisionStore(path, initial_cash=10_000)
            decision = seed.record_decision(
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            paper_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE kr_paper_trades")
            conn.execute(
                """
                CREATE TABLE kr_paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_trade_id TEXT,
                    decision_id TEXT,
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO kr_paper_trades
                    (client_trade_id,decision_id,trade_date,ticker,side,quantity,price)
                VALUES(?,?,?,?,?,?,?)
                """,
                ("legacy-concurrent-1", decision.decision_id, paper_date, "005930", "BUY", 1, 100),
            )
            conn.commit()
            conn.close()

            def open_store(_: int):
                opened = DecisionStore(path, initial_cash=10_000)
                return opened.paper_summary()

            with ThreadPoolExecutor(max_workers=8) as pool:
                summaries = list(pool.map(open_store, range(8)))

            self.assertEqual(summaries, [(9_900, {"005930": 1})] * 8)
            check = sqlite3.connect(path)
            stale = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kr_paper_trades_strict'"
            ).fetchone()[0]
            check.close()
            self.assertEqual(stale, 0)

    def test_legacy_outcomes_without_provenance_are_quarantined_and_can_be_reevaluated(self):
        from personal_kr.evaluation import Outcome

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-outcome.db"
            seed = DecisionStore(path)
            decision = seed.record_decision(
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
                    portfolio_manager="SIGNAL: HOLD",
                )
            )
            decision_id = decision.decision_id or ""
            legacy_payload = {
                "decision_id": decision_id,
                "horizon": 5,
                "start_date": (self.candidate.analysis_date + timedelta(days=1)).isoformat(),
                "end_date": (self.candidate.analysis_date + timedelta(days=5)).isoformat(),
                "raw_return": 0.05,
                "benchmark_return": 0.02,
                "alpha_return": 0.03,
                "max_gain": 0.08,
                "max_drawdown": -0.02,
            }
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE kr_outcome_quarantine")
            conn.execute(
                "INSERT INTO kr_outcomes(decision_id,horizon,payload,created_at) VALUES(?,?,?,?)",
                (
                    decision_id,
                    5,
                    json.dumps(legacy_payload, separators=(",", ":")),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            conn.close()

            upgraded = DecisionStore(path)
            self.assertEqual(upgraded.list_outcomes(decision_id), [])
            check = sqlite3.connect(path)
            quarantined = check.execute(
                "SELECT payload,reason FROM kr_outcome_quarantine WHERE decision_id=? AND horizon=5",
                (decision_id,),
            ).fetchone()
            active = check.execute(
                "SELECT COUNT(*) FROM kr_outcomes WHERE decision_id=? AND horizon=5", (decision_id,)
            ).fetchone()[0]
            check.close()
            self.assertIsNotNone(quarantined)
            self.assertIn("provenance", quarantined[1])
            self.assertEqual(json.loads(quarantined[0])["raw_return"], 0.05)
            self.assertEqual(active, 0)

            audited = Outcome(
                decision_id=decision_id,
                horizon=5,
                start_date=self.candidate.analysis_date + timedelta(days=1),
                end_date=self.candidate.analysis_date + timedelta(days=5),
                raw_return=0.04,
                benchmark_return=0.01,
                alpha_return=0.03,
                max_gain=0.07,
                max_drawdown=-0.015,
                stock_ticker="005930",
                stock_source="KIS",
                stock_price_mode="adjusted",
                benchmark_symbol="^KS11",
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
                evaluated_at=datetime.now(timezone.utc),
                stock_input_hash="a" * 64,
                benchmark_input_hash="b" * 64,
                evaluation_version="personal-kr-outcome-v1",
            )
            recorded = upgraded.record_outcome(audited)
            self.assertEqual(recorded.stock_input_hash, "a" * 64)
            self.assertEqual(len(upgraded.list_outcomes(decision_id)), 1)
            reopened = DecisionStore(path)
            self.assertEqual(len(reopened.list_outcomes(decision_id)), 1)
            self.assertEqual(reopened.list_outcomes(decision_id)[0].stock_input_hash, "a" * 64)

    def test_json_contract_is_valid(self):
        payload = {"success": True, "data": {"ticker": self.instrument.ticker}, "error": None}
        self.assertEqual(json.loads(json.dumps(payload))["data"]["ticker"], "005930")


if __name__ == "__main__":
    unittest.main()
