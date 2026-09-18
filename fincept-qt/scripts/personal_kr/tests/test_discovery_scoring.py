from __future__ import annotations

import unittest
from datetime import date

from personal_kr.discovery_scoring import DiscoveryWeights, score_universe_entries, weights_for_profile
from personal_kr.models import Instrument
from personal_kr.universe import UniverseEntry


class DiscoveryScoringTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 9, 18)

    def entry(self, ticker, *, value, cap, volume, market="KOSPI"):
        return UniverseEntry(
            Instrument(ticker, ticker, market),
            self.as_of,
            trading_value_krw=value,
            market_cap_krw=cap,
            volume=volume,
        )

    def test_default_v2_score_is_deterministic_and_prefers_liquid_large_names(self):
        entries = [
            self.entry("005930", value=900, cap=1000, volume=500),
            self.entry("000660", value=700, cap=800, volume=400),
            self.entry("035420", value=300, cap=600, volume=250),
        ]

        first = score_universe_entries(entries, self.as_of, limit=3)
        second = score_universe_entries(list(reversed(entries)), self.as_of, limit=3)

        self.assertEqual([item.instrument.ticker for item in first], ["005930", "000660", "035420"])
        self.assertEqual(
            [(item.instrument.ticker, item.score) for item in first],
            [(item.instrument.ticker, item.score) for item in second],
        )
        self.assertEqual([item.rank for item in first], [1, 2, 3])
        self.assertGreaterEqual(first[0].factors["liquidity_score"], first[1].factors["liquidity_score"])
        self.assertIn("turnover_score", first[0].factors)

    def test_missing_factor_weights_are_renormalized_not_zero_filled(self):
        entries = [
            self.entry("005930", value=100, cap=None, volume=None),
            self.entry("000660", value=50, cap=None, volume=None),
        ]

        ranked = score_universe_entries(entries, self.as_of, limit=2)

        self.assertEqual(ranked[0].instrument.ticker, "005930")
        self.assertEqual(ranked[0].score, 100.0)
        self.assertEqual(ranked[1].score, 0.0)
        self.assertNotIn("size_score", ranked[0].factors)
        self.assertNotIn("turnover_score", ranked[0].factors)

    def test_ties_are_deterministic_by_ticker(self):
        entries = [
            self.entry("005930", value=100, cap=100, volume=100),
            self.entry("000660", value=100, cap=100, volume=100),
        ]
        ranked = score_universe_entries(entries, self.as_of, limit=2)
        self.assertEqual([item.instrument.ticker for item in ranked], ["000660", "005930"])
        self.assertEqual(ranked[0].score, ranked[1].score)

    def test_custom_weights_change_ranking_without_breaking_bounds(self):
        entries = [
            self.entry("005930", value=1000, cap=1000, volume=100),
            self.entry("000660", value=500, cap=100, volume=1000),
        ]
        liquidity_first = score_universe_entries(
            entries,
            self.as_of,
            limit=2,
            weights=DiscoveryWeights(liquidity=1, size=0, turnover=0, volume=0),
        )
        volume_first = score_universe_entries(
            entries,
            self.as_of,
            limit=2,
            weights=DiscoveryWeights(liquidity=0, size=0, turnover=0, volume=1),
        )
        self.assertEqual(liquidity_first[0].instrument.ticker, "005930")
        self.assertEqual(volume_first[0].instrument.ticker, "000660")
        self.assertTrue(all(0 <= item.score <= 100 for item in liquidity_first + volume_first))

    def test_mismatched_entry_date_is_rejected(self):
        bad = UniverseEntry(
            Instrument("005930", "삼성전자", "KOSPI"),
            date(2026, 9, 17),
            trading_value_krw=100,
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            score_universe_entries([bad], self.as_of)

    def test_named_profiles_are_normalized_and_materially_different(self):
        balanced = weights_for_profile("balanced").normalized()
        liquidity = weights_for_profile("liquidity").normalized()
        large = weights_for_profile("large_cap").normalized()
        active = weights_for_profile("active").normalized()

        self.assertAlmostEqual(sum(balanced.values()), 1.0)
        self.assertGreater(liquidity["liquidity_score"], balanced["liquidity_score"])
        self.assertGreater(large["size_score"], balanced["size_score"])
        self.assertGreater(active["turnover_score"], balanced["turnover_score"])
        with self.assertRaisesRegex(ValueError, "unknown discovery profile"):
            weights_for_profile("anything")


if __name__ == "__main__":
    unittest.main()
