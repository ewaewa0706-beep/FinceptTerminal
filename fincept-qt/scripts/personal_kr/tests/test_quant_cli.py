from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import personal_kr.cli as cli
from personal_kr.models import (
    FundamentalSnapshot,
    Instrument,
    InvestorFlowSnapshot,
    MarketSnapshot,
    OHLCVBar,
    QuantCandidate,
    ResearchResult,
)


KST = timezone(timedelta(hours=9))
TODAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 14, 5, tzinfo=KST)


def make_market(instrument: Instrument, gain: float) -> MarketSnapshot:
    bars = []
    price = 10_000.0
    start = TODAY - timedelta(days=89)
    for offset in range(90):
        day = start + timedelta(days=offset)
        price *= 1 + gain
        bars.append(OHLCVBar(day, price, price * 1.01, price * 0.99, price, 1_000_000))
    return MarketSnapshot(instrument, TODAY, tuple(bars), "KIS", "original")


class FakeKis:
    def daily_bars(self, instrument, as_of, lookback_days=120, *, price_mode="original"):
        self.last_lookback = lookback_days
        self.last_price_mode = price_mode
        gain = 0.004 if instrument.ticker == "005930" else 0.001
        return make_market(instrument, gain)

    def investor_flow(self, instrument, as_of):
        if instrument.ticker == "000660":
            raise RuntimeError("flow unavailable")
        return InvestorFlowSnapshot(as_of, "KIS", 200_000, 100_000)


