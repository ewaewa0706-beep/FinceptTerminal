from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from personal_kr.models import Instrument
from personal_kr.persistence import DecisionStore
from personal_kr.universe import (
    KST,
    CurrentUniverseOnlyError,
    KisPublicMasterUniverseProvider,
    SnapshotAwareUniverseProvider,
    UniverseEntry,
    liquidity_candidates,
)


class FakeMasterClient:
    def __init__(self, rows_by_market):
        self.rows_by_market = rows_by_market
        self.calls = []

    def get_master_rows(self, market):
        self.calls.append(market)
        return list(self.rows_by_market.get(market, ()))


def row(
    ticker: str,
    name: str,
    *,
    group_code: str = "ST",
    preferred_code: str = "0",
    spac: str = "N",
    reference_price: int = 10_000,
    previous_volume: int = 1_000,
    market_cap_100m: int = 100,
):
    return {
        "ticker": ticker,
        "name": name,
        "group_code": group_code,
        "preferred_code": preferred_code,
        "spac": spac,
        "reference_price": str(reference_price),
        "previous_volume": str(previous_volume),
        "listed_on": "20200102",
        "listed_shares_thousands": "10000",
        "market_cap_100m": str(market_cap_100m),
    }


class UniverseTests(unittest.TestCase):
    def test_current_public_master_filters_and_sorts_by_liquidity_proxy(self):
        today = date(2026, 9, 18)
        client = FakeMasterClient(
            {
                "KOSPI": [
                    row("005930", "삼성전자", reference_price=70_000, previous_volume=2_000),
                    row("005931", "삼성전자우", preferred_code="1", reference_price=50_000, previous_volume=9_000),
                    row("123456", "테스트스팩1호", spac="Y", reference_price=2_000, previous_volume=999_999),
                ],
                "KOSDAQ": [
                    row("247540", "에코프로비엠", reference_price=100_000, previous_volume=500),
                ],
            }
        )
        provider = KisPublicMasterUniverseProvider(client, today_fn=lambda: today)

        entries = provider.get_universe(today)

        self.assertEqual([item.instrument.ticker for item in entries], ["005930", "247540"])
        self.assertEqual(entries[0].trading_value_krw, 140_000_000)
        self.assertEqual(entries[1].trading_value_krw, 50_000_000)
        self.assertEqual(client.calls, ["KOSPI", "KOSDAQ"])

    def test_current_provider_refuses_historical_date(self):
        today = date(2026, 9, 18)
        provider = KisPublicMasterUniverseProvider(FakeMasterClient({}), today_fn=lambda: today)
        with self.assertRaisesRegex(CurrentUniverseOnlyError, "current-date only"):
            provider.get_universe(today - timedelta(days=1))

    def test_snapshot_is_first_write_wins_and_exact_date_replays(self):
        snapshot_date = date(2026, 9, 18)
        captured = datetime(2026, 9, 18, 13, 0, tzinfo=KST)
        first = [
            UniverseEntry(
                Instrument("005930", "삼성전자", "KOSPI"),
                snapshot_date,
                volume=1_000,
                trading_value_krw=70_000_000,
                market_cap_krw=400_000_000_000_000,
            ),
            UniverseEntry(
                Instrument("247540", "에코프로비엠", "KOSDAQ"),
                snapshot_date,
                volume=500,
                trading_value_krw=50_000_000,
                market_cap_krw=20_000_000_000_000,
            ),
        ]
        changed = [
            UniverseEntry(
                Instrument("005930", "삼성전자", "KOSPI"),
                snapshot_date,
                volume=9_999,
                trading_value_krw=999_999_999,
                market_cap_krw=400_000_000_000_000,
            ),
            UniverseEntry(
                Instrument("247540", "에코프로비엠", "KOSDAQ"),
                snapshot_date,
                volume=8_888,
                trading_value_krw=888_888_888,
                market_cap_krw=20_000_000_000_000,
            ),
        ]

        class MutableCurrent:
            def __init__(self):
                self.calls = 0

            def get_universe(self, *_args, **_kwargs):
                self.calls += 1
                return first if self.calls == 1 else changed

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "research.db")
            current = MutableCurrent()
            provider = SnapshotAwareUniverseProvider(
                current,
                store,
                today_fn=lambda: snapshot_date,
                now_fn=lambda: captured,
            )

            first_result = provider.get_universe(snapshot_date)
            second_result = provider.get_universe(snapshot_date)
            self.assertEqual(first_result[0].trading_value_krw, 70_000_000)
            self.assertEqual(second_result[0].trading_value_krw, 70_000_000)
            self.assertEqual(current.calls, 1)

            replay = SnapshotAwareUniverseProvider(
                object(),
                store,
                today_fn=lambda: snapshot_date + timedelta(days=1),
                now_fn=lambda: captured + timedelta(days=1),
            ).get_universe(snapshot_date)
            self.assertEqual(replay[0].trading_value_krw, 70_000_000)
            self.assertIn("PIT universe snapshot replay", replay[0].source)

            with self.assertRaisesRegex(CurrentUniverseOnlyError, "exact PIT universe snapshot"):
                SnapshotAwareUniverseProvider(
                    object(),
                    store,
                    today_fn=lambda: snapshot_date + timedelta(days=2),
                ).get_universe(snapshot_date + timedelta(days=1))

    def test_market_scope_order_cannot_bypass_first_write_snapshot(self):
        snapshot_date = date(2026, 9, 18)
        captured = datetime(2026, 9, 18, 10, 0, tzinfo=KST)
        entries = [
            UniverseEntry(Instrument("005930", "삼성전자", "KOSPI"), snapshot_date, trading_value_krw=300),
            UniverseEntry(Instrument("247540", "에코프로비엠", "KOSDAQ"), snapshot_date, trading_value_krw=200),
        ]

        class Current:
            def __init__(self):
                self.calls = 0

            def get_universe(self, *_args, **_kwargs):
                self.calls += 1
                return entries

        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "research.db")
            current = Current()
            provider = SnapshotAwareUniverseProvider(
                current, store, today_fn=lambda: snapshot_date, now_fn=lambda: captured
            )
            provider.get_universe(snapshot_date, markets=("KOSDAQ", "KOSPI"))
            provider.get_universe(snapshot_date, markets=("KOSPI", "KOSDAQ"))
            self.assertEqual(current.calls, 1)
            snap = store.get_universe_snapshot(snapshot_date, markets=["KOSDAQ", "KOSPI"])
            self.assertEqual(snap.markets, ("KOSPI", "KOSDAQ"))

    def test_snapshot_hash_detects_tampering(self):
        snapshot_date = date(2026, 9, 18)
        captured = datetime(2026, 9, 18, 12, 0, tzinfo=KST)
        entry = UniverseEntry(
            Instrument("005930", "삼성전자", "KOSPI"),
            snapshot_date,
            trading_value_krw=70_000_000,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research.db"
            store = DecisionStore(path)
            store.record_universe_snapshot(
                snapshot_date=snapshot_date,
                markets=["KOSPI"],
                entries=[entry],
                captured_at=captured,
            )
            conn = sqlite3.connect(path)
            try:
                conn.execute("UPDATE kr_universe_snapshots SET payload = payload || ' '")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                store.get_universe_snapshot(snapshot_date, markets=["KOSPI"])

    def test_liquidity_candidates_are_deterministic_and_bounded(self):
        as_of = date(2026, 9, 18)
        entries = [
            UniverseEntry(Instrument("005930", "삼성전자", "KOSPI"), as_of, volume=100, trading_value_krw=300),
            UniverseEntry(Instrument("000660", "SK하이닉스", "KOSPI"), as_of, volume=90, trading_value_krw=200),
            UniverseEntry(Instrument("247540", "에코프로비엠", "KOSDAQ"), as_of, volume=80, trading_value_krw=100),
        ]
        candidates = liquidity_candidates(entries, as_of, limit=2)
        self.assertEqual([candidate.instrument.ticker for candidate in candidates], ["005930", "000660"])
        self.assertEqual([candidate.rank for candidate in candidates], [1, 2])
        self.assertGreater(candidates[0].score, candidates[1].score)
        self.assertEqual(candidates[0].factors["trading_value_krw"], 300.0)

    def test_current_kis_volume_rank_overlays_public_master_without_creating_membership(self):
        today = date(2026, 9, 18)
        master = FakeMasterClient(
            {
                "KOSPI": [
                    row("005930", "삼성전자", reference_price=70_000, previous_volume=100),
                    row("000660", "SK하이닉스", reference_price=200_000, previous_volume=1_000_000),
                ]
            }
        )

        class RankClient:
            def volume_rank(self, market):
                self.market = market
                return [
                    {"mksc_shrn_iscd": "005930", "acml_tr_pbmn": "900000000000"},
                    {"mksc_shrn_iscd": "999999", "acml_tr_pbmn": "999999999999"},
                ]

        rank = RankClient()
        provider = KisPublicMasterUniverseProvider(
            master, rank_client=rank, today_fn=lambda: today
        )
        entries = provider.get_universe(today, markets=("KOSPI",))

        self.assertEqual(rank.market, "KOSPI")
        self.assertEqual([item.instrument.ticker for item in entries], ["005930", "000660"])
        self.assertEqual(entries[0].trading_value_krw, 900_000_000_000)
        self.assertIn("volume-rank", entries[0].source)
        self.assertIn("proxy", entries[1].source)

    def test_current_kis_rank_failure_or_zero_falls_back_to_master_proxy(self):
        today = date(2026, 9, 18)
        rows = {
            "KOSPI": [
                row("005930", "삼성전자", reference_price=70_000, previous_volume=100),
                row("000660", "SK하이닉스", reference_price=200_000, previous_volume=1_000_000),
            ]
        }

        class ZeroRank:
            def volume_rank(self, _market):
                return [{"mksc_shrn_iscd": "005930", "acml_tr_pbmn": "0", "stck_prpr": "0", "acml_vol": "0"}]

        zero_entries = KisPublicMasterUniverseProvider(
            FakeMasterClient(rows), rank_client=ZeroRank(), today_fn=lambda: today
        ).get_universe(today, markets=("KOSPI",))
        self.assertEqual([item.instrument.ticker for item in zero_entries], ["000660", "005930"])
        self.assertTrue(all("proxy" in item.source for item in zero_entries))

        class BrokenRank:
            def volume_rank(self, _market):
                raise RuntimeError("temporary KIS rank outage")

        fallback = KisPublicMasterUniverseProvider(
            FakeMasterClient(rows), rank_client=BrokenRank(), today_fn=lambda: today
        ).get_universe(today, markets=("KOSPI",))
        self.assertEqual([item.instrument.ticker for item in fallback], ["000660", "005930"])
        self.assertTrue(all("proxy" in item.source for item in fallback))


if __name__ == "__main__":
    unittest.main()
