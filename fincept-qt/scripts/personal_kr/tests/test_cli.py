from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_kr.models import Instrument, QuantCandidate, ResearchResult
from personal_kr.persistence import DecisionStore
from personal_kr.evaluation import Outcome
from personal_kr.universe import KST, UniverseEntry


SCRIPTS_DIR = Path(__file__).resolve().parents[2]
ENTRYPOINT = SCRIPTS_DIR / "personal_kr_terminal.py"


def run_cli(*args: str, input_data: dict | None = None, data_dir: str | None = None):
    env = os.environ.copy()
    if data_dir:
        env["FINCEPT_DATA_DIR"] = data_dir
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT), *args],
        input=json.dumps(input_data, ensure_ascii=False) if input_data is not None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=SCRIPTS_DIR,
        env=env,
        check=False,
    )
    return proc, json.loads(proc.stdout)


class CliIntegrationTests(unittest.TestCase):
    def test_status_returns_structured_readiness(self):
        proc, body = run_cli("status")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["market"], "KR")
        self.assertEqual(body["data"]["execution_mode"], "research_only")
        self.assertIn("credentials", body["data"])
        self.assertIn("llm", body["data"])

        desktop_proc, desktop = run_cli("status", "--llm-provider", "fincept")
        self.assertEqual(desktop_proc.returncode, 0)
        self.assertTrue(desktop["data"]["llm"]["ready"])
        self.assertEqual(desktop["data"]["llm"]["provider"], "fincept")
        self.assertEqual(desktop["data"]["llm"]["source"], "fincept_active_profile")

    def test_future_analysis_date_is_rejected(self):
        proc, body = run_cli(
            "select",
            input_data={
                "analysis_date": "2099-01-01",
                "rows": [{"ticker": "005930", "name": "삼성전자", "score": 90}],
            },
        )
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(body["success"])
        self.assertIn("future", body["error"])

    def test_batch_with_only_malformed_rows_returns_isolated_input_errors_without_kis(self):
        proc, body = run_cli(
            "batch",
            input_data={
                "analysis_date": "2026-09-15",
                "ranking_source": "test-quant-v1",
                "ranking_generated_at": "2026-09-15T15:30:00+09:00",
                "rows": [{"name": "broken", "score": 1}],
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["selected"], [])
        self.assertEqual(body["data"]["results"], [])
        self.assertTrue(body["data"]["input_errors"])

    def test_batch_rejects_quant_ranking_generated_after_cutoff(self):
        proc, body = run_cli(
            "batch",
            input_data={
                "analysis_date": "2026-09-15",
                "ranking_source": "test-quant-v1",
                "ranking_generated_at": "2026-09-16T00:01:00+09:00",
                "rows": [{"ticker": "005930", "name": "삼성전자", "score": 90}],
            },
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("analysis cutoff", body["error"])

    def test_select_wrapper_preserves_korean_utf8_and_top_n(self):
        proc, body = run_cli(
            "select",
            input_data={
                "analysis_date": "2026-08-20",
                "limit": 1,
                "rows": [
                    {"ticker": "005930", "name": "삼성전자", "market": "KOSPI", "score": 92.5},
                    {"ticker": "000660", "name": "SK하이닉스", "market": "KOSPI", "score": 89},
                ],
            },
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(body["success"])
        self.assertEqual(body["data"][0]["instrument"]["name"], "삼성전자")
        self.assertEqual(body["data"][0]["instrument"]["ticker"], "005930")

    def test_historical_discovery_without_exact_snapshot_fails_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, body = run_cli(
                "discover",
                "--analysis-date",
                "2020-01-02",
                "--limit",
                "5",
                data_dir=tmp,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(body["success"])
        self.assertIn("exact PIT universe snapshot", body["error"])

    def test_historical_discovery_replays_snapshot_with_batch_provenance(self):
        snapshot_date = date(2026, 9, 15)
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "personal_kr" / "research.db")
            store.record_universe_snapshot(
                snapshot_date=snapshot_date,
                markets=["KOSPI", "KOSDAQ"],
                entries=[
                    UniverseEntry(
                        Instrument("005930", "삼성전자", "KOSPI"),
                        snapshot_date,
                        trading_value_krw=300_000_000,
                        market_cap_krw=400_000_000_000_000,
                    ),
                    UniverseEntry(
                        Instrument("247540", "에코프로비엠", "KOSDAQ"),
                        snapshot_date,
                        trading_value_krw=200_000_000,
                        market_cap_krw=20_000_000_000_000,
                    ),
                ],
                captured_at=datetime(2026, 9, 15, 14, 30, tzinfo=KST),
            )
            proc, body = run_cli(
                "discover",
                "--analysis-date",
                snapshot_date.isoformat(),
                "--limit",
                "2",
                data_dir=tmp,
            )

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["snapshot_entry_count"], 2)
        self.assertEqual(data["candidates"][0]["instrument"]["ticker"], "005930")
        self.assertEqual(
            data["candidates"][0]["ranking_source"],
            "fincept-kis-public-master-cross-sectional-v2/balanced",
        )
        self.assertEqual(data["candidates"][0]["analysis_cutoff_mode"], "external")
        self.assertEqual(len(data["ranking_payload_hash"]), 64)
        self.assertEqual(data["ranking"]["ranking_source"], data["ranking_source"])
        self.assertEqual(data["ranking"]["rows"][0]["ticker"], "005930")
        self.assertEqual(data["scoring_model"], "cross-sectional-v2")
        self.assertEqual(data["scoring_profile"], "balanced")
        self.assertAlmostEqual(sum(data["scoring_weights"].values()), 1.0)

    def test_paper_trade_wrapper_is_idempotent_and_summary_is_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "personal_kr" / "research.db")
            decision = store.record_decision(
                ResearchResult(
                    candidate=QuantCandidate(Instrument("005930", "삼성전자"), date(2026, 9, 16), 90, 1),
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
            paper_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
            payload = {
                "decision_id": decision.decision_id,
                "client_trade_id": "cli-replay-1",
                "trade_date": paper_date,
                "ticker": "005930",
                "side": "BUY",
                "quantity": 1,
                "price": 70000,
            }
            first_proc, first = run_cli("paper-trade", input_data=payload, data_dir=tmp)
            second_proc, second = run_cli("paper-trade", input_data=payload, data_dir=tmp)
            trades_proc, trades = run_cli("paper-trades", "--limit", "10", data_dir=tmp)
            summary_proc, summary = run_cli("paper-summary", data_dir=tmp)

            self.assertEqual(
                (first_proc.returncode, second_proc.returncode, trades_proc.returncode, summary_proc.returncode),
                (0, 0, 0, 0),
            )
            self.assertEqual(first["data"]["trade_id"], second["data"]["trade_id"])
            self.assertEqual(first["data"]["client_trade_id"], "cli-replay-1")
            self.assertEqual(first["data"]["decision_id"], decision.decision_id)
            self.assertEqual(first["data"]["trade_date"], paper_date)
            self.assertEqual(first["data"]["ticker"], "005930")
            self.assertEqual(first["data"]["side"], "BUY")
            self.assertEqual(first["data"]["quantity"], 1)
            self.assertEqual(first["data"]["price"], 70000.0)
            self.assertEqual(first["data"]["execution_mode"], "paper_only")
            self.assertEqual(first["data"]["cash_krw"], 99_930_000)
            self.assertEqual(second["data"]["cash_krw"], 99_930_000)
            self.assertEqual(trades["data"]["count"], 1)
            self.assertEqual(trades["data"]["execution_mode"], "paper_only")
            self.assertEqual(trades["data"]["trades"][0]["client_trade_id"], "cli-replay-1")
            self.assertEqual(trades["data"]["trades"][0]["company_name"], "삼성전자")
            self.assertEqual(trades["data"]["trades"][0]["signal"], "Hold")
            self.assertEqual(summary["data"]["positions"], {"005930": 1})
            self.assertEqual(summary["data"]["execution_mode"], "paper_only")

    def test_outcomes_wrapper_reads_frozen_alpha_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DecisionStore(Path(tmp) / "personal_kr" / "research.db")
            decision = store.record_decision(
                ResearchResult(
                    candidate=QuantCandidate(Instrument("005930", "삼성전자"), date(2026, 9, 1), 90, 1),
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
            store.record_outcome(
                Outcome(
                    decision.decision_id or "",
                    5,
                    date(2026, 9, 2),
                    date(2026, 9, 8),
                    0.04,
                    0.01,
                    0.03,
                    0.05,
                    -0.02,
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
            )
            proc, body = run_cli("outcomes", decision.decision_id or "", data_dir=tmp)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(body["success"])
            self.assertEqual(body["data"][0]["horizon"], 5)
            self.assertEqual(body["data"][0]["alpha_return"], 0.03)
            self.assertEqual(body["data"][0]["benchmark_symbol"], "^KS11")

    def test_provider_smoke_missing_kis_returns_structured_error(self):
        env_backup = {key: os.environ.pop(key, None) for key in ("KIS_APP_KEY", "KIS_APP_SECRET")}
        try:
            proc, body = run_cli(
                "providers-only", "--ticker", "005930", "--name", "삼성전자", "--market", "KOSPI"
            )
        finally:
            for key, value in env_backup.items():
                if value is not None:
                    os.environ[key] = value

        self.assertEqual(proc.returncode, 1)
        self.assertFalse(body["success"])
        self.assertIn("KIS_APP_KEY", body["error"])


if __name__ == "__main__":
    unittest.main()
