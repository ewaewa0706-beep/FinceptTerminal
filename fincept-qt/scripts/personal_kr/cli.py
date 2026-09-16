"""JSON CLI for Fincept's PythonRunner.

Commands intentionally remain request-driven; there is no background websocket
or polling loop.  All stdout is one JSON object so PythonRunner can validate it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .engine import ResearchEngine
from .benchmark import load_yahoo_benchmark
from .evaluation import calculate_forward_return
from .llm import llm_from_payload
from .models import KR_DAILY_FINALITY_TIME, Instrument, QuantCandidate, to_jsonable
from .persistence import DecisionStore
from .providers import DartClient, EcosClient, KisClient, NaverNewsClient
from .ranking import candidate_from_mapping, select_top_candidates, select_top_candidates_isolated


_KST = timezone(timedelta(hours=9))


def _input_json() -> dict[str, Any]:
    text = sys.stdin.read().strip()
    return json.loads(text) if text else {}


def _optional_input_json() -> dict[str, Any]:
    """Read stdin only when it is a pipe/file, never block an interactive TTY."""

    if sys.stdin.isatty():
        return {}
    return _input_json()


def _print(data: Any, *, success: bool = True, error: str | None = None) -> None:
    print(json.dumps({"success": success, "data": to_jsonable(data), "error": error}, ensure_ascii=False))


def credential_status(llm_provider: str | None = None) -> dict[str, Any]:
    keys = {
        "kis": bool(os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET")),
        "dart": bool(os.getenv("DART_API_KEY")),
        "naver": bool(os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET")),
        "ecos": bool(os.getenv("ECOS_API_KEY")),
        "google": bool(os.getenv("GOOGLE_API_KEY")),
    }
    active_provider = str(llm_provider or "").strip().lower()
    if not active_provider and keys["google"]:
        active_provider = "google"
    llm_ready = bool(active_provider)
    return {
        "market": "KR",
        "refresh_mode": "on_demand",
        "execution_mode": "research_only",
        "credentials": keys,
        "llm": {
            "ready": llm_ready,
            "provider": active_provider or None,
            "source": "fincept_active_profile" if llm_provider else ("headless_google_env" if keys["google"] else None),
        },
        # These fields intentionally describe configuration only. A present key
        # can still be disabled, expired or unauthorized; provider/full smoke is
        # the authoritative live-readiness check.
        "credential_ready": keys["kis"] and llm_ready,
        "full_credentials_configured": keys["kis"] and keys["dart"] and keys["naver"] and keys["ecos"] and llm_ready,
        "readiness_basis": "credential_presence_only",
    }


def _korea_today() -> date:
    """Use Korean civil date regardless of the server's local timezone."""

    # Korea has used UTC+09:00 year-round since 1988.  A fixed offset avoids a
    # hidden dependency on the optional `tzdata` wheel, which Windows Python
    # installations commonly do not include.
    return _korea_now().date()


def _korea_now() -> datetime:
    """Return a timezone-aware Korean civil timestamp for PIT cutoffs."""

    return datetime.now(_KST)


def _finalized_daily_as_of(now_kst: datetime | None = None) -> date:
    """Return the latest Korean civil date safe to treat as a finalized daily bar.

    KIS/Yahoo daily endpoints can expose the current session before its final
    close. Outcome rows are immutable, so freezing a still-moving bar would make
    a partial intraday price the permanent 1/5/20/60-session endpoint. Mirror the
    research PIT rule and only admit today's daily bar after the conservative
    KRX daily-finality boundary.
    """

    now = (now_kst or _korea_now()).astimezone(_KST)
    return now.date() if now.time() >= KR_DAILY_FINALITY_TIME else now.date() - timedelta(days=1)


