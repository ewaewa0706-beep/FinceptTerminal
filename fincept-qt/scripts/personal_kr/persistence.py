"""SQLite decision journal and research-only paper portfolio."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .evaluation import Outcome
from .models import ResearchResult, to_jsonable, validate_ticker


class DecisionStore:
    def __init__(self, path: str | Path, *, initial_cash: float = 100_000_000.0) -> None:
        self.path = str(path)
        self.initial_cash = float(initial_cash)
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and > 0")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kr_decisions(
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_id,ticker,analysis_date)
                );
                CREATE TABLE IF NOT EXISTS kr_outcomes(
                    decision_id TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(decision_id,horizon)
                );
                CREATE TABLE IF NOT EXISTS kr_paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_trade_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL REFERENCES kr_decisions(id) ON DELETE RESTRICT,
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price REAL NOT NULL CHECK(price > 0),
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS kr_paper_trade_quarantine(
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL
                );
                """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(kr_paper_trades)")}
                if "client_trade_id" not in columns:
                    conn.execute("ALTER TABLE kr_paper_trades ADD COLUMN client_trade_id TEXT")
                self._migrate_paper_provenance(conn)

    def _migrate_paper_provenance(self, conn: sqlite3.Connection) -> None:
        invalid = conn.execute(
            """
            SELECT t.* FROM kr_paper_trades t
            LEFT JOIN kr_decisions d ON d.id=t.decision_id
            WHERE t.client_trade_id IS NULL OR t.client_trade_id=''
               OR t.decision_id IS NULL OR t.decision_id=''
               OR d.id IS NULL
            ORDER BY t.id
            """
        ).fetchall()
        for row in invalid:
            conn.execute(
                "INSERT INTO kr_paper_trade_quarantine(original_id,payload,reason,quarantined_at) VALUES(?,?,?,?)",
                (
                    row["id"],
                    json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")),
                    "missing or invalid decision/client provenance",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute("DELETE FROM kr_paper_trades WHERE id=?", (row["id"],))

        info = {row[1]: row for row in conn.execute("PRAGMA table_info(kr_paper_trades)")}
        strict = bool(info.get("client_trade_id") and info["client_trade_id"][3]) and bool(
            info.get("decision_id") and info["decision_id"][3]
        )
        if not strict:
            conn.executescript(
                """
                CREATE TABLE kr_paper_trades_strict(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_trade_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL REFERENCES kr_decisions(id) ON DELETE RESTRICT,
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    price REAL NOT NULL CHECK(price > 0),
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0
                );
                INSERT INTO kr_paper_trades_strict
                    (id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax)
                SELECT id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax
                FROM kr_paper_trades ORDER BY id;
                DROP TABLE kr_paper_trades;
                ALTER TABLE kr_paper_trades_strict RENAME TO kr_paper_trades;
                """
            )

    def record_decision(self, result: ResearchResult, *, strategy_id: str = "personal-kr") -> ResearchResult:
        ticker = result.candidate.instrument.ticker
        analysis_date = result.candidate.analysis_date.isoformat()
        with closing(self._connect()) as conn:
            with conn:
                # Serialize the read-before-write uniqueness check. Without an
                # immediate transaction, two concurrent research callbacks can
                # both observe "missing" and race on the UNIQUE constraint.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM kr_decisions WHERE strategy_id=? AND ticker=? AND analysis_date=?",
                    (strategy_id, ticker, analysis_date),
                ).fetchone()
                if row:
                    existing = _result_from_payload(json.loads(row["payload"]))
                    incoming_provenance = _ranking_provenance_tuple(result)
                    if any(incoming_provenance) and incoming_provenance != _ranking_provenance_tuple(existing):
                        raise ValueError(
                            "decision provenance conflict: an immutable decision already exists for a different "
                            "quant ranking run"
                        )
                    return existing
                decision_id = str(uuid.uuid4())
                frozen = replace(result, decision_id=decision_id, strategy_id=strategy_id)
                payload = json.dumps(to_jsonable(frozen), ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO kr_decisions(id,strategy_id,ticker,analysis_date,signal,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        strategy_id,
                        ticker,
                        analysis_date,
                        frozen.signal,
                        payload,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                return frozen

    def get_decision(self, decision_id: str) -> ResearchResult | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM kr_decisions WHERE id=?", (decision_id,)).fetchone()
        return _result_from_payload(json.loads(row["payload"])) if row else None

    def list_decisions(self, limit: int = 100) -> list[ResearchResult]:
        limit = min(max(int(limit), 1), 500)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload FROM kr_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_result_from_payload(json.loads(row["payload"])) for row in rows]

    def record_outcome(self, outcome: Outcome) -> Outcome:
        if outcome.horizon < 1:
            raise ValueError("outcome horizon must be >= 1")
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                decision = conn.execute(
                    "SELECT ticker,analysis_date,payload FROM kr_decisions WHERE id=?", (outcome.decision_id,)
                ).fetchone()
                if decision is None:
                    raise ValueError("outcome decision not found")
                decision_result = _result_from_payload(json.loads(decision["payload"]))
                analysis_date = date.fromisoformat(decision["analysis_date"])
                if outcome.start_date <= analysis_date:
                    raise ValueError("outcome start_date must be after decision analysis_date")
                if outcome.end_date < outcome.start_date:
                    raise ValueError("outcome end_date cannot precede start_date")
                if not outcome.stock_ticker:
                    raise ValueError("outcome stock_ticker provenance is required")
                if outcome.stock_ticker != decision["ticker"]:
                    raise ValueError("outcome ticker does not match decision")
                expected_benchmark = decision_result.candidate.instrument.benchmark_symbol
                if outcome.benchmark_symbol != expected_benchmark:
                    raise ValueError("outcome benchmark_symbol does not match decision market")
                for label, value in (
                    ("stock_source", outcome.stock_source),
                    ("stock_price_mode", outcome.stock_price_mode),
                    ("benchmark_source", outcome.benchmark_source),
                    ("benchmark_price_mode", outcome.benchmark_price_mode),
                    ("evaluation_version", outcome.evaluation_version),
                ):
                    if not str(value or "").strip():
                        raise ValueError(f"outcome {label} provenance is required")
                if outcome.evaluated_at is None or outcome.evaluated_at.tzinfo is None:
                    raise ValueError("outcome evaluated_at must be timezone-aware")
                if not _is_sha256(outcome.stock_input_hash) or not _is_sha256(outcome.benchmark_input_hash):
                    raise ValueError("outcome stock/benchmark input hashes must be SHA-256 fingerprints")
                if outcome.benchmark_return is None or outcome.alpha_return is None:
                    raise ValueError("outcome benchmark return and alpha are required")
                numeric = (
                    outcome.raw_return,
                    outcome.benchmark_return,
                    outcome.alpha_return,
                    outcome.max_gain,
                    outcome.max_drawdown,
                )
                if any(value is not None and not math.isfinite(float(value)) for value in numeric):
                    raise ValueError("outcome metrics must be finite")
                payload = json.dumps(to_jsonable(outcome), ensure_ascii=False, separators=(",", ":"))
                existing_row = conn.execute(
                    "SELECT payload FROM kr_outcomes WHERE decision_id=? AND horizon=?",
                    (outcome.decision_id, outcome.horizon),
                ).fetchone()
                if existing_row is not None:
                    existing = _outcome_from_payload(json.loads(existing_row["payload"]))
                    incoming_provenance = _outcome_provenance_tuple(outcome)
                    existing_provenance = _outcome_provenance_tuple(existing)
                    if any(incoming_provenance + existing_provenance) and incoming_provenance != existing_provenance:
                        raise ValueError(
                            "outcome provenance conflict: immutable outcome already exists for different price inputs"
                        )
                    return existing
                conn.execute(
                    "INSERT INTO kr_outcomes(decision_id,horizon,payload,created_at) VALUES(?,?,?,?)",
                    (outcome.decision_id, outcome.horizon, payload, datetime.now(timezone.utc).isoformat()),
                )
                row = conn.execute(
                    "SELECT payload FROM kr_outcomes WHERE decision_id=? AND horizon=?",
                    (outcome.decision_id, outcome.horizon),
                ).fetchone()
        assert row is not None
        return _outcome_from_payload(json.loads(row["payload"]))

    def list_outcomes(self, decision_id: str) -> list[Outcome]:
        """Return frozen outcome/alpha observations for one decision by horizon."""

        decision_id = str(decision_id).strip()
        if not decision_id:
            raise ValueError("decision_id is required")
        with closing(self._connect()) as conn:
            decision = conn.execute("SELECT 1 FROM kr_decisions WHERE id=?", (decision_id,)).fetchone()
            if decision is None:
                raise ValueError("decision not found")
            rows = conn.execute(
                "SELECT payload FROM kr_outcomes WHERE decision_id=? ORDER BY horizon",
                (decision_id,),
            ).fetchall()
        return [_outcome_from_payload(json.loads(row["payload"])) for row in rows]

    def add_paper_trade(
        self,
        *,
        trade_date: date,
        ticker: str,
        side: str,
        quantity: int,
        price: float,
        decision_id: str | None = None,
        client_trade_id: str | None = None,
        fee: float = 0.0,
        tax: float = 0.0,
    ) -> int:
        ticker = validate_ticker(ticker)
        side = side.upper()
        decision_id = str(decision_id or "").strip()
        client_trade_id = str(client_trade_id or "").strip()
        if not decision_id:
            raise ValueError("decision_id is required for paper provenance")
        if not client_trade_id:
            raise ValueError("client_trade_id is required for idempotent paper execution")
        values = (float(price), float(fee), float(tax))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("paper trade price/fee/tax must be finite")
        if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0 or fee < 0 or tax < 0:
            raise ValueError("invalid paper trade")
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM kr_paper_trades WHERE client_trade_id=?", (client_trade_id,)
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["decision_id"] == decision_id
                        and existing["trade_date"] == trade_date.isoformat()
                        and existing["ticker"] == ticker
                        and existing["side"] == side
                        and int(existing["quantity"]) == quantity
                        and float(existing["price"]) == float(price)
                        and float(existing["fee"]) == float(fee)
                        and float(existing["tax"]) == float(tax)
                    )
                    if not same:
                        raise ValueError("client_trade_id replay conflicts with the original paper trade")
                    return int(existing["id"])
                decision = conn.execute(
                    "SELECT ticker,analysis_date,created_at FROM kr_decisions WHERE id=?", (decision_id,)
                ).fetchone()
                if decision is None:
                    raise ValueError("decision not found")
                if decision["ticker"] != ticker:
                    raise ValueError("paper trade ticker does not match decision")
                if trade_date < date.fromisoformat(decision["analysis_date"]):
                    raise ValueError("paper trade cannot predate decision")
                created_at = datetime.fromisoformat(decision["created_at"])
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                created_korea_date = created_at.astimezone(timezone(timedelta(hours=9))).date()
                if trade_date < created_korea_date:
                    raise ValueError("paper trade cannot predate the actual decision creation date")
                latest_trade = conn.execute("SELECT MAX(trade_date) FROM kr_paper_trades").fetchone()[0]
                if latest_trade and trade_date < date.fromisoformat(str(latest_trade)):
                    raise ValueError("paper trade cannot be backdated before the latest ledger trade")
                cash, positions = self.paper_summary(conn)
                held = positions.get(ticker, 0)
                cost = quantity * price + fee + tax
                if side == "BUY" and cost > cash + 1e-9:
                    raise ValueError("insufficient paper cash")
                if side == "SELL" and quantity > held:
                    raise ValueError("cannot oversell paper position")
                cursor = conn.execute(
                    "INSERT INTO kr_paper_trades(client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (client_trade_id, decision_id, trade_date.isoformat(), ticker, side, quantity, price, fee, tax),
                )
                return int(cursor.lastrowid)

    def paper_summary(self, conn: sqlite3.Connection | None = None) -> tuple[float, dict[str, int]]:
        owns = conn is None
        connection = conn or self._connect()
        try:
            cash = self.initial_cash
            positions: dict[str, int] = {}
            for row in connection.execute(
                "SELECT t.* FROM kr_paper_trades t JOIN kr_decisions d ON d.id=t.decision_id ORDER BY t.id"
            ):
                qty = int(row["quantity"])
                gross = qty * float(row["price"])
                extras = float(row["fee"]) + float(row["tax"])
                ticker = row["ticker"]
                if row["side"] == "BUY":
                    cash -= gross + extras
                    positions[ticker] = positions.get(ticker, 0) + qty
                else:
                    cash += gross - extras
                    positions[ticker] = positions.get(ticker, 0) - qty
            return cash, {ticker: qty for ticker, qty in positions.items() if qty}
        finally:
            if owns:
                connection.close()


def _result_from_payload(data: dict) -> ResearchResult:
    from .models import Instrument, QuantCandidate

    c = data["candidate"]
    i = c["instrument"]
    candidate = QuantCandidate(
        Instrument(i["ticker"], i["name"], i["market"], i.get("currency", "KRW")),
        date.fromisoformat(c["analysis_date"]),
        c["score"],
        c.get("rank"),
        c.get("factors") or {},
        c.get("ranking_source") or "",
        datetime.fromisoformat(c["ranking_generated_at"]) if c.get("ranking_generated_at") else None,
        c.get("ranking_payload_hash") or "",
        datetime.fromisoformat(c["analysis_cutoff_at"]) if c.get("analysis_cutoff_at") else None,
    )
    return ResearchResult(
        candidate=candidate,
        signal=data["signal"],
        market_report=data["market_report"],
        fundamentals_report=data["fundamentals_report"],
        news_macro_report=data["news_macro_report"],
        bull_case=data["bull_case"],
        bear_case=data["bear_case"],
        research_manager=data["research_manager"],
        trader=data["trader"],
        risk_manager=data["risk_manager"],
        portfolio_manager=data["portfolio_manager"],
        unavailable=tuple(data.get("unavailable") or ()),
        unavailable_reasons={str(k): str(v) for k, v in (data.get("unavailable_reasons") or {}).items()},
        decision_id=data.get("decision_id"),
        strategy_id=data.get("strategy_id") or "personal-kr",
        generated_at=datetime.fromisoformat(data["generated_at"]) if data.get("generated_at") else None,
        evidence=data.get("evidence") or {},
        llm_provider=data.get("llm_provider") or "",
        llm_model_id=data.get("llm_model_id") or "",
        workflow_version=data.get("workflow_version") or "personal-kr-v1",
    )


def _outcome_from_payload(data: dict) -> Outcome:
    return Outcome(
        decision_id=data["decision_id"],
        horizon=int(data["horizon"]),
        start_date=date.fromisoformat(data["start_date"]),
        end_date=date.fromisoformat(data["end_date"]),
        raw_return=float(data["raw_return"]),
        benchmark_return=data.get("benchmark_return"),
        alpha_return=data.get("alpha_return"),
        max_gain=data.get("max_gain"),
        max_drawdown=data.get("max_drawdown"),
        stock_ticker=data.get("stock_ticker"),
        stock_source=data.get("stock_source"),
        stock_price_mode=data.get("stock_price_mode"),
        benchmark_symbol=data.get("benchmark_symbol"),
        benchmark_source=data.get("benchmark_source"),
        benchmark_price_mode=data.get("benchmark_price_mode"),
        evaluated_at=datetime.fromisoformat(data["evaluated_at"]) if data.get("evaluated_at") else None,
        stock_input_hash=data.get("stock_input_hash"),
        benchmark_input_hash=data.get("benchmark_input_hash"),
        evaluation_version=data.get("evaluation_version") or "personal-kr-outcome-v1",
    )


def _ranking_provenance_tuple(result: ResearchResult) -> tuple[str, str, str]:
    candidate = result.candidate
    generated = candidate.ranking_generated_at.isoformat() if candidate.ranking_generated_at else ""
    return candidate.ranking_source, generated, candidate.ranking_payload_hash


def _outcome_provenance_tuple(outcome: Outcome) -> tuple[str, ...]:
    return (
        outcome.stock_ticker or "",
        outcome.stock_source or "",
        outcome.stock_price_mode or "",
        outcome.benchmark_symbol or "",
        outcome.benchmark_source or "",
        outcome.benchmark_price_mode or "",
        outcome.stock_input_hash or "",
        outcome.benchmark_input_hash or "",
        outcome.evaluation_version or "",
    )


def _is_sha256(value: str | None) -> bool:
    if value is None or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
