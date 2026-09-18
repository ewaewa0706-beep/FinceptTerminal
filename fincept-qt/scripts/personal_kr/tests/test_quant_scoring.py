from __future__ import annotations

import unittest
from datetime import date, timedelta

from personal_kr.models import FundamentalSnapshot, Instrument, InvestorFlowSnapshot, MarketSnapshot, OHLCVBar
from personal_kr.quant_scoring import (
    QuantFeatureRecord,
    score_quant_records,
    weights_for_quant_profile,
)


ANALYSIS_DATE = date(2026, 9, 18)


def market_snapshot(instrument: Instrument, *, daily_gain: float, volume: int) -> MarketSnapshot:
    bars = []
    price = 10_000.0
    start = ANALYSIS_DATE - timedelta(days=89)
    for offset in range(90):
        day = start + timedelta(days=offset)
        price *= 1.0 + daily_gain
        bars.append(
            OHLCVBar(
                trade_date=day,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=volume,
            )
        )
    return MarketSnapshot(instrument, ANALYSIS_DATE, tuple(bars), "KIS", "original")


class QuantScoringTests(unittest.TestCase):
    def test_balanced_quant_rank_combines_momentum_flow_liquidity_and_risk(self):
        alpha = Instrument("005930", "삼성전자", "KOSPI")
        beta = Instrument("000660", "SK하이닉스", "KOSPI")
        records = [
            QuantFeatureRecord(
                alpha,
                ANALYSIS_DATE,
                market_snapshot(alpha, daily_gain=0.004, volume=2_000_000),
                InvestorFlowSnapshot(ANALYSIS_DATE, "KIS", 200_000, 100_000),
            ),
            QuantFeatureRecord(
                beta,
                ANALYSIS_DATE,
                market_snapshot(beta, daily_gain=0.001, volume=500_000),
                InvestorFlowSnapshot(ANALYSIS_DATE, "KIS", -50_000, -20_000),
            ),
        ]

        ranked, rows = score_quant_records(records, ANALYSIS_DATE, limit=2)

        self.assertEqual([item.instrument.ticker for item in ranked], ["005930", "000660"])
        self.assertEqual([item.rank for item in ranked], [1, 2])
        self.assertTrue(all(0 <= item.score <= 100 for item in ranked))
        row = next(item for item in rows if item["ticker"] == "005930")
        self.assertIn("momentum_score", row)
        self.assertIn("flow_score", row)
        self.assertIn("liquidity_score", row)
        self.assertIn("risk_score", row)
        self.assertEqual(row["market_data_as_of"], ANALYSIS_DATE.isoformat())
        self.assertEqual(row["flow_data_as_of"], ANALYSIS_DATE.isoformat())
        self.assertAlmostEqual(row["foreign_flow_ratio"], 0.1)
        self.assertAlmostEqual(row["institution_flow_ratio"], 0.05)

    def test_dart_fundamentals_add_margin_and_equity_factor(self):
        alpha = Instrument("005930", "Samsung", "KOSPI")
        beta = Instrument("000660", "SK Hynix", "KOSPI")
        records = [
            QuantFeatureRecord(
                alpha,
                ANALYSIS_DATE,
                market_snapshot(alpha, daily_gain=0.002, volume=1_000_000),
                None,
                FundamentalSnapshot(
                    as_of=ANALYSIS_DATE - timedelta(days=30),
                    source="DART",
                    revenue=1_000, operating_profit=250, net_income=180,
                    assets=2_000, liabilities=700, equity=1_300,
                ),
            ),
            QuantFeatureRecord(
                beta,
                ANALYSIS_DATE,
                market_snapshot(beta, daily_gain=0.002, volume=1_000_000),
                None,
                FundamentalSnapshot(
                    as_of=ANALYSIS_DATE - timedelta(days=30),
                    source="DART",
                    revenue=1_000, operating_profit=50, net_income=20,
                    assets=2_000, liabilities=1_400, equity=600,
                ),
            ),
        ]

        ranked, rows = score_quant_records(records, ANALYSIS_DATE, limit=2)
        self.assertEqual(ranked[0].instrument.ticker, "005930")
        self.assertIn("fundamental_score", ranked[0].factors)
        row = next(item for item in rows if item["ticker"] == "005930")
        self.assertEqual(row["fundamental_data_as_of"], (ANALYSIS_DATE - timedelta(days=30)).isoformat())
        self.assertAlmostEqual(row["operating_margin"], 0.25)
        self.assertAlmostEqual(row["net_margin"], 0.18)
        self.assertAlmostEqual(row["equity_ratio"], 0.65)

    def test_missing_flow_is_renormalized_not_zero_filled(self):
        instrument = Instrument("035420", "NAVER", "KOSPI")
        record = QuantFeatureRecord(
            instrument,
            ANALYSIS_DATE,
            market_snapshot(instrument, daily_gain=0.002, volume=1_000_000),
            None,
        )

        ranked, rows = score_quant_records([record], ANALYSIS_DATE, limit=1)

        self.assertEqual(len(ranked), 1)
        self.assertNotIn("flow_score", ranked[0].factors)
        self.assertNotIn("flow_score", rows[0])
        self.assertGreaterEqual(ranked[0].score, 0)

    def test_profile_weights_are_normalized_and_materially_distinct(self):
        balanced = weights_for_quant_profile("balanced").normalized()
        momentum = weights_for_quant_profile("momentum").normalized()
        defensive = weights_for_quant_profile("defensive").normalized()

        self.assertAlmostEqual(sum(balanced.values()), 1.0)
        self.assertGreater(momentum["momentum_score"], balanced["momentum_score"])
        self.assertGreater(defensive["risk_score"], balanced["risk_score"])

    def test_feature_record_rejects_market_data_newer_than_analysis_date(self):
        instrument = Instrument("005930", "삼성전자", "KOSPI")
        snapshot = market_snapshot(instrument, daily_gain=0.001, volume=1_000)
        with self.assertRaisesRegex(ValueError, "newer than quant analysis_date"):
            QuantFeatureRecord(instrument, ANALYSIS_DATE - timedelta(days=1), snapshot, None)


if __name__ == "__main__":
    unittest.main()
