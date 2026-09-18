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
from .quant_scoring import (
    QUANT_PROFILE_NAMES,
    QuantFeatureRecord,
    score_quant_records,
    weights_for_quant_profile,
)
from .ranking import candidate_from_mapping, select_top_candidates, select_top_candidates_isolated
from .discovery_scoring import DISCOVERY_PROFILE_NAMES, score_universe_entries, weights_for_profile
from .universe import (
    KisPublicMasterUniverseProvider,
    KrxClient,
    KrxUniverseProvider,
    SnapshotAwareUniverseProvider,
    krx_auth_key_configured,
)


_KST = timezone(timedelta(hours=9))
_KRX_RECONSTRUCTION_RANKING_PREFIX = "fincept-krx-openapi-historical-cross-sectional-v2"


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
        "krx": krx_auth_key_configured(),
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
        "universe": {
            "current_ready": True,
            "source": "KIS public master (keyless current snapshot)",
            "historical_mode": (
                "exact_snapshot_replay_or_krx_reconstruction"
                if keys["krx"]
                else "exact_snapshot_replay_only"
            ),
            "historical_krx_ready": keys["krx"],
        },
        "quant_ranking": {
            "ready": keys["kis"],
            "mode": "bounded_kis_feature_rank",
            "historical_mode": "current_date_only",
            "default_cache_ttl_seconds": 300,
        },
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
    (
        ranking_source,
        ranking_generated_at,
        ranking_payload_hash,
        ranking_mode,
        ranking_data_as_of,
    ) = _candidate_ranking_provenance(
        payload,
        analysis_date=analysis_date,
        analysis_cutoff_at=analysis_cutoff_at,
        now_kst=now_kst,
    )
    if "instrument" in payload:
        inst = payload["instrument"]
        return QuantCandidate(
            Instrument(inst["ticker"], inst["name"], inst["market"]),
            analysis_date,
            float(payload.get("score", 0)),
            payload.get("rank"),
            {str(k): float(v) for k, v in (payload.get("factors") or {}).items()},
            ranking_source=ranking_source,
            ranking_generated_at=ranking_generated_at,
            ranking_payload_hash=ranking_payload_hash,
            analysis_cutoff_at=analysis_cutoff_at,
            analysis_cutoff_mode=analysis_cutoff_mode,
            ranking_mode=ranking_mode,
            ranking_data_as_of=ranking_data_as_of,
        )
    candidate = candidate_from_mapping(payload, payload["analysis_date"])
    return replace(
        candidate,
        ranking_source=ranking_source,
        ranking_generated_at=ranking_generated_at,
        ranking_payload_hash=ranking_payload_hash,
        analysis_cutoff_at=analysis_cutoff_at,
        analysis_cutoff_mode=analysis_cutoff_mode,
        ranking_mode=ranking_mode,
        ranking_data_as_of=ranking_data_as_of,
    )


