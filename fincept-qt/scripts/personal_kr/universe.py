"""PIT-safe Korean whole-market universe discovery.

The keyless KIS public KOSPI/KOSDAQ master files are current snapshots, not a
historical archive.  This module therefore permits live/current discovery only
for the Korean civil date and relies on immutable exact-date snapshots for any
later replay.  It never substitutes today's membership for a historical date.
"""

from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Callable, Iterable

from .http import RetryHttpClient
from .models import Instrument, QuantCandidate


KST = timezone(timedelta(hours=9))

KIS_PUBLIC_MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}

# Fixed-width tail layouts published by Korea Investment & Securities.  The
# prefix is variable-width and carries the short code/name; these offsets apply
# only to the fixed-width tail.
_KIS_MASTER_LAYOUTS = {
    "KOSPI": {
        "widths": (
            2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
            1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 9, 5, 5, 1, 1, 1, 2, 1, 1,
            1, 2, 2, 2, 3, 1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1,
            1, 9, 9, 9, 5, 9, 8, 9, 3, 1, 1, 1,
        ),
        "spac": 19,
        "reference_price": 31,
        "previous_volume": 47,
        "listed_on": 49,
        "listed_shares_thousands": 50,
        "preferred_code": 54,
        "market_cap_100m": 65,
    },
    "KOSDAQ": {
        "widths": (
            2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
            1, 1, 1, 1, 1, 1, 9, 5, 5, 1, 1, 1, 2, 1, 1, 1, 2, 2, 2, 3,
            1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1, 9, 9, 9, 5, 9,
            8, 9, 3, 1, 1, 1,
        ),
        "spac": 14,
        "reference_price": 26,
        "previous_volume": 42,
        "listed_on": 44,
        "listed_shares_thousands": 45,
        "preferred_code": 49,
        "market_cap_100m": 59,
    },
}


class CurrentUniverseOnlyError(RuntimeError):
    """Raised when current-only discovery is requested for an unsafe date."""


class KisPublicMasterError(RuntimeError):
    """Raised when the keyless KIS public master cannot be parsed safely."""


@dataclass(frozen=True)
class UniverseEntry:
    instrument: Instrument
    as_of: date
    listed_on: date | None = None
    listed_shares: int | None = None
    volume: int | None = None
    trading_value_krw: int | None = None
    market_cap_krw: int | None = None
    source: str = "KIS public master current-only"

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, date):
            object.__setattr__(self, "as_of", date.fromisoformat(str(self.as_of)))
        if self.listed_on is not None and not isinstance(self.listed_on, date):
            object.__setattr__(self, "listed_on", date.fromisoformat(str(self.listed_on)))
        for name in ("listed_shares", "volume", "trading_value_krw", "market_cap_krw"):
            value = getattr(self, name)
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} cannot be negative")
            if value is not None:
                object.__setattr__(self, name, int(value))
        if not self.source.strip():
            raise ValueError("universe source provenance is required")


@dataclass(frozen=True)
class UniverseSnapshot:
    snapshot_date: date
    markets: tuple[str, ...]
    common_only: bool
    exclude_spac: bool
    entries: tuple[UniverseEntry, ...]
    payload_sha256: str
    captured_at: datetime

    def replay_entries(self) -> list[UniverseEntry]:
        replay = (
            "PIT universe snapshot replay "
            f"(snapshot_date={self.snapshot_date.isoformat()}, "
            f"captured_at={self.captured_at.isoformat()}, "
            f"sha256={self.payload_sha256[:12]}); original="
        )
        return [replace(entry, source=f"{replay}{entry.source}") for entry in self.entries]


