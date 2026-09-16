"""SQLite decision journal and research-only paper portfolio."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .evaluation import Outcome
from .models import KR_DAILY_FINALITY_TIME, ResearchResult, to_jsonable, validate_ticker


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
                # Serialize schema inspection/migration. A deferred transaction
                # lets concurrent constructors both inspect a legacy table and
                # then collide when either starts DDL; take the write reservation
                # before any schema work instead.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS kr_decisions(
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_id,ticker,analysis_date)
                )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS kr_outcomes(
                    decision_id TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(decision_id,horizon)
                )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS kr_outcome_quarantine(
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT,
                    horizon INTEGER,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL
                )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS kr_paper_trades(
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
                )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS kr_paper_trade_quarantine(
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL
                )"""
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(kr_paper_trades)")}
                if "client_trade_id" not in columns:
                    conn.execute("ALTER TABLE kr_paper_trades ADD COLUMN client_trade_id TEXT")
                self._migrate_outcome_provenance(conn)
                self._migrate_paper_provenance(conn)

    def _migrate_outcome_provenance(self, conn: sqlite3.Connection) -> None:
        """Quarantine legacy outcomes that cannot satisfy the immutable provenance contract.

        Older Personal-KR builds stored forward returns before source, price-mode,
        timestamp and input-hash provenance existed. Leaving those rows active
        permanently occupies the ``(decision_id, horizon)`` first-write-wins key,
        so a modern audited re-evaluation can only conflict. Preserve the legacy
        payload for inspection and free the active key for a fully provenanced
        outcome.
        """

        rows = conn.execute(
            "SELECT decision_id,horizon,payload FROM kr_outcomes ORDER BY decision_id,horizon"
        ).fetchall()
        for row in rows:
            reason = self._outcome_quarantine_reason(conn, row)
            if reason is None:
                continue
            conn.execute(
                "INSERT INTO kr_outcome_quarantine(decision_id,horizon,payload,reason,quarantined_at) "
                "VALUES(?,?,?,?,?)",
                (
                    row["decision_id"],
                    row["horizon"],
                    row["payload"],
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                "DELETE FROM kr_outcomes WHERE decision_id=? AND horizon=?",
                (row["decision_id"], row["horizon"]),
            )

    def _outcome_quarantine_reason(self, conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        decision = conn.execute(
            "SELECT ticker,analysis_date,payload FROM kr_decisions WHERE id=?", (row["decision_id"],)
        ).fetchone()
        if decision is None:
            return "missing decision provenance"
        try:
            raw = json.loads(row["payload"])
            if not isinstance(raw, dict):
                return "invalid outcome payload"
            outcome = _outcome_from_payload(raw)
            decision_result = _result_from_payload(json.loads(decision["payload"]))
            analysis_date = date.fromisoformat(decision["analysis_date"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return "invalid outcome payload"

        if outcome.decision_id != row["decision_id"] or outcome.horizon != int(row["horizon"]):
            return "outcome key does not match stored payload"
        if outcome.start_date <= analysis_date or outcome.end_date < outcome.start_date:
            return "invalid outcome date provenance"
        if not outcome.stock_ticker or outcome.stock_ticker != decision["ticker"]:
            return "missing or mismatched stock ticker provenance"
        if outcome.benchmark_symbol != decision_result.candidate.instrument.benchmark_symbol:
            return "missing or mismatched benchmark provenance"
        for label in (
            "stock_source",
            "stock_price_mode",
            "benchmark_source",
            "benchmark_price_mode",
            "evaluation_version",
        ):
            # Check the raw payload as well as the dataclass value. In particular,
            # _outcome_from_payload supplies a default evaluation version for old
            # rows so they remain readable, but that must not make legacy evidence
            # look as though it had been frozen with the modern contract.
            if not str(raw.get(label) or "").strip() or not str(getattr(outcome, label) or "").strip():
                return f"missing {label} provenance"
        finality_error = _outcome_finality_error(outcome)
        if finality_error is not None:
            return finality_error
        if not _is_sha256(outcome.stock_input_hash) or not _is_sha256(outcome.benchmark_input_hash):
            return "missing or invalid outcome input fingerprints"
        if outcome.benchmark_return is None or outcome.alpha_return is None:
            return "missing benchmark return or alpha provenance"
        numeric = (
            outcome.raw_return,
            outcome.benchmark_return,
            outcome.alpha_return,
            outcome.max_gain,
            outcome.max_drawdown,
        )
        try:
            invalid_numeric = any(value is not None and not math.isfinite(float(value)) for value in numeric)
        except (TypeError, ValueError, OverflowError):
            return "invalid outcome metric"
        if invalid_numeric:
            return "non-finite outcome metric"
        return None

    def _migrate_paper_provenance(self, conn: sqlite3.Connection) -> None:
        """Run the paper migration atomically and recover an interrupted old rebuild."""

        savepoint = "personal_kr_paper_migration"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            recovered_stale = self._recover_stale_paper_rebuild(conn)
            if recovered_stale or not self._paper_schema_is_strict(conn):
                self._migrate_paper_rows(conn)
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _paper_schema_is_strict(self, conn: sqlite3.Connection) -> bool:
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(kr_paper_trades)")}
        if not (
            info.get("client_trade_id")
            and info["client_trade_id"][3]
            and info.get("decision_id")
            and info["decision_id"][3]
        ):
            return False
        for index in conn.execute("PRAGMA index_list(kr_paper_trades)"):
            if not index[2]:
                continue
            columns = [row[2] for row in conn.execute(f"PRAGMA index_info('{index[1]}')")]
            if columns == ["client_trade_id"]:
                return True
        return False

    def _recover_stale_paper_rebuild(self, conn: sqlite3.Connection) -> bool:
        """Recover the temp table left by the pre-atomic migration implementation.

        The old ``executescript`` rebuild could be interrupted after copying rows
        and dropping the original table but before renaming the strict table. On
        the next startup the base schema recreates an empty ``kr_paper_trades``;
        restore the copied rows before discarding that stale temp table.
        """

        stale = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kr_paper_trades_strict'"
        ).fetchone()
        if stale is None:
            return False
        main_count = int(conn.execute("SELECT COUNT(*) FROM kr_paper_trades").fetchone()[0])
        stale_count = int(conn.execute("SELECT COUNT(*) FROM kr_paper_trades_strict").fetchone()[0])
        if main_count == 0 and stale_count > 0:
            conn.execute(
                """
                INSERT INTO kr_paper_trades
                    (id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax)
                SELECT id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax
                FROM kr_paper_trades_strict ORDER BY id
                """
            )
        conn.execute("DROP TABLE kr_paper_trades_strict")
        return True

    def _migrate_paper_rows(self, conn: sqlite3.Connection) -> None:
        """Validate legacy paper rows before rebuilding the strict ledger schema.

        Older databases can predate the UNIQUE/CHECK/FK constraints now enforced
        by ``kr_paper_trades``. A bulk INSERT into the strict replacement table
        therefore is not itself a migration strategy: one duplicate idempotency
        key or malformed trade would abort startup with ``IntegrityError``. Walk
        the ledger in its original id order, quarantine anything the current
        runtime would reject, and only then rebuild the table.
        """

        rows = conn.execute(
            """
            SELECT t.*,
                   d.ticker AS decision_ticker,
                   d.analysis_date AS decision_analysis_date,
                   d.created_at AS decision_created_at
            FROM kr_paper_trades t
            LEFT JOIN kr_decisions d ON d.id=t.decision_id
            ORDER BY t.id
            """
        ).fetchall()
        seen_client_ids: set[str] = set()
        latest_trade_date: date | None = None
        positions: dict[str, int] = {}

        for row in rows:
            reason: str | None = None
            client_trade_id = str(row["client_trade_id"] or "").strip()
            decision_id = str(row["decision_id"] or "").strip()
            if not client_trade_id or not decision_id or row["decision_ticker"] is None:
                reason = "missing or invalid decision/client provenance"
            elif client_trade_id in seen_client_ids:
                reason = "duplicate client_trade_id provenance"

            trade_date: date | None = None
            ticker = ""
            side = ""
            quantity = 0
            price = fee = tax = 0.0
            if reason is None:
                try:
                    ticker = validate_ticker(row["ticker"])
                    if ticker != str(row["decision_ticker"]):
                        raise ValueError("ticker does not match decision")
                    side = str(row["side"] or "").strip().upper()
                    raw_quantity = float(row["quantity"])
                    if not math.isfinite(raw_quantity) or not raw_quantity.is_integer():
                        raise ValueError("quantity must be a finite integer")
                    quantity = int(raw_quantity)
                    price = float(row["price"])
                    fee = float(row["fee"])
                    tax = float(row["tax"])
                    if not all(math.isfinite(value) for value in (price, fee, tax)):
                        raise ValueError("price/fee/tax must be finite")
                    if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0 or fee < 0 or tax < 0:
                        raise ValueError("invalid paper trade fields")
                    trade_date = date.fromisoformat(str(row["trade_date"]))
                    analysis_date = date.fromisoformat(str(row["decision_analysis_date"]))
                    created_at = datetime.fromisoformat(str(row["decision_created_at"]))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    created_korea_date = created_at.astimezone(timezone(timedelta(hours=9))).date()
                    if trade_date < analysis_date or trade_date < created_korea_date:
                        raise ValueError("paper trade predates decision provenance")
                    if latest_trade_date is not None and trade_date < latest_trade_date:
                        raise ValueError("paper trade backdates the ledger")
                    held = positions.get(ticker, 0)
                    if side == "SELL" and quantity > held:
                        raise ValueError("paper trade oversells position")
                except (TypeError, ValueError, OverflowError) as exc:
                    reason = f"invalid paper trade provenance: {exc}"

            if reason is not None:
                conn.execute(
                    "INSERT INTO kr_paper_trade_quarantine(original_id,payload,reason,quarantined_at) VALUES(?,?,?,?)",
                    (
                        row["id"],
                        json.dumps(dict(row), ensure_ascii=False, separators=(",", ":"), default=str),
                        reason,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.execute("DELETE FROM kr_paper_trades WHERE id=?", (row["id"],))
                continue

            assert trade_date is not None
            # Persist the same canonical values the current add_paper_trade path
            # would store. This prevents a legacy row such as " buy " or a
            # whitespace-padded id/ticker from passing semantic validation yet
            # remaining a different replay key/string after the strict rebuild.
            conn.execute(
                """
                UPDATE kr_paper_trades
                SET client_trade_id=?, decision_id=?, trade_date=?, ticker=?, side=?,
                    quantity=?, price=?, fee=?, tax=?
                WHERE id=?
                """,
                (
                    client_trade_id,
                    decision_id,
                    trade_date.isoformat(),
                    ticker,
                    side,
                    quantity,
                    price,
                    fee,
                    tax,
                    row["id"],
                ),
            )
            seen_client_ids.add(client_trade_id)
            latest_trade_date = trade_date
            if side == "BUY":
                positions[ticker] = positions.get(ticker, 0) + quantity
            else:
                positions[ticker] = positions.get(ticker, 0) - quantity

        if not self._paper_schema_is_strict(conn):
            conn.execute(
                """CREATE TABLE kr_paper_trades_strict(
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
                )"""
            )
            conn.execute(
                """INSERT INTO kr_paper_trades_strict
                    (id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax)
                SELECT id,client_trade_id,decision_id,trade_date,ticker,side,quantity,price,fee,tax
                FROM kr_paper_trades ORDER BY id"""
            )
            conn.execute("DROP TABLE kr_paper_trades")
            conn.execute("ALTER TABLE kr_paper_trades_strict RENAME TO kr_paper_trades")

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
                    incoming_provenance = _decision_provenance_tuple(result)
                    if incoming_provenance != _decision_provenance_tuple(existing):
                        raise ValueError(
                            "decision provenance conflict: an immutable decision already exists for different "
                            "research inputs"
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
                finality_error = _outcome_finality_error(outcome)
                if finality_error is not None:
                    raise ValueError(finality_error)
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
        c.get("analysis_cutoff_mode") or "",
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


def _decision_provenance_tuple(result: ResearchResult) -> tuple[str, ...]:
    candidate = result.candidate
    ranking_generated = (
        candidate.ranking_generated_at.astimezone(timezone.utc).isoformat()
        if candidate.ranking_generated_at
        else ""
    )
    cutoff = candidate.analysis_cutoff_at.astimezone(timezone.utc).isoformat() if candidate.analysis_cutoff_at else ""
    candidate_payload = to_jsonable(candidate)
    if isinstance(candidate_payload, dict):
        if candidate.ranking_generated_at is not None:
            candidate_payload["ranking_generated_at"] = ranking_generated
        if candidate.analysis_cutoff_at is not None:
            candidate_payload["analysis_cutoff_at"] = cutoff
    candidate_hash = hashlib.sha256(
        json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    evidence_payload = to_jsonable(result.evidence)
    if isinstance(evidence_payload, dict):
        # ResearchEngine freezes the full ResearchPacket, including the
        # candidate. Candidate provenance is already hashed above after UTC
        # normalization, so exclude that duplicate subtree here; otherwise the
        # same instant expressed with a different UTC offset would hash
        # differently even though its provenance is semantically identical.
        evidence_payload = {key: value for key, value in evidence_payload.items() if key != "candidate"}
    evidence_hash = hashlib.sha256(
        json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        candidate.ranking_source,
        ranking_generated,
        candidate.ranking_payload_hash,
        cutoff,
        candidate.analysis_cutoff_mode,
        candidate_hash,
        evidence_hash,
        result.llm_provider,
        result.llm_model_id,
        result.workflow_version,
    )


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


def _outcome_finality_error(outcome: Outcome) -> str | None:
    """Validate that an immutable outcome only uses a finalized daily endpoint."""

    if outcome.evaluated_at is None or outcome.evaluated_at.tzinfo is None:
        return "outcome evaluated_at must be timezone-aware"
    kst = timezone(timedelta(hours=9))
    evaluated_kst = outcome.evaluated_at.astimezone(kst)
    if outcome.end_date > evaluated_kst.date():
        return "outcome end_date cannot be later than evaluated_at"
    if outcome.end_date == evaluated_kst.date() and evaluated_kst.time() < KR_DAILY_FINALITY_TIME:
        return "outcome current-day endpoint is not finalized before 17:00 KST"
    return None