def _candidate_ranking_provenance(
    payload: dict[str, Any],
    *,
    analysis_date: date,
    analysis_cutoff_at: datetime | None,
    now_kst: datetime,
) -> tuple[str, datetime | None, str, str, date | None]:
    """Validate optional ranking provenance on direct/single-candidate research."""

    source = str(payload.get("ranking_source") or "").strip()
    raw_generated = str(payload.get("ranking_generated_at") or "").strip()
    payload_hash = str(payload.get("ranking_payload_hash") or "").strip().lower()
    raw_mode = str(payload.get("ranking_mode") or "").strip().lower()
    raw_data_as_of = str(payload.get("ranking_data_as_of") or "").strip()
    present = bool(source or raw_generated or payload_hash or raw_mode or raw_data_as_of)
    if not present:
        return "", None, "", "", None
    if not source or not raw_generated or not payload_hash:
        raise ValueError(
            "ranking_source, ranking_generated_at and ranking_payload_hash must be supplied together"
        )
    if len(payload_hash) != 64 or any(ch not in "0123456789abcdef" for ch in payload_hash):
        raise ValueError("ranking_payload_hash must be a 64-character lowercase hex SHA-256")
    try:
        generated_at = datetime.fromisoformat(raw_generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ranking_generated_at must be ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise ValueError("ranking_generated_at must include a timezone")
    ranking_mode = raw_mode or "observed"
    if ranking_mode not in {"observed", "historical_reconstruction"}:
        raise ValueError("ranking_mode must be observed or historical_reconstruction")
    if raw_data_as_of:
        try:
            ranking_data_as_of = date.fromisoformat(raw_data_as_of)
        except ValueError as exc:
            raise ValueError("ranking_data_as_of must be ISO date") from exc
    else:
        ranking_data_as_of = analysis_date if ranking_mode == "observed" else None
    if ranking_mode == "historical_reconstruction" and ranking_data_as_of is None:
        raise ValueError("historical_reconstruction requires ranking_data_as_of")
    if ranking_data_as_of is not None and ranking_data_as_of > analysis_date:
        raise ValueError("ranking_data_as_of cannot be later than analysis_date")
    generated_kst = generated_at.astimezone(_KST)
    is_reconstruction = ranking_mode == "historical_reconstruction"
    if generated_kst > now_kst:
        raise ValueError("ranking_generated_at cannot be in the future")
    if is_reconstruction:
        if analysis_cutoff_at is None:
            raise ValueError("historical_reconstruction requires analysis_cutoff_at")
        cutoff_kst = analysis_cutoff_at.astimezone(_KST)
        if ranking_data_as_of is not None and ranking_data_as_of > cutoff_kst.date():
            raise ValueError("ranking_data_as_of cannot be later than analysis_cutoff_at")
    if generated_kst.date() > analysis_date and not is_reconstruction:
        raise ValueError("ranking_generated_at cannot be later than analysis_date")
    if (
        analysis_cutoff_at is not None
        and generated_at > analysis_cutoff_at
        and not is_reconstruction
    ):
        raise ValueError("ranking_generated_at cannot be later than analysis_cutoff_at")
    return source, generated_at, payload_hash, ranking_mode, ranking_data_as_of


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


def _ranking_provenance(
    payload: dict[str, Any],
    analysis_date: date,
) -> tuple[str, datetime, str, str, date]:
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
    ranking_mode = str(payload.get("ranking_mode") or "observed").strip().lower()
    if ranking_mode not in {"observed", "historical_reconstruction"}:
        raise ValueError("ranking_mode must be observed or historical_reconstruction")
    raw_data_as_of = str(payload.get("ranking_data_as_of") or "").strip()
    if raw_data_as_of:
        try:
            ranking_data_as_of = date.fromisoformat(raw_data_as_of)
        except ValueError as exc:
            raise ValueError("ranking_data_as_of must be ISO date") from exc
    elif ranking_mode == "observed":
        ranking_data_as_of = analysis_date
    else:
        raise ValueError("historical_reconstruction requires ranking_data_as_of")
    if ranking_data_as_of > analysis_date:
        raise ValueError("ranking_data_as_of cannot be later than analysis_date")
    kst = timezone(timedelta(hours=9))
    now_kst = _korea_now()
    cutoff = (
        now_kst
        if analysis_date == now_kst.date()
        else datetime.combine(analysis_date, datetime.max.time(), tzinfo=kst)
    )
    if generated_at.astimezone(kst) > now_kst:
        raise ValueError("ranking_generated_at cannot be in the future")
    if (
        ranking_mode != "historical_reconstruction"
        and generated_at.astimezone(kst) > cutoff
    ):
        raise ValueError("ranking_generated_at is later than the analysis cutoff")
    return (
        source,
        generated_at,
        _ranking_payload_hash(payload, analysis_date),
        ranking_mode,
        ranking_data_as_of,
    )


def _ranking_payload_hash(payload: dict[str, Any], analysis_date: date) -> str:
    canonical = json.dumps(
        {"analysis_date": analysis_date.isoformat(), "rows": payload.get("rows") or []},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def cmd_select() -> Any:
    payload = _input_json()
    analysis_date = date.fromisoformat(payload["analysis_date"])
    if analysis_date > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    return select_top_candidates(payload["rows"], analysis_date, int(payload.get("limit", 5)))


def cmd_discover(args: argparse.Namespace) -> Any:
    """Whole-market discovery with observed snapshots and explicit KRX reconstruction."""

    as_of = date.fromisoformat(args.analysis_date or _korea_today().isoformat())
    if as_of > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    limit = int(args.limit)
    if limit < 1 or limit > 200:
        raise ValueError("discovery limit must be between 1 and 200")
    min_trading_value_krw = int(args.min_trading_value_krw)
    if min_trading_value_krw < 0:
        raise ValueError("min_trading_value_krw must be >= 0")
    markets = tuple(args.market or ("KOSPI", "KOSDAQ"))

    store = _store()
    rank_client = KisClient.from_env() if os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET") else None
    historical_provider = KrxUniverseProvider(KrxClient.from_env()) if krx_auth_key_configured() else None
    provider = SnapshotAwareUniverseProvider(
        KisPublicMasterUniverseProvider(rank_client=rank_client, today_fn=_korea_today),
        store,
        historical_provider=historical_provider,
        today_fn=_korea_today,
        now_fn=_korea_now,
    )
    all_entries = provider.get_universe(
        as_of,
        markets=markets,
        min_trading_value_krw=0,
    )
    entries = [
        entry
        for entry in all_entries
        if (entry.trading_value_krw or 0) >= min_trading_value_krw
    ]
    discovery_profile = str(args.profile or "balanced")
    discovery_weights = weights_for_profile(discovery_profile)
    candidates = score_universe_entries(entries, as_of, limit=limit, weights=discovery_weights)
    snapshot = store.get_universe_snapshot(as_of, markets=list(dict.fromkeys(markets)))
    reconstructed_at: datetime | None = None
    resolved_data_date: date | None = None
    analysis_cutoff_at: datetime
    if snapshot is not None:
        ranking_source = f"fincept-kis-public-master-cross-sectional-v2/{discovery_profile}"
        frozen_generated_at = snapshot.captured_at
        analysis_cutoff_at = snapshot.captured_at
        ranking_mode = "observed"
        ranking_data_as_of = as_of
        output_source = "KIS public master current snapshot / exact PIT replay"
    else:
        if historical_provider is None or as_of >= _korea_today():
            raise RuntimeError("universe snapshot missing after discovery")
        resolved_dates = {entry.as_of for entry in all_entries}
        if len(resolved_dates) != 1:
            raise RuntimeError("KRX historical reconstruction must resolve to one exchange session")
        resolved_data_date = next(iter(resolved_dates))
        reconstructed_at = _korea_now()
        ranking_source = (
            f"{_KRX_RECONSTRUCTION_RANKING_PREFIX}/{discovery_profile}"
            f";data_as_of={resolved_data_date.isoformat()}"
        )
        frozen_generated_at = reconstructed_at
        analysis_cutoff_at = datetime.combine(as_of, KR_DAILY_FINALITY_TIME, tzinfo=_KST)
        ranking_mode = "historical_reconstruction"
        ranking_data_as_of = resolved_data_date
        output_source = "KRX OpenAPI historical reconstruction"
    entries_by_ticker = {entry.instrument.ticker: entry for entry in entries}
    flat_rows = [
        {
            "ticker": candidate.instrument.ticker,
            "name": candidate.instrument.name,
            "market": candidate.instrument.market,
            "score": candidate.score,
            "rank": candidate.rank,
            "liquidity_source": entries_by_ticker[candidate.instrument.ticker].source,
            **candidate.factors,
        }
        for candidate in candidates
    ]
    rank_overlay_count = sum("volume-rank" in entry.source for entry in entries)
    ranking_payload = {
        "analysis_date": as_of.isoformat(),
        "ranking_source": ranking_source,
        "ranking_generated_at": frozen_generated_at.isoformat(),
        "ranking_mode": ranking_mode,
        "ranking_data_as_of": ranking_data_as_of.isoformat(),
        "limit": limit,
        "rows": flat_rows,
    }
    if snapshot is not None:
        (
            frozen_source,
            frozen_generated_at,
            ranking_hash,
            frozen_mode,
            frozen_data_as_of,
        ) = _ranking_provenance(ranking_payload, as_of)
    else:
        frozen_source = ranking_source
        ranking_hash = _ranking_payload_hash(ranking_payload, as_of)
        frozen_mode = ranking_mode
        frozen_data_as_of = ranking_data_as_of
    candidates = [
        replace(
            candidate,
            ranking_source=frozen_source,
            ranking_generated_at=frozen_generated_at,
            ranking_payload_hash=ranking_hash,
            analysis_cutoff_at=analysis_cutoff_at,
            analysis_cutoff_mode="external",
            ranking_mode=frozen_mode,
            ranking_data_as_of=frozen_data_as_of,
        )
        for candidate in candidates
    ]
    return {
        "analysis_date": as_of,
        "snapshot_entry_count": len(snapshot.entries) if snapshot is not None else None,
        "eligible_count": len(entries),
        "snapshot_hash": snapshot.payload_sha256 if snapshot is not None else None,
        "captured_at": snapshot.captured_at if snapshot is not None else None,
        "reconstructed_at": reconstructed_at,
        "resolved_data_date": resolved_data_date,
        "source": output_source,
        "rank_overlay_count": rank_overlay_count,
        "scoring_model": "cross-sectional-v2",
        "scoring_profile": discovery_profile,
        "scoring_weights": discovery_weights.normalized(),
        "ranking_source": frozen_source,
        "ranking_generated_at": frozen_generated_at,
        "ranking_payload_hash": ranking_hash,
        "ranking_mode": frozen_mode,
        "ranking_data_as_of": frozen_data_as_of,
        "candidates": candidates,
        "ranking": ranking_payload,
        "execution_mode": "research_only",
    }


def cmd_quant_rank(args: argparse.Namespace) -> Any:
    """Bounded KIS feature ranking over the cheap whole-market prefilter.

    v1 is deliberately current-date only. KIS's investor-flow quote does not
    expose an arbitrary historical date input, so pretending it can reproduce a
    past cross-section would violate the terminal's PIT contract.
    """

    analysis_date = date.fromisoformat(args.analysis_date or _korea_today().isoformat())
    today = _korea_today()
    if analysis_date != today:
        raise ValueError(
            "quant-rank v1 is current-date only because KIS investor flow is not an arbitrary-date historical endpoint"
        )
    if not (os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET")):
        raise ValueError("KIS_APP_KEY and KIS_APP_SECRET are required for quant-rank")

    limit = int(args.limit)
    prefilter_limit = int(args.prefilter_limit)
    lookback_days = int(args.lookback_days)
    if limit < 1 or limit > 10:
        raise ValueError("quant-rank limit must be between 1 and 10")
    if prefilter_limit < limit or prefilter_limit > 50:
        raise ValueError("prefilter_limit must be >= limit and <= 50")
    if lookback_days < 90 or lookback_days > 365:
        raise ValueError("lookback_days must be between 90 and 365")

    quant_profile = str(args.profile or "balanced")
    quant_weights = weights_for_quant_profile(quant_profile)
    discovery_profile = str(args.discovery_profile or "balanced")
    markets = list(args.market or [])
    cache_ttl_seconds = int(getattr(args, "cache_ttl_seconds", 0) or 0)
    if cache_ttl_seconds < 0 or cache_ttl_seconds > 3600:
        raise ValueError("cache_ttl_seconds must be between 0 and 3600")
    force_refresh = bool(getattr(args, "refresh", False))
    cache_key = _quant_rank_cache_key(
        analysis_date=analysis_date,
        markets=markets,
        limit=limit,
        prefilter_limit=prefilter_limit,
        lookback_days=lookback_days,
        min_trading_value_krw=int(args.min_trading_value_krw),
        quant_profile=quant_profile,
        discovery_profile=discovery_profile,
        dart_enabled=bool(os.getenv("DART_API_KEY")),
    )
    cache_store = _store() if cache_ttl_seconds > 0 else None
    if cache_store is not None and not force_refresh:
        cached = cache_store.get_quant_rank_cache(cache_key, now=_korea_now())
        if cached is not None:
            try:
                result = _hydrate_quant_rank_result(cached["payload"])
            except (KeyError, TypeError, ValueError):
                # The API cache is mutable acceleration only, never PIT evidence.
                # A structurally valid JSON row can still be stale/corrupt or
                # internally inconsistent, so discard it and rebuild from the
                # providers instead of surfacing mismatched provenance.
                cache_store.delete_quant_rank_cache(cache_key)
            else:
                result.update(
                    {
                        "cache_enabled": True,
                        "cache_hit": True,
                        "cache_key": cache_key,
                        "cache_created_at": cached["created_at"],
                        "cache_expires_at": cached["expires_at"],
                        "cache_ttl_seconds": cache_ttl_seconds,
                    }
                )
                return result
    discovery = cmd_discover(
        argparse.Namespace(
            analysis_date=analysis_date.isoformat(),
            market=markets or None,
            limit=prefilter_limit,
            min_trading_value_krw=int(args.min_trading_value_krw),
            profile=discovery_profile,
        )
    )
    upstream_candidates = list(discovery.get("candidates") or [])
    if not upstream_candidates:
        raise RuntimeError("whole-market prefilter produced no quant candidates")

    kis = KisClient.from_env()
    dart = DartClient.from_env() if os.getenv("DART_API_KEY") else None
    records: list[QuantFeatureRecord] = []
    market_errors: dict[str, str] = {}
    flow_errors: dict[str, str] = {}
    fundamental_errors: dict[str, str] = {}
    for candidate in upstream_candidates[:prefilter_limit]:
        instrument = candidate.instrument
        try:
            market_snapshot = kis.daily_bars(
                instrument,
                analysis_date,
                lookback_days=lookback_days,
                price_mode="original",
            )
        except Exception as exc:
            market_errors[instrument.ticker] = _safe_quant_error(exc)
            continue

        flow_snapshot = None
        try:
            flow_snapshot = kis.investor_flow(instrument, analysis_date)
        except Exception as exc:
            flow_errors[instrument.ticker] = _safe_quant_error(exc)

        records.append(
            QuantFeatureRecord(
                instrument=instrument,
                analysis_date=analysis_date,
                market=market_snapshot,
                flow=flow_snapshot,
            )
        )

    if not records:
        raise RuntimeError("quant-rank could not load market features for any prefiltered ticker")

    # Stage 1: KIS-only ranking across the bounded discovery prefilter. DART can
    # require multiple HTTP requests per company, so never fan it out across the
    # entire prefilter. Keep a wider finalist set than the requested Top-N, then
    # let fundamentals rerank only that bounded slice.
    preliminary_limit = min(len(records), max(limit, min(prefilter_limit, 15)))
    preliminary_candidates, preliminary_rows = score_quant_records(
        records,
        analysis_date,
        limit=preliminary_limit,
        weights=quant_weights,
    )
    if not preliminary_candidates:
        raise RuntimeError("quant-rank produced no scored candidates")

    preliminary_feature_payload_hash = hashlib.sha256(
        json.dumps(
            preliminary_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    candidates = preliminary_candidates[:limit]
    feature_rows = preliminary_rows
    dart_enrichment_count = 0
    if dart is not None:
        record_by_ticker = {record.instrument.ticker: record for record in records}
        enriched_records: list[QuantFeatureRecord] = []
        for candidate in preliminary_candidates:
            base = record_by_ticker[candidate.instrument.ticker]
            fundamentals_snapshot = None
            try:
                fundamentals_snapshot = dart.fundamentals(candidate.instrument, analysis_date)
                dart_enrichment_count += 1
            except Exception as exc:
                fundamental_errors[candidate.instrument.ticker] = _safe_quant_error(exc)
            enriched_records.append(
                QuantFeatureRecord(
                    instrument=base.instrument,
                    analysis_date=base.analysis_date,
                    market=base.market,
                    flow=base.flow,
                    fundamentals=fundamentals_snapshot,
                )
            )
        candidates, feature_rows = score_quant_records(
            enriched_records,
            analysis_date,
            limit=limit,
            weights=quant_weights,
        )
        if not candidates:
            raise RuntimeError("quant-rank produced no candidates after DART finalist enrichment")

    selected_tickers = {candidate.instrument.ticker for candidate in candidates}
    selected_feature_rows = [
        row for row in feature_rows if str(row.get("ticker") or "") in selected_tickers
    ]
    feature_payload_hash = hashlib.sha256(
        json.dumps(
            feature_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    generated_at = _korea_now()
    # The ranking itself is an observed current-date artifact. Individual feature
    # vintages are frozen per row as market/flow/fundamental_data_as_of.
    ranking_data_as_of = analysis_date
    ranking_source = (
        f"fincept-kis-feature-quant-v1/{quant_profile}"
        f";upstream_sha256={discovery['ranking_payload_hash']}"
        f";prefilter_features_sha256={preliminary_feature_payload_hash}"
        f";finalist_features_sha256={feature_payload_hash}"
    )
    rows_by_ticker = {str(row["ticker"]): row for row in selected_feature_rows}
    ranking_rows = [
        {
            "ticker": candidate.instrument.ticker,
            "name": candidate.instrument.name,
            "market": candidate.instrument.market,
            "score": candidate.score,
            "rank": candidate.rank,
            **{
                key: value
                for key, value in rows_by_ticker[candidate.instrument.ticker].items()
                if key not in {"ticker", "name", "market", "score"}
            },
        }
        for candidate in candidates
    ]
    ranking_payload = {
        "analysis_date": analysis_date.isoformat(),
        "ranking_source": ranking_source,
        "ranking_generated_at": generated_at.isoformat(),
        "ranking_mode": "observed",
        "ranking_data_as_of": ranking_data_as_of.isoformat(),
        "limit": limit,
        "rows": ranking_rows,
    }
    (
        frozen_source,
        frozen_generated_at,
        ranking_hash,
        frozen_mode,
        frozen_data_as_of,
    ) = _ranking_provenance(ranking_payload, analysis_date)
    candidates = [
        replace(
            candidate,
            ranking_source=frozen_source,
            ranking_generated_at=frozen_generated_at,
            ranking_payload_hash=ranking_hash,
            analysis_cutoff_at=frozen_generated_at,
            analysis_cutoff_mode="external",
            ranking_mode=frozen_mode,
            ranking_data_as_of=frozen_data_as_of,
        )
        for candidate in candidates
    ]
    result = {
        "analysis_date": analysis_date,
        "source": "KIS bounded per-symbol feature ranking",
        "scoring_model": "kis-feature-quant-v1",
        "scoring_profile": quant_profile,
        "scoring_weights": quant_weights.normalized(),
        "lookback_days": lookback_days,
        "prefilter_limit": prefilter_limit,
        "prefilter_count": len(upstream_candidates),
        "feature_record_count": len(records),
        "preliminary_candidate_count": len(preliminary_candidates),
        "dart_candidate_limit": preliminary_limit if dart is not None else 0,
        "dart_enrichment_count": dart_enrichment_count,
        "market_errors": market_errors,
        "flow_errors": flow_errors,
        "fundamental_errors": fundamental_errors,
        "dart_enrichment": dart is not None,
        "upstream_discovery_source": discovery["ranking_source"],
        "upstream_discovery_hash": discovery["ranking_payload_hash"],
        "preliminary_feature_payload_hash": preliminary_feature_payload_hash,
        "feature_payload_hash": feature_payload_hash,
        "ranking_source": frozen_source,
        "ranking_generated_at": frozen_generated_at,
        "ranking_payload_hash": ranking_hash,
        "ranking_mode": frozen_mode,
        "ranking_data_as_of": frozen_data_as_of,
        "candidates": candidates,
        "ranking": ranking_payload,
        "execution_mode": "research_only",
    }
    if cache_store is not None:
        cached = cache_store.put_quant_rank_cache(
            cache_key,
            result,
            ttl_seconds=cache_ttl_seconds,
            created_at=frozen_generated_at,
        )
        result.update(
            {
                "cache_enabled": True,
                "cache_hit": False,
                "cache_key": cache_key,
                "cache_created_at": cached["created_at"],
                "cache_expires_at": cached["expires_at"],
                "cache_ttl_seconds": cache_ttl_seconds,
            }
        )
    else:
        result.update(
            {
                "cache_enabled": False,
                "cache_hit": False,
                "cache_key": cache_key,
                "cache_created_at": None,
                "cache_expires_at": None,
                "cache_ttl_seconds": 0,
            }
        )
    return result


def _quant_rank_cache_key(
    *,
    analysis_date: date,
    markets: list[str],
    limit: int,
    prefilter_limit: int,
    lookback_days: int,
    min_trading_value_krw: int,
    quant_profile: str,
    discovery_profile: str,
    dart_enabled: bool,
) -> str:
    requested = {str(market).upper().strip() for market in markets}
    canonical_markets = [
        market for market in ("KOSPI", "KOSDAQ") if not requested or market in requested
    ]
    payload = {
        "schema": "personal-kr-quant-cache-v1",
        "analysis_date": analysis_date.isoformat(),
        "markets": canonical_markets,
        "limit": int(limit),
        "prefilter_limit": int(prefilter_limit),
        "lookback_days": int(lookback_days),
        "min_trading_value_krw": int(min_trading_value_krw),
        "quant_profile": str(quant_profile),
        "discovery_profile": str(discovery_profile),
        "dart_enabled": bool(dart_enabled),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _hydrate_quant_rank_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore and verify one cached Quant Ranking result."""

    result = dict(payload)
    analysis_date = date.fromisoformat(str(result["analysis_date"]))
    ranking = result.get("ranking")
    if not isinstance(ranking, dict):
        raise ValueError("invalid cached quant ranking envelope")
    source, generated_at, payload_hash, mode, data_as_of = _ranking_provenance(
        ranking, analysis_date
    )
    if str(result.get("ranking_source") or "") != source:
        raise ValueError("cached quant ranking_source mismatch")
    if str(result.get("ranking_payload_hash") or "").lower() != payload_hash:
        raise ValueError("cached quant ranking_payload_hash mismatch")
    if str(result.get("ranking_mode") or "observed").lower() != mode:
        raise ValueError("cached quant ranking_mode mismatch")
    top_generated = datetime.fromisoformat(
        str(result["ranking_generated_at"]).replace("Z", "+00:00")
    )
    if top_generated != generated_at:
        raise ValueError("cached quant ranking_generated_at mismatch")
    top_data_as_of = date.fromisoformat(str(result["ranking_data_as_of"]))
    if top_data_as_of != data_as_of:
        raise ValueError("cached quant ranking_data_as_of mismatch")

    candidates = result.get("candidates") or []
    if not isinstance(candidates, list):
        raise ValueError("invalid cached quant candidates")
    hydrated_candidates = [_candidate_from_payload(dict(item)) for item in candidates]
    for candidate in hydrated_candidates:
        if (
            candidate.analysis_date != analysis_date
            or candidate.ranking_source != source
            or candidate.ranking_generated_at != generated_at
            or candidate.ranking_payload_hash != payload_hash
            or candidate.ranking_mode != mode
            or candidate.ranking_data_as_of != data_as_of
        ):
            raise ValueError("cached quant candidate provenance mismatch")

    result["analysis_date"] = analysis_date
    result["ranking_generated_at"] = generated_at
    result["ranking_data_as_of"] = data_as_of
    result["candidates"] = hydrated_candidates
    return result


def _safe_quant_error(exc: Exception) -> str:
    """Bound diagnostics without ever serializing provider credentials or URLs."""

    label = type(exc).__name__
    message = " ".join(str(exc).split())
    lowered = message.lower()
    if any(token in lowered for token in ("appsecret", "app_secret", "api_key", "access_token", "http://", "https://")):
        return label
    if len(message) > 180:
        message = message[:177] + "..."
    return f"{label}: {message}" if message else label


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

    return _run_batch_payload(_input_json())


def _run_batch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the immutable ranking -> bounded deep-research batch contract."""

    analysis_date = date.fromisoformat(payload["analysis_date"])
    if analysis_date > _korea_today():
        raise ValueError("analysis_date cannot be in the future")
    (
        ranking_source,
        ranking_generated_at,
        ranking_hash,
        ranking_mode,
        ranking_data_as_of,
    ) = _ranking_provenance(payload, analysis_date)
    limit = int(payload.get("limit", 5))
    if limit < 1 or limit > 10:
        raise ValueError("production batch limit must be between 1 and 10")
    candidates, input_errors = select_top_candidates_isolated(payload["rows"], analysis_date, limit)
    batch_cutoff_at = (
        ranking_generated_at
        if ranking_mode == "observed"
        else datetime.combine(analysis_date, KR_DAILY_FINALITY_TIME, tzinfo=_KST)
    )
    candidates = [
        replace(
            candidate,
            ranking_source=ranking_source,
            ranking_generated_at=ranking_generated_at,
            ranking_payload_hash=ranking_hash,
            analysis_cutoff_at=batch_cutoff_at,
            analysis_cutoff_mode="external",
            ranking_mode=ranking_mode,
            ranking_data_as_of=ranking_data_as_of,
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


def cmd_quant_research(args: argparse.Namespace) -> dict[str, Any]:
    """Current Quant Ranking -> Top-N deep research -> frozen decisions.

    The ranking is generated and consumed in one Python process. The existing
    batch implementation remains the sole deep-research/decision boundary, so
    ranking hashes, cutoff semantics, candidate isolation and first-write-wins
    persistence cannot diverge between manual two-step and one-click workflows.
    """

    request = _optional_input_json()
    llm_config = request.get("llm")
    strategy_id = str(request.get("strategy_id") or "personal-kr-quant-research")
    if not strategy_id.strip():
        raise ValueError("strategy_id cannot be blank")

    quant = cmd_quant_rank(args)
    ranking_payload = dict(quant["ranking"])
    ranking_payload["strategy_id"] = strategy_id
    if llm_config is not None:
        ranking_payload["llm"] = llm_config
    batch = _run_batch_payload(ranking_payload)
    return {
        "analysis_date": quant["analysis_date"],
        "strategy_id": strategy_id,
        "ranking_source": quant["ranking_source"],
        "ranking_generated_at": quant["ranking_generated_at"],
        "ranking_payload_hash": quant["ranking_payload_hash"],
        "ranking_mode": quant["ranking_mode"],
        "ranking_data_as_of": quant["ranking_data_as_of"],
        "scoring_profile": quant["scoring_profile"],
        "prefilter_count": quant["prefilter_count"],
        "feature_record_count": quant["feature_record_count"],
        "dart_enrichment_count": quant["dart_enrichment_count"],
        "cache_hit": quant["cache_hit"],
        "cache_key": quant["cache_key"],
        "ranking": quant["ranking"],
        "selected": batch["selected"],
        "results": batch["results"],
        "input_errors": batch["input_errors"],
        "errors": batch["errors"],
        "quant_warnings": {
            "market": quant.get("market_errors") or {},
            "flow": quant.get("flow_errors") or {},
            "fundamentals": quant.get("fundamental_errors") or {},
        },
        "completed_count": len(batch["results"]),
        "failed_count": len(batch["errors"]) + len(batch["input_errors"]),
        "failed_tickers": sorted(batch["errors"]),
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


def cmd_paper_trades(args: argparse.Namespace) -> Any:
    trades = _store().list_paper_trades(args.limit)
    return {"trades": trades, "count": len(trades), "execution_mode": "paper_only"}


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
    return {
        "trade_id": trade_id,
        "client_trade_id": client_trade_id,
        "decision_id": decision_id,
        "trade_date": trade_date.isoformat(),
        "ticker": str(payload["ticker"]),
        "side": str(payload["side"]).upper(),
        "quantity": int(payload["quantity"]),
        "price": float(payload["price"]),
        "fee": float(payload.get("fee", 0.0)),
        "tax": float(payload.get("tax", 0.0)),
        "cash_krw": cash,
        "positions": positions,
        "execution_mode": "paper_only",
    }


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
    paper_trades = sub.add_parser("paper-trades")
    paper_trades.add_argument("--limit", type=int, default=100)
    sub.add_parser("paper-trade")
    sub.add_parser("llm-smoke")
    discover = sub.add_parser("discover")
    discover.add_argument("--analysis-date")
    discover.add_argument("--market", action="append", choices=("KOSPI", "KOSDAQ"))
    discover.add_argument("--limit", type=int, default=20)
    discover.add_argument("--min-trading-value-krw", type=int, default=0)
    discover.add_argument("--profile", choices=DISCOVERY_PROFILE_NAMES, default="balanced")
    quant_rank = sub.add_parser("quant-rank")
    quant_rank.add_argument("--analysis-date")
    quant_rank.add_argument("--market", action="append", choices=("KOSPI", "KOSDAQ"))
    quant_rank.add_argument("--limit", type=int, default=10)
    quant_rank.add_argument("--prefilter-limit", type=int, default=30)
    quant_rank.add_argument("--lookback-days", type=int, default=120)
    quant_rank.add_argument("--min-trading-value-krw", type=int, default=0)
    quant_rank.add_argument("--profile", choices=QUANT_PROFILE_NAMES, default="balanced")
    quant_rank.add_argument("--discovery-profile", choices=DISCOVERY_PROFILE_NAMES, default="balanced")
    quant_rank.add_argument("--cache-ttl-seconds", type=int, default=300)
    quant_rank.add_argument("--refresh", action="store_true")
    quant_research = sub.add_parser("quant-research")
    quant_research.add_argument("--analysis-date")
    quant_research.add_argument("--market", action="append", choices=("KOSPI", "KOSDAQ"))
    quant_research.add_argument("--limit", type=int, default=5)
    quant_research.add_argument("--prefilter-limit", type=int, default=30)
    quant_research.add_argument("--lookback-days", type=int, default=120)
    quant_research.add_argument("--min-trading-value-krw", type=int, default=0)
    quant_research.add_argument("--profile", choices=QUANT_PROFILE_NAMES, default="balanced")
    quant_research.add_argument("--discovery-profile", choices=DISCOVERY_PROFILE_NAMES, default="balanced")
    quant_research.add_argument("--cache-ttl-seconds", type=int, default=300)
    quant_research.add_argument("--refresh", action="store_true")
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
        elif args.command == "paper-trades":
            _print(cmd_paper_trades(args))
        elif args.command == "paper-trade":
            _print(cmd_paper_trade())
        elif args.command == "llm-smoke":
            _print(cmd_llm_smoke())
        elif args.command == "discover":
            _print(cmd_discover(args))
        elif args.command == "quant-rank":
            _print(cmd_quant_rank(args))
        elif args.command == "quant-research":
            _print(cmd_quant_research(args))
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