class KisPublicMasterClient:
    """Download the current keyless KIS KOSPI/KOSDAQ master archives in memory."""

    def __init__(self, http: RetryHttpClient | None = None) -> None:
        self.http = http or RetryHttpClient(timeout=20.0)

    def get_master_rows(self, market: str) -> list[dict[str, Any]]:
        market = str(market).upper().strip()
        if market not in KIS_PUBLIC_MASTER_URLS:
            raise ValueError(f"unsupported KIS public-master market: {market}")
        response = self.http.request("GET", KIS_PUBLIC_MASTER_URLS[market])
        try:
            with zipfile.ZipFile(BytesIO(response.body)) as archive:
                members = [name for name in archive.namelist() if name.lower().endswith(".mst")]
                if len(members) != 1:
                    raise KisPublicMasterError(
                        f"KIS public master archive for {market} contains {len(members)} .mst files"
                    )
                raw = archive.read(members[0]).decode("cp949")
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
            raise KisPublicMasterError(f"invalid KIS public master archive for {market}") from exc
        rows = [
            row
            for line in raw.splitlines()
            if (row := _parse_kis_master_line(market, line)) is not None
        ]
        if not rows:
            raise KisPublicMasterError(
                f"KIS public master for {market} contained no parseable rows; upstream schema may have changed"
            )
        return rows


