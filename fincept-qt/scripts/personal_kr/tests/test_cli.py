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
            summary_proc, summary = run_cli("paper-summary", data_dir=tmp)

            self.assertEqual((first_proc.returncode, second_proc.returncode, summary_proc.returncode), (0, 0, 0))
            self.assertEqual(first["data"]["trade_id"], second["data"]["trade_id"])
            self.assertEqual(first["data"]["cash_krw"], 99_930_000)
            self.assertEqual(second["data"]["cash_krw"], 99_930_000)
            self.assertEqual(summary["data"]["positions"], {"005930": 1})
            self.assertEqual(summary["data"]["execution_mode"], "paper_only")

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