class QuantCliTests(unittest.TestCase):
    def args(self, **overrides):
        values = {
            "analysis_date": TODAY.isoformat(),
            "market": None,
            "limit": 2,
            "prefilter_limit": 2,
            "lookback_days": 120,
            "min_trading_value_krw": 0,
            "profile": "balanced",
            "discovery_profile": "balanced",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def discovery(self):
        return {
            "candidates": [
                QuantCandidate(Instrument("005930", "삼성전자", "KOSPI"), TODAY, 90, 1),
                QuantCandidate(Instrument("000660", "SK하이닉스", "KOSPI"), TODAY, 80, 2),
            ],
            "ranking_source": "fincept-kis-public-master-cross-sectional-v2/balanced",
            "ranking_payload_hash": "a" * 64,
        }

    def test_quant_rank_builds_batch_ready_observed_ranking(self):
        fake_kis = FakeKis()
        with (
            patch.dict("os.environ", {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "DART_API_KEY": ""}, clear=False),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "_korea_now", return_value=NOW),
            patch.object(cli, "cmd_discover", return_value=self.discovery()),
            patch.object(cli.KisClient, "from_env", return_value=fake_kis),
        ):
            result = cli.cmd_quant_rank(self.args())

        self.assertEqual(result["scoring_model"], "kis-feature-quant-v1")
        self.assertEqual(result["ranking_mode"], "observed")
        self.assertEqual(result["ranking_generated_at"], NOW)
        self.assertEqual(len(result["ranking_payload_hash"]), 64)
        self.assertEqual(result["upstream_discovery_hash"], "a" * 64)
        self.assertEqual(result["feature_record_count"], 2)
        self.assertIn("000660", result["flow_errors"])
        self.assertNotIn("secret", result["flow_errors"]["000660"])
        self.assertEqual(fake_kis.last_lookback, 120)
        self.assertEqual(fake_kis.last_price_mode, "original")
        self.assertTrue(all(item.ranking_payload_hash == result["ranking_payload_hash"] for item in result["candidates"]))

        source, generated, payload_hash, mode, data_as_of = cli._ranking_provenance(
            result["ranking"], TODAY
        )
        self.assertEqual(source, result["ranking_source"])
        self.assertEqual(generated, NOW)
        self.assertEqual(payload_hash, result["ranking_payload_hash"])
        self.assertEqual(mode, "observed")
        self.assertLessEqual(data_as_of, TODAY)

    def test_quant_rank_optionally_adds_dart_fundamental_factor(self):
        class FakeDart:
            def fundamentals(self, instrument, as_of):
                return FundamentalSnapshot(
                    as_of=as_of - timedelta(days=30),
                    source="DART",
                    revenue=1_000,
                    operating_profit=200 if instrument.ticker == "005930" else 50,
                    net_income=150 if instrument.ticker == "005930" else 20,
                    assets=2_000,
                    liabilities=800,
                    equity=1_200 if instrument.ticker == "005930" else 600,
                )

        with (
            patch.dict(
                "os.environ",
                {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "DART_API_KEY": "dart"},
                clear=False,
            ),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "_korea_now", return_value=NOW),
            patch.object(cli, "cmd_discover", return_value=self.discovery()),
            patch.object(cli.KisClient, "from_env", return_value=FakeKis()),
            patch.object(cli.DartClient, "from_env", return_value=FakeDart()),
        ):
            result = cli.cmd_quant_rank(self.args())

        self.assertTrue(result["dart_enrichment"])
        self.assertEqual(result["fundamental_errors"], {})
        self.assertIn("fundamental_score", result["candidates"][0].factors)
        self.assertEqual(result["ranking_data_as_of"], TODAY)
        self.assertIn("fundamental_data_as_of", result["ranking"]["rows"][0])

    def test_quant_rank_bounds_dart_fanout_to_fifteen_preliminary_candidates(self):
        class CountingDart:
            def __init__(self):
                self.calls = []

            def fundamentals(self, instrument, as_of):
                self.calls.append(instrument.ticker)
                return FundamentalSnapshot(
                    as_of=as_of - timedelta(days=30),
                    source="DART",
                    revenue=1_000,
                    operating_profit=100,
                    net_income=80,
                    assets=2_000,
                    liabilities=800,
                    equity=1_200,
                )

        many_candidates = [
            QuantCandidate(
                Instrument(f"{index:06d}", f"Stock {index}", "KOSPI"),
                TODAY,
                100 - index,
                index,
            )
            for index in range(1, 31)
        ]
        discovery = {
            "candidates": many_candidates,
            "ranking_source": "fincept-kis-public-master-cross-sectional-v2/balanced",
            "ranking_payload_hash": "b" * 64,
        }
        dart = CountingDart()
        with (
            patch.dict(
                "os.environ",
                {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "DART_API_KEY": "dart"},
                clear=False,
            ),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "_korea_now", return_value=NOW),
            patch.object(cli, "cmd_discover", return_value=discovery),
            patch.object(cli.KisClient, "from_env", return_value=FakeKis()),
            patch.object(cli.DartClient, "from_env", return_value=dart),
        ):
            result = cli.cmd_quant_rank(self.args(limit=5, prefilter_limit=30))

        self.assertEqual(result["preliminary_candidate_count"], 15)
        self.assertEqual(result["dart_candidate_limit"], 15)
        self.assertEqual(result["dart_enrichment_count"], 15)
        self.assertEqual(len(dart.calls), 15)
        self.assertEqual(len(result["candidates"]), 5)

    def test_quant_rank_rejects_historical_request_before_provider_calls(self):
        with (
            patch.dict("os.environ", {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "DART_API_KEY": ""}, clear=False),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "cmd_discover") as discover,
            patch.object(cli.KisClient, "from_env") as from_env,
        ):
            with self.assertRaisesRegex(ValueError, "current-date only"):
                cli.cmd_quant_rank(self.args(analysis_date="2026-09-17"))
        discover.assert_not_called()
        from_env.assert_not_called()

    def test_quant_ranking_envelope_roundtrips_into_batch_candidate_provenance(self):
        fake_kis = FakeKis()
        with (
            patch.dict(
                "os.environ",
                {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret", "DART_API_KEY": ""},
                clear=False,
            ),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "_korea_now", return_value=NOW),
            patch.object(cli, "cmd_discover", return_value=self.discovery()),
            patch.object(cli.KisClient, "from_env", return_value=fake_kis),
        ):
            quant = cli.cmd_quant_rank(self.args(limit=1, prefilter_limit=2))

        class FakeEngine:
            def __init__(self):
                self.seen = []

            def analyze(self, candidate):
                self.seen.append(candidate)
                return ResearchResult(
                    candidate=candidate,
                    signal="Hold",
                    market_report="market",
                    fundamentals_report="fundamentals",
                    news_macro_report="news",
                    bull_case="bull",
                    bear_case="bear",
                    research_manager="research",
                    trader="trader",
                    risk_manager="risk",
                    portfolio_manager="portfolio",
                )

        class FakeStore:
            def __init__(self):
                self.saved = []

            def record_decision(self, result, *, strategy_id):
                self.saved.append((result, strategy_id))
                return result

        engine = FakeEngine()
        store = FakeStore()
        batch_payload = dict(quant["ranking"])
        batch_payload["strategy_id"] = "personal-kr-quant-test"
        with (
            patch.object(cli, "_input_json", return_value=batch_payload),
            patch.object(cli, "_korea_today", return_value=TODAY),
            patch.object(cli, "_korea_now", return_value=NOW),
            patch.object(cli, "_engine", return_value=engine),
            patch.object(cli, "_store", return_value=store),
        ):
            result = cli.cmd_batch()

        self.assertEqual(len(engine.seen), 1)
        candidate = engine.seen[0]
        self.assertEqual(candidate.ranking_source, quant["ranking_source"])
        self.assertEqual(candidate.ranking_generated_at, NOW)
        self.assertEqual(candidate.ranking_payload_hash, quant["ranking_payload_hash"])
        self.assertEqual(candidate.ranking_mode, "observed")
        self.assertEqual(candidate.ranking_data_as_of, TODAY)
        self.assertEqual(candidate.analysis_cutoff_at, NOW)
        self.assertEqual(candidate.analysis_cutoff_mode, "external")
        self.assertEqual(store.saved[0][1], "personal-kr-quant-test")
        self.assertEqual(result["selected"][0].ranking_payload_hash, quant["ranking_payload_hash"])


if __name__ == "__main__":
    unittest.main()