def _candidate_from_payload(payload: dict[str, Any]) -> QuantCandidate:
    analysis_date = date.fromisoformat(payload["analysis_date"])
    now_kst = _korea_now()
    if analysis_date > now_kst.date():
        raise ValueError("analysis_date cannot be in the future")
    raw_cutoff = str(payload.get("analysis_cutoff_at") or "").strip()
    raw_cutoff_mode = str(payload.get("analysis_cutoff_mode") or "").strip().lower()
    analysis_cutoff_at: datetime | None = None
    analysis_cutoff_mode = "date"
    if raw_cutoff:
        try:
            analysis_cutoff_at = datetime.fromisoformat(raw_cutoff.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("analysis_cutoff_at must be ISO-8601") from exc
        if analysis_cutoff_at.tzinfo is None:
            raise ValueError("analysis_cutoff_at must include a timezone")
        cutoff_kst = analysis_cutoff_at.astimezone(_KST)
        if cutoff_kst.date() != analysis_date:
            raise ValueError("analysis_cutoff_at date must match analysis_date")
        if cutoff_kst > now_kst:
            raise ValueError("analysis_cutoff_at cannot be in the future")
        analysis_cutoff_mode = raw_cutoff_mode or "external"
        if analysis_cutoff_mode == "live_request":
            # Only the desktop's current request (or an equivalent immediate
            # caller) may claim live observation semantics. Stale timestamps must
            # remain external PIT cutoffs so non-vintage ECOS cannot leak.
            if analysis_date != now_kst.date() or now_kst - cutoff_kst > timedelta(minutes=5):
                raise ValueError("live_request cutoff must be a current KST request timestamp")
        elif analysis_cutoff_mode != "external":
            raise ValueError("analysis_cutoff_mode must be external or live_request")
    elif raw_cutoff_mode:
        raise ValueError("analysis_cutoff_mode requires analysis_cutoff_at")
    elif analysis_date == now_kst.date():
        # Manual/UI/MCP research on today's market must freeze the exact request
        # instant. A date-only cutoff could otherwise consume a later close,
        # flow row or news item when the same decision is reconstructed.
        analysis_cutoff_at = now_kst
        analysis_cutoff_mode = "live_request"
    if "instrument" in payload:
        inst = payload["instrument"]
        return QuantCandidate(
            Instrument(inst["ticker"], inst["name"], inst["market"]),
            analysis_date,
            float(payload.get("score", 0)),
            payload.get("rank"),
            {str(k): float(v) for k, v in (payload.get("factors") or {}).items()},
            analysis_cutoff_at=analysis_cutoff_at,
            analysis_cutoff_mode=analysis_cutoff_mode,
        )
    candidate = candidate_from_mapping(payload, payload["analysis_date"])
    return replace(
        candidate,
        analysis_cutoff_at=analysis_cutoff_at,
        analysis_cutoff_mode=analysis_cutoff_mode,
    )


def _engine(llm_config: dict[str, Any] | None = None) -> ResearchEngine:
    kis = KisClient.from_env()
    llm = llm_from_payload(llm_config)
    flow = kis
    fundamentals = DartClient.from_env() if os.getenv("DART_API_KEY") else None
    news = (
        NaverNewsClient.from_env()
        if os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET")
        else None
    )
    macro = EcosClient.from_env() if os.getenv("ECOS_API_KEY") else None
    return ResearchEngine(
        market=kis,
        flow=flow,
        fundamentals=fundamentals,
        news=news,
        macro=macro,
        llm=llm,
    )


def _store() -> DecisionStore:
    data_dir = Path(os.getenv("FINCEPT_DATA_DIR") or Path.home() / ".fincept")
    return DecisionStore(data_dir / "personal_kr" / "research.db")


def _ranking_provenance(payload: dict[str, Any], analysis_date: date) -> tuple[str, datetime, str]:
    source = str(payload.get("ranking_source") or "").strip()
    raw_generated = str(payload.get("ranking_generated_at") or "").strip()
    if not source:
        raise ValueError("ranking_source is required for Quant batch provenance")
    if not raw_generated:
        raise ValueError("ranking_generated_at is required for Quant batch provenance")
    try:
        generated_at = datetime.fromisoformat(raw_generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ranking_generated_at must be ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise ValueError("ranking_generated_at must include a timezone")
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    cutoff = (
        now_kst
        if analysis_date == now_kst.date()
        else datetime.combine(analysis_date, datetime.max.time(), tzinfo=kst)
    )
    if generated_at.astimezone(kst) > cutoff:
        raise ValueError("ranking_generated_at is later than the analysis cutoff")
    canonical = json.dumps(
        {"analysis_date": analysis_date.isoformat(), "rows": payload.get("rows") or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return source, generated_at, hashlib.sha256(canonical).hexdigest()


def cmd_select() -> Any:
    payload = _input_json()
    analysis_date = date.fromisoformat(payload["analysis_date"])
    if analysis_date > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    return select_top_candidates(payload["rows"], analysis_date, int(payload.get("limit", 5)))


def cmd_analyze() -> Any:
    payload = _input_json()
    result = _engine(payload.get("llm")).analyze(_candidate_from_payload(payload))
    strategy_id = str(payload.get("strategy_id") or "personal-kr-single")
    return _store().record_decision(result, strategy_id=strategy_id)


def cmd_batch() -> Any:
    """External Quant Ranking -> Top-N -> isolated deep research in one process.

    One ResearchEngine means one KisClient/token is reused across the whole Top-N
    batch.  This is both the production batch-isolation boundary and the safe KIS
    authentication path; spawning one Python process per candidate would request
    a fresh KIS token for every name.
    """

    payload = _input_json()
    analysis_date = date.fromisoformat(payload["analysis_date"])
    if analysis_date > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    ranking_source, ranking_generated_at, ranking_hash = _ranking_provenance(payload, analysis_date)
    limit = int(payload.get("limit", 5))
    if limit < 1 or limit > 10:
        raise ValueError("production batch limit must be between 1 and 10")
    candidates, input_errors = select_top_candidates_isolated(payload["rows"], analysis_date, limit)
    candidates = [
        replace(
            candidate,
            ranking_source=ranking_source,
            ranking_generated_at=ranking_generated_at,
            ranking_payload_hash=ranking_hash,
            analysis_cutoff_at=ranking_generated_at,
            analysis_cutoff_mode="external",
        )
        for candidate in candidates
    ]
    if not candidates:
        return {
            "selected": [],
            "results": [],
            "input_errors": input_errors,
            "errors": {},
            "execution_mode": "research_only",
        }
    store = _store()
    engine = _engine(payload.get("llm"))
    strategy_id = str(payload.get("strategy_id") or "personal-kr-quant")
    stored = []
    errors: dict[str, str] = {}
    # Analyze and freeze one candidate at a time. If a later candidate stalls,
    # fails, or the outer subprocess watchdog fires, earlier completed decisions
    # have already been checkpointed in SQLite instead of being lost in memory.
    for candidate in candidates:
        try:
            result = engine.analyze(candidate)
            stored.append(store.record_decision(result, strategy_id=strategy_id))
        except Exception as exc:
            errors[candidate.instrument.ticker] = str(exc)
    return {
        "selected": candidates,
        "results": stored,
        "input_errors": input_errors,
        "errors": errors,
        "execution_mode": "research_only",
    }


def cmd_providers_only(args: argparse.Namespace) -> Any:
    as_of = date.fromisoformat(args.analysis_date or _korea_today().isoformat())
    if as_of > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    instrument = Instrument(args.ticker, args.name, args.market)
    kis = KisClient.from_env()
    output: dict[str, Any] = {
        "market": kis.daily_bars(instrument, as_of),
        "flow": kis.investor_flow(instrument, as_of),
    }
    if os.getenv("DART_API_KEY"):
        output["fundamentals"] = DartClient.from_env().fundamentals(instrument, as_of)
    if os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET"):
        output["news"] = NaverNewsClient.from_env().news(instrument, as_of, 10)
    if os.getenv("ECOS_API_KEY"):
        output["macro"] = EcosClient.from_env().macro(as_of)
    return output


def cmd_full(args: argparse.Namespace) -> Any:
    now_kst = _korea_now()
    as_of = date.fromisoformat(args.analysis_date or now_kst.date().isoformat())
    if as_of > now_kst.date():
        raise ValueError("analysis_date cannot be in the future")
    cutoff_at = now_kst if as_of == now_kst.date() else None
    candidate = QuantCandidate(
        Instrument(args.ticker, args.name, args.market),
        as_of,
        0.0,
        1,
        {},
        analysis_cutoff_at=cutoff_at,
        analysis_cutoff_mode="live_request" if cutoff_at is not None else "date",
    )
    result = _engine().analyze(candidate)
    return _store().record_decision(result)


def cmd_decisions(args: argparse.Namespace) -> Any:
    return _store().list_decisions(args.limit)


def cmd_evaluate(args: argparse.Namespace) -> Any:
    store = _store()
    decision = store.get_decision(args.decision_id)
    if decision is None:
        raise ValueError("decision not found")
    instrument = decision.candidate.instrument
    evaluation_as_of = _finalized_daily_as_of()
    lookback_days = max(120, (evaluation_as_of - decision.candidate.analysis_date).days + 30)
    market = KisClient.from_env().daily_bars(
        instrument,
        evaluation_as_of,
        lookback_days=lookback_days,
        # Research evidence uses original prices for PIT stability. Realized
        # outcome measurement instead uses adjusted history so an in-horizon
        # split/reverse-split does not become a fictitious investment return.
        price_mode="adjusted",
    )
    benchmark_symbol = instrument.benchmark_symbol
    benchmark = load_yahoo_benchmark(
        benchmark_symbol,
        decision.candidate.analysis_date - timedelta(days=14),
        evaluation_as_of,
    )
    outcomes = []
    pending_horizons: list[int] = []
    for horizon in args.horizons:
        try:
            outcome = calculate_forward_return(
                args.decision_id,
                market.bars,
                decision.candidate.analysis_date,
                horizon,
                benchmark,
                stock_ticker=instrument.ticker,
                stock_source=market.source,
                stock_price_mode=market.price_mode,
                benchmark_symbol=benchmark_symbol,
                benchmark_source="Yahoo Finance",
                benchmark_price_mode="raw_close",
            )
        except ValueError as exc:
            if str(exc) in {"insufficient future trading sessions", "no future trading session after analysis date"}:
                pending_horizons.append(horizon)
                continue
            raise
        outcomes.append(store.record_outcome(outcome))
    return {
        "decision_id": args.decision_id,
        "outcomes": outcomes,
        "pending_horizons": pending_horizons,
    }


def cmd_outcomes(args: argparse.Namespace) -> Any:
    return _store().list_outcomes(args.decision_id)


def cmd_paper_summary() -> Any:
    cash, positions = _store().paper_summary()
    return {"cash_krw": cash, "positions": positions, "execution_mode": "paper_only"}


def cmd_paper_trade() -> Any:
    payload = _input_json()
    decision_id = str(payload.get("decision_id") or "").strip()
    client_trade_id = str(payload.get("client_trade_id") or "").strip()
    if not decision_id:
        raise ValueError("decision_id is required for paper provenance")
    if not client_trade_id:
        raise ValueError("client_trade_id is required for idempotent paper execution")
    trade_date = date.fromisoformat(payload.get("trade_date") or _korea_today().isoformat())
    if trade_date > _korea_today():
        raise ValueError("paper trade cannot be future-dated")
    store = _store()
    trade_id = store.add_paper_trade(
        decision_id=decision_id,
        client_trade_id=client_trade_id,
        trade_date=trade_date,
        ticker=str(payload["ticker"]),
        side=str(payload["side"]),
        quantity=int(payload["quantity"]),
        price=float(payload["price"]),
        fee=float(payload.get("fee", 0.0)),
        tax=float(payload.get("tax", 0.0)),
    )
    cash, positions = store.paper_summary()
    return {"trade_id": trade_id, "cash_krw": cash, "positions": positions, "execution_mode": "paper_only"}


def cmd_llm_smoke() -> Any:
    payload = _optional_input_json()
    llm_config = payload.get("llm") if isinstance(payload, dict) else None
    llm = llm_from_payload(llm_config)
    response = llm.complete(
        "Return exactly FINCEPT_KR_LLM_OK and nothing else.",
        "Personal Korean-market research LLM connection smoke test.",
    )
    if response.strip() != "FINCEPT_KR_LLM_OK":
        raise RuntimeError(f"unexpected LLM smoke response: {response[:120]}")
    return {
        "provider": str(getattr(llm, "provider", llm.__class__.__name__)).lower(),
        "model": str(getattr(llm, "model", "")),
        "source": "fincept_active_profile" if llm_config else "headless_google_env",
        "response": response,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--llm-provider")
    sub.add_parser("select")
    sub.add_parser("analyze")
    sub.add_parser("batch")
    decisions = sub.add_parser("decisions")
    decisions.add_argument("--limit", type=int, default=100)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("decision_id")
    evaluate.add_argument("--horizons", nargs="+", type=int, default=[1, 5, 20, 60])
    outcomes = sub.add_parser("outcomes")
    outcomes.add_argument("decision_id")
    sub.add_parser("paper-summary")
    sub.add_parser("paper-trade")
    sub.add_parser("llm-smoke")
    for name in ("providers-only", "full"):
        p = sub.add_parser(name)
        p.add_argument("--ticker", default="005930")
        p.add_argument("--name", default="삼성전자")
        p.add_argument("--market", default="KOSPI")
        p.add_argument("--analysis-date")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            _print(credential_status(args.llm_provider))
        elif args.command == "select":
            _print(cmd_select())
        elif args.command == "analyze":
            _print(cmd_analyze())
        elif args.command == "batch":
            _print(cmd_batch())
        elif args.command == "decisions":
            _print(cmd_decisions(args))
        elif args.command == "evaluate":
            _print(cmd_evaluate(args))
        elif args.command == "outcomes":
            _print(cmd_outcomes(args))
        elif args.command == "paper-summary":
            _print(cmd_paper_summary())
        elif args.command == "paper-trade":
            _print(cmd_paper_trade())
        elif args.command == "llm-smoke":
            _print(cmd_llm_smoke())
        elif args.command == "providers-only":
            _print(cmd_providers_only(args))
        elif args.command == "full":
            _print(cmd_full(args))
        return 0
    except Exception as exc:
        _print(None, success=False, error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