class KisPublicMasterUniverseProvider:
    """Current-only, keyless KOSPI/KOSDAQ universe with a liquidity proxy."""

    SOURCE = "KIS public master current-only; liquidity=reference_price*previous_volume proxy"
    RANKED_SOURCE = "KIS current volume-rank trading value + public master current-only"

    def __init__(
        self,
        client: KisPublicMasterClient | Any | None = None,
        *,
        rank_client: Any | None = None,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self.client = client or KisPublicMasterClient()
        self.rank_client = rank_client
        self.today_fn = today_fn or (lambda: datetime.now(KST).date())
        self._cache: dict[tuple[str, date], list[dict[str, Any]]] = {}

    def get_universe(
        self,
        as_of: date,
        *,
        markets: Iterable[str] = ("KOSPI", "KOSDAQ"),
        common_only: bool = True,
        exclude_spac: bool = True,
        min_trading_value_krw: int = 0,
        limit: int | None = None,
    ) -> list[UniverseEntry]:
        today = self.today_fn()
        if as_of != today:
            raise CurrentUniverseOnlyError(
                "KIS public-master discovery is current-date only; historical discovery requires an exact stored PIT snapshot"
            )
        if min_trading_value_krw < 0:
            raise ValueError("min_trading_value_krw must be >= 0")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        market_list = _normalize_markets(markets)
        if not market_list:
            return []

        entries: list[UniverseEntry] = []
        ranked_tickers: set[str] = set()
        for market in market_list:
            current_rank_values: dict[str, int] = {}
            if self.rank_client is not None:
                try:
                    rank_rows = self.rank_client.volume_rank(market)
                except Exception:
                    rank_rows = []
                for rank_row in rank_rows:
                    ticker = _kis_rank_ticker(rank_row)
                    value = _kis_rank_trading_value(rank_row)
                    if ticker is None or value is None:
                        continue
                    current_rank_values[ticker] = value
                    ranked_tickers.add(ticker)
            cache_key = (market, today)
            if cache_key not in self._cache:
                self._cache[cache_key] = self.client.get_master_rows(market)
            rows = self._cache[cache_key]
            if not rows:
                raise KisPublicMasterError(f"KIS public master returned no rows for {market}")
            for row in rows:
                group_code = str(row.get("group_code") or "").strip().upper()
                preferred_code = str(row.get("preferred_code") or "").strip()
                if common_only and (group_code != "ST" or preferred_code not in {"", "0"}):
                    continue
                name = str(row.get("name") or "").strip()
                if exclude_spac and (str(row.get("spac") or "").upper() == "Y" or _is_spac(name)):
                    continue
                ticker = str(row.get("ticker") or "").strip()
                if len(ticker) != 6 or not ticker.isdigit() or ticker == "000000" or not name:
                    continue
                reference_price = _int_or_none(row.get("reference_price"))
                previous_volume = _int_or_none(row.get("previous_volume"))
                liquidity = (
                    reference_price * previous_volume
                    if reference_price is not None and previous_volume is not None
                    else None
                )
                ranked_value = current_rank_values.get(ticker)
                if ranked_value is not None:
                    liquidity = ranked_value
                if (liquidity or 0) < min_trading_value_krw:
                    continue
                listed_shares_thousands = _int_or_none(row.get("listed_shares_thousands"))
                market_cap_100m = _int_or_none(row.get("market_cap_100m"))
                entries.append(
                    UniverseEntry(
                        instrument=Instrument(ticker, name, market),
                        as_of=as_of,
                        listed_on=_date_or_none(row.get("listed_on")),
                        listed_shares=(
                            listed_shares_thousands * 1_000
                            if listed_shares_thousands is not None
                            else None
                        ),
                        volume=previous_volume,
                        trading_value_krw=liquidity,
                        market_cap_krw=(
                            market_cap_100m * 100_000_000 if market_cap_100m is not None else None
                        ),
                        source=self.RANKED_SOURCE if ranked_value is not None else self.SOURCE,
                    )
                )
        entries.sort(
            key=lambda item: (
                0 if item.instrument.ticker in ranked_tickers else 1,
                -(item.trading_value_krw or 0),
                -(item.market_cap_krw or 0),
                item.instrument.ticker,
            )
        )
        return entries[:limit] if limit is not None else entries


class SnapshotAwareUniverseProvider:
    """Capture today's canonical universe and replay historical dates exactly."""

    def __init__(
        self,
        current_provider: Any,
        snapshot_store: Any,
        *,
        today_fn: Callable[[], date] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.current_provider = current_provider
        self.snapshot_store = snapshot_store
        self.today_fn = today_fn or (lambda: datetime.now(KST).date())
        self.now_fn = now_fn or (lambda: datetime.now(KST))

    def get_universe(
        self,
        as_of: date,
        *,
        markets: Iterable[str] = ("KOSPI", "KOSDAQ"),
        common_only: bool = True,
        exclude_spac: bool = True,
        min_trading_value_krw: int = 0,
        limit: int | None = None,
    ) -> list[UniverseEntry]:
        market_list = _normalize_markets(markets)
        today = self.today_fn()
        if as_of > today:
            raise CurrentUniverseOnlyError("future whole-market universe discovery is not allowed")
        if min_trading_value_krw < 0:
            raise ValueError("min_trading_value_krw must be >= 0")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")

        if as_of < today:
            snapshot = self.snapshot_store.get_universe_snapshot(
                as_of,
                markets=market_list,
                common_only=common_only,
                exclude_spac=exclude_spac,
            )
            if snapshot is None:
                raise CurrentUniverseOnlyError(
                    "historical whole-market discovery requires an exact PIT universe snapshot captured on "
                    f"{as_of.isoformat()}"
                )
            return _apply_threshold_and_limit(
                snapshot.replay_entries(), min_trading_value_krw=min_trading_value_krw, limit=limit
            )

        existing = self.snapshot_store.get_universe_snapshot(
            as_of,
            markets=market_list,
            common_only=common_only,
            exclude_spac=exclude_spac,
        )
        if existing is not None:
            return _apply_threshold_and_limit(
                existing.replay_entries(), min_trading_value_krw=min_trading_value_krw, limit=limit
            )

        # Capture the full canonical current-day universe before any request-
        # specific threshold or limit.  Persistence is first-write-wins, and the
        # returned snapshot becomes the current run's source too.
        entries = self.current_provider.get_universe(
            as_of,
            markets=market_list,
            common_only=common_only,
            exclude_spac=exclude_spac,
            min_trading_value_krw=0,
            limit=None,
        )
        if entries:
            snapshot = self.snapshot_store.record_universe_snapshot(
                snapshot_date=as_of,
                markets=market_list,
                entries=entries,
                common_only=common_only,
                exclude_spac=exclude_spac,
                captured_at=self.now_fn(),
            )
            entries = list(snapshot.entries)
        return _apply_threshold_and_limit(
            entries, min_trading_value_krw=min_trading_value_krw, limit=limit
        )


def liquidity_candidates(
    entries: Iterable[UniverseEntry], analysis_date: date, *, limit: int = 20
) -> list[QuantCandidate]:
    """Convert a liquidity-sorted universe into a deterministic 0..100 shortlist."""

    if limit < 1:
        raise ValueError("limit must be >= 1")
    ordered = list(entries)
    if not ordered:
        return []
    total = len(ordered)
    candidates: list[QuantCandidate] = []
    for index, entry in enumerate(ordered[:limit], start=1):
        percentile = 100.0 * (total - index + 1) / total
        factors = {
            "trading_value_krw": float(entry.trading_value_krw or 0),
            "market_cap_krw": float(entry.market_cap_krw or 0),
            "previous_volume": float(entry.volume or 0),
        }
        if not all(math.isfinite(value) for value in factors.values()):
            raise ValueError("universe ranking factors must be finite")
        candidates.append(
            QuantCandidate(
                instrument=entry.instrument,
                analysis_date=analysis_date,
                score=percentile,
                rank=index,
                factors=factors,
            )
        )
    return candidates


def _apply_threshold_and_limit(
    entries: Iterable[UniverseEntry], *, min_trading_value_krw: int, limit: int | None
) -> list[UniverseEntry]:
    filtered = [entry for entry in entries if (entry.trading_value_krw or 0) >= min_trading_value_krw]
    return filtered[:limit] if limit is not None else filtered


def _normalize_markets(markets: Iterable[str]) -> tuple[str, ...]:
    requested = set(str(market).upper().strip() for market in markets)
    unsupported = sorted(market for market in requested if market not in KIS_PUBLIC_MASTER_URLS)
    if unsupported:
        raise ValueError(f"unsupported universe markets: {unsupported}")
    return tuple(market for market in ("KOSPI", "KOSDAQ") if market in requested)


def _parse_kis_master_line(market: str, line: str) -> dict[str, Any] | None:
    market = str(market).upper().strip()
    layout = _KIS_MASTER_LAYOUTS[market]
    widths = layout["widths"]
    tail_width = sum(widths)
    if len(line) <= tail_width + 21:
        return None
    prefix = line[:-tail_width]
    ticker_field = prefix[:9].strip()
    ticker = ticker_field[-6:] if len(ticker_field) >= 6 else ticker_field
    name = prefix[21:].strip()
    tail = line[-tail_width:]
    values = _split_fixed_width(tail, widths)
    return {
        "ticker": ticker,
        "name": name,
        "group_code": values[0],
        "spac": values[layout["spac"]],
        "reference_price": values[layout["reference_price"]],
        "previous_volume": values[layout["previous_volume"]],
        "listed_on": values[layout["listed_on"]],
        "listed_shares_thousands": values[layout["listed_shares_thousands"]],
        "preferred_code": values[layout["preferred_code"]],
        "market_cap_100m": values[layout["market_cap_100m"]],
    }


def _split_fixed_width(text: str, widths: Iterable[int]) -> list[str]:
    values: list[str] = []
    offset = 0
    for width in widths:
        values.append(text[offset : offset + width].strip())
        offset += width
    return values


def _kis_rank_ticker(row: dict[str, Any]) -> str | None:
    for key in ("mksc_shrn_iscd", "stck_shrn_iscd"):
        ticker = str(row.get(key) or "").strip()
        if len(ticker) == 6 and ticker.isdigit() and ticker != "000000":
            return ticker
    return None


def _kis_rank_trading_value(row: dict[str, Any]) -> int | None:
    direct = _int_or_none(row.get("acml_tr_pbmn"))
    if direct is not None and direct > 0:
        return direct
    price = _int_or_none(row.get("stck_prpr"))
    volume = _int_or_none(row.get("acml_vol"))
    if price is None or volume is None or price <= 0 or volume <= 0:
        return None
    return price * volume


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _date_or_none(value: Any) -> date | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _is_spac(name: str) -> bool:
    normalized = "".join(str(name).upper().split())
    return "스팩" in normalized or "SPAC" in normalized
