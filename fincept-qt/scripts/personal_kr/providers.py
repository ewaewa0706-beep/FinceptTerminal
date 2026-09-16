"""Korean-market provider adapters (KIS, DART, Naver News, ECOS)."""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

from .http import HttpStatusError, RetryHttpClient
from .models import (
    FundamentalSnapshot,
    Instrument,
    InvestorFlowSnapshot,
    MacroSnapshot,
    MarketSnapshot,
    NewsItem,
    OHLCVBar,
)


def _float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean_html(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


class KisClient:
    BASE = "https://openapi.koreainvestment.com:9443"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        http: RetryHttpClient | None = None,
        base_url: str | None = None,
        token_cache_dir: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_request_interval: float = 0.1,
        rate_limit_wait: float = 61.0,
        rate_limit_retries: int = 3,
    ) -> None:
        if not app_key or not app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET are required")
        self.app_key = app_key
        self.app_secret = app_secret
        supplied_http = http is not None
        self.http = http or RetryHttpClient()
        self.base_url = (base_url or self.BASE).rstrip("/")
        self._token: str | None = None
        self._sleep = sleep
        self._min_request_interval = max(float(min_request_interval), 0.0)
        self._rate_limit_wait = max(float(rate_limit_wait), 0.0)
        self._rate_limit_retries = max(int(rate_limit_retries), 1)
        self._last_request_monotonic = 0.0
        # Production subprocesses get FINCEPT_DATA_DIR from PythonRunner. Keep
        # real KIS tokens across Python processes so repeated Top-N runs do not
        # hit KIS's token-issuance throttle. Test doubles do not touch the user's
        # filesystem unless a cache directory is explicitly supplied.
        if token_cache_dir is not None:
            self._token_cache_dir = Path(token_cache_dir)
        elif not supplied_http:
            data_dir = Path(os.getenv("FINCEPT_DATA_DIR") or Path.home() / ".fincept")
            self._token_cache_dir = data_dir / "personal_kr"
        else:
            self._token_cache_dir = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "KisClient":
        return cls(os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", ""), **kwargs)

    def _auth(self, *, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        cached = self._read_cached_token()
        if cached and not force:
            self._token = cached
            return cached

        lock_path: Path | None = None
        lock_fd: int | None = None
        try:
            if self._token_cache_dir is not None:
                lock_path, lock_fd = self._acquire_token_lock()
                # Another process may have refreshed while we waited. On an
                # explicit refresh only accept a token different from the one
                # this process just saw fail.
                refreshed = self._read_cached_token()
                if refreshed and (not force or refreshed != self._token):
                    self._token = refreshed
                    return refreshed

            payload = self.http.post_json(
                f"{self.base_url}/oauth2/tokenP",
                json_body={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
            )
            token = payload.get("access_token")
            if not token:
                raise RuntimeError("KIS token response did not contain access_token")
            self._token = str(token)
            self._write_cached_token(self._token, payload.get("access_token_token_expired"))
            return self._token
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if lock_path is not None:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _token_paths(self) -> tuple[Path, Path] | None:
        if self._token_cache_dir is None:
            return None
        account_hash = hashlib.sha256(self.app_key.encode("utf-8")).hexdigest()[:16]
        return (
            self._token_cache_dir / f".kis_token_{account_hash}.json",
            self._token_cache_dir / f".kis_token_{account_hash}.lock",
        )

    def _read_cached_token(self) -> str | None:
        paths = self._token_paths()
        if paths is None:
            return None
        token_path, _ = paths
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(str(data["expires_at"]))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc) + timedelta(minutes=5):
                return None
            token = str(data.get("access_token") or "")
            return token or None
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _write_cached_token(self, token: str, raw_expiry: Any) -> None:
        paths = self._token_paths()
        if paths is None:
            return
        token_path, _ = paths
        self._token_cache_dir.mkdir(parents=True, exist_ok=True)
        expiry: datetime
        try:
            # KIS examples document this field as Korea-local
            # "YYYY-mm-dd HH:MM:SS" and a one-day token lifetime.
            expiry = datetime.strptime(str(raw_expiry), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=9))
            ).astimezone(timezone.utc)
        except (TypeError, ValueError):
            expiry = datetime.now(timezone.utc) + timedelta(hours=23)
        tmp = token_path.with_suffix(token_path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"access_token": token, "expires_at": expiry.isoformat()}, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, token_path)

    def _acquire_token_lock(self) -> tuple[Path, int]:
        paths = self._token_paths()
        if paths is None:
            raise RuntimeError("KIS token cache is disabled")
        _, lock_path = paths
        self._token_cache_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode("ascii"))
                return lock_path, fd
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 60:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for KIS token cache lock")
                time.sleep(0.1)

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._auth()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        # KIS can signal an expired access token either as HTTP 401 or as a
        # normal HTTP 200 envelope with msg_cd=EGW00123.  Refresh exactly once
        # for either shape; otherwise a stale token gets laundered into a generic
        # RuntimeError and the caller never recovers.
        auth_refreshed = False
        rate_attempt = 0
        while True:
            headers = self._headers(tr_id)
            self._pace_request()
            try:
                payload = self.http.get_json(f"{self.base_url}{path}", headers=headers, params=params)
            except HttpStatusError as exc:
                if exc.status != 401 or auth_refreshed:
                    raise
                self._auth(force=True)
                auth_refreshed = True
                continue
            msg_cd = str(payload.get("msg_cd") or "")
            if str(payload.get("rt_cd", "0")) in {"0", ""}:
                return payload
            if msg_cd == "EGW00123" and not auth_refreshed:
                self._auth(force=True)
                auth_refreshed = True
                continue
            if msg_cd == "EGW00201" and rate_attempt < self._rate_limit_retries - 1:
                rate_attempt += 1
                self._sleep(self._rate_limit_wait)
                continue
            raise RuntimeError(f"KIS error: {payload.get('msg1') or msg_cd}")

    def _pace_request(self) -> None:
        if self._min_request_interval <= 0:
            self._last_request_monotonic = time.monotonic()
            return
        now = time.monotonic()
        remaining = self._min_request_interval - (now - self._last_request_monotonic)
        if self._last_request_monotonic and remaining > 0:
            self._sleep(remaining)
        self._last_request_monotonic = time.monotonic()

    def daily_bars(self, instrument: Instrument, as_of: date, lookback_days: int = 120) -> MarketSnapshot:
        start = date.fromordinal(max(1, as_of.toordinal() - lookback_days))
        bars: list[OHLCVBar] = []
        # KIS can cap the number of daily rows returned from one request. Split
        # the requested history into bounded calendar windows so long lookbacks
        # (and 60-session outcome checks) never silently truncate at that cap.
        window_start = start
        while window_start <= as_of:
            window_end = min(as_of, window_start + timedelta(days=79))
            payload = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": instrument.ticker,
                    "FID_INPUT_DATE_1": window_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": window_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    # Strict historical research uses original prices. KIS
                    # adjusted history can be restated by later corporate
                    # actions, which would leak information into old PIT runs.
                    "FID_ORG_ADJ_PRC": "1",
                },
            )
            rows = payload.get("output2") or []
            for row in rows:
                day = str(row.get("stck_bsop_date", ""))
                if len(day) != 8:
                    continue
                trade_date = datetime.strptime(day, "%Y%m%d").date()
                if trade_date < start or trade_date > as_of:
                    continue
                values = [
                    _float(row.get("stck_oprc")),
                    _float(row.get("stck_hgpr")),
                    _float(row.get("stck_lwpr")),
                    _float(row.get("stck_clpr")),
                ]
                if any(value is None for value in values):
                    continue
                bars.append(
                    OHLCVBar(
                        trade_date=trade_date,
                        open=values[0] or 0,
                        high=values[1] or 0,
                        low=values[2] or 0,
                        close=values[3] or 0,
                        volume=int(_float(row.get("acml_vol")) or 0),
                    )
                )
            window_start = window_end + timedelta(days=1)
        if not bars:
            raise RuntimeError(f"KIS returned no daily bars for {instrument.ticker}")
        deduped = {bar.trade_date: bar for bar in bars}
        return MarketSnapshot(instrument, as_of, tuple(deduped[day] for day in sorted(deduped)), "KIS")

    def investor_flow(self, instrument: Instrument, as_of: date) -> InvestorFlowSnapshot:
        payload = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": instrument.ticker,
            },
        )
        rows = payload.get("output") or payload.get("output1") or []
        selected: dict[str, Any] | None = None
        selected_date: date | None = None
        for row in rows:
            raw = str(row.get("stck_bsop_date") or row.get("bsop_date") or "")
            if len(raw) != 8:
                continue
            row_date = datetime.strptime(raw, "%Y%m%d").date()
            if row_date <= as_of and (selected_date is None or row_date > selected_date):
                selected = row
                selected_date = row_date
        if selected is None or selected_date is None:
            raise RuntimeError(f"KIS returned no investor flow for {instrument.ticker}")
        foreign = _float(
            selected.get("frgn_ntby_qty")
            or selected.get("frgn_ntby_tr_pbmn")
            or selected.get("frgn_ntby_qty_icdc")
        )
        institution = _float(
            selected.get("orgn_ntby_qty")
            or selected.get("orgn_ntby_tr_pbmn")
            or selected.get("orgn_ntby_qty_icdc")
        )
        return InvestorFlowSnapshot(selected_date, "KIS", foreign, institution)


class DartClient:
    BASE = "https://opendart.fss.or.kr/api"
    PERIODIC_REPORT_CODES = ("11011", "11012", "11013", "11014")

    def __init__(self, api_key: str, *, http: RetryHttpClient | None = None, base_url: str | None = None) -> None:
        if not api_key:
            raise ValueError("DART_API_KEY is required")
        self.api_key = api_key
        self.http = http or RetryHttpClient()
        self.base_url = (base_url or self.BASE).rstrip("/")
        self._corp_codes: dict[str, str] | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "DartClient":
        return cls(os.getenv("DART_API_KEY", ""), **kwargs)

    def _check(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status", "000"))
        if status not in {"000", "013"}:  # 013 = no data
            raise RuntimeError(f"DART error {status}: {payload.get('message', '')}")
        return payload

    def corp_code_for(self, ticker: str) -> str:
        if self._corp_codes is None:
            response = self.http.request(
                "GET", f"{self.base_url}/corpCode.xml", params={"crtfc_key": self.api_key}
            )
            with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                data = archive.read("CORPCODE.xml")
            root = ElementTree.fromstring(data)
            mapping: dict[str, str] = {}
            for item in root.findall("list"):
                stock_code = (item.findtext("stock_code") or "").strip()
                corp_code = (item.findtext("corp_code") or "").strip()
                if stock_code and corp_code:
                    mapping[stock_code] = corp_code
            self._corp_codes = mapping
        corp = self._corp_codes.get(ticker)
        if not corp:
            raise RuntimeError(f"DART corp code not found for {ticker}")
        return corp

    def _latest_periodic_filing(self, corp_code: str, as_of: date) -> dict[str, Any]:
        begin = date(as_of.year - 2, 1, 1)
        payload = self._check(
            self.http.get_json(
                f"{self.base_url}/list.json",
                params={
                    "crtfc_key": self.api_key,
                    "corp_code": corp_code,
                    "bgn_de": begin.strftime("%Y%m%d"),
                    "end_de": as_of.strftime("%Y%m%d"),
                    "pblntf_ty": "A",
                    "page_count": 100,
                },
            )
        )
        candidates = []
        for row in payload.get("list") or []:
            raw_date = str(row.get("rcept_dt", ""))
            if len(raw_date) != 8:
                continue
            receipt_date = datetime.strptime(raw_date, "%Y%m%d").date()
            if receipt_date > as_of:
                continue
            report_name = str(row.get("report_nm", ""))
            report_code = _report_code_from_name(report_name)
            if report_code:
                candidates.append((receipt_date, report_code, row))
        if not candidates:
            raise RuntimeError("no periodic DART filing available at analysis date")
        _, report_code, row = max(candidates, key=lambda item: (item[0], str(item[2].get("rcept_no", ""))))
        return {**row, "reprt_code": report_code}

    def fundamentals(self, instrument: Instrument, as_of: date) -> FundamentalSnapshot:
        corp_code = self.corp_code_for(instrument.ticker)
        filing = self._latest_periodic_filing(corp_code, as_of)
        receipt_date = datetime.strptime(str(filing["rcept_dt"]), "%Y%m%d").date()
        year = _business_year_from_filing(str(filing.get("report_nm", "")), filing["reprt_code"], receipt_date)
        report_code = filing["reprt_code"]
        selected_receipt = str(filing.get("rcept_no") or "")
        rows: list[dict[str, Any]] = []
        for fs_div in ("CFS", "OFS"):
            payload = self._check(
                self.http.get_json(
                    f"{self.base_url}/fnlttSinglAcntAll.json",
                    params={
                        "crtfc_key": self.api_key,
                        "corp_code": corp_code,
                        "bsns_year": year,
                        "reprt_code": report_code,
                        "fs_div": fs_div,
                    },
                )
            )
            rows = list(payload.get("list") or [])
            if rows:
                break
        if not rows:
            raise RuntimeError("DART returned no financial statement rows")
        row_receipts = {str(row.get("rcept_no")) for row in rows if row.get("rcept_no")}
        if row_receipts and selected_receipt and row_receipts != {selected_receipt}:
            raise RuntimeError("DART financial statement is from a later amendment")
        values = _map_dart_accounts(rows)
        return FundamentalSnapshot(
            as_of=receipt_date,
            source="DART",
            filing_receipt_no=selected_receipt or None,
            **values,
        )


def _report_code_from_name(name: str) -> str | None:
    if "사업보고서" in name:
        return "11011"
    if "반기보고서" in name:
        return "11012"
    if "분기보고서" in name:
        # DART codes: 11013=Q1, 11014=Q3. Infer from common '(2026.03)'/'(2026.09)' marker.
        if ".03" in name or "03)" in name:
            return "11013"
        if ".09" in name or "09)" in name:
            return "11014"
    return None


def _business_year_from_filing(report_name: str, report_code: str, receipt_date: date) -> int:
    """Resolve DART ``bsns_year`` from the filing period, not receipt year.

    Annual reports for fiscal year N are normally filed in N+1, so blindly
    using ``rcept_dt`` makes the financial-statement request one year too new.
    DART report names usually carry a ``(YYYY.MM)`` period; when absent, the
    annual-report fallback uses the prior year while interim reports use the
    receipt year.
    """

    match = re.search(r"\((20\d{2})[.\-/](\d{1,2})\)", report_name)
    if match:
        return int(match.group(1))
    if report_code == "11011":
        return receipt_date.year - 1
    return receipt_date.year


def _map_dart_accounts(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    aliases = {
        "revenue": ("매출액", "영업수익", "수익(매출액)"),
        "operating_profit": ("영업이익", "영업이익(손실)"),
        "net_income": ("당기순이익", "당기순이익(손실)", "연결당기순이익"),
        "assets": ("자산총계",),
        "liabilities": ("부채총계",),
        "equity": ("자본총계",),
    }
    result: dict[str, float | None] = {key: None for key in aliases}
    for row in rows:
        name = str(row.get("account_nm", "")).replace(" ", "")
        for key, names in aliases.items():
            if result[key] is not None or not any(alias.replace(" ", "") in name for alias in names):
                continue
            # For interim IS/CIS rows OpenDART exposes the current-period amount
            # and, separately, the year-to-date cumulative amount.  Research
            # comparisons need the cumulative revenue/profit figures when they
            # are available; balance-sheet values remain point-in-time amounts.
            cumulative_keys = {"revenue", "operating_profit", "net_income"}
            raw = row.get("thstrm_add_amount") if key in cumulative_keys else None
            value = _float(raw)
            if value is None:
                value = _float(row.get("thstrm_amount"))
            if value is not None:
                result[key] = value
    return result


class NaverNewsClient:
    URL = "https://openapi.naver.com/v1/search/news.json"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: RetryHttpClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or RetryHttpClient()
        self._now = now or (lambda: datetime.now(timezone(timedelta(hours=9))))

    @classmethod
    def from_env(cls, **kwargs: Any) -> "NaverNewsClient":
        return cls(os.getenv("NAVER_CLIENT_ID", ""), os.getenv("NAVER_CLIENT_SECRET", ""), **kwargs)

    def news(self, instrument: Instrument, as_of: date, count: int = 20) -> tuple[NewsItem, ...]:
        kst = timezone(timedelta(hours=9))
        now_kst = self._now().astimezone(kst)
        if as_of < now_kst.date():
            raise RuntimeError("Naver historical news is non-vintage; point-in-time history unavailable")
        if as_of > now_kst.date():
            raise ValueError("Naver news as_of cannot be in the future")
        cutoff = now_kst
        display = min(max(count, 1), 100)
        result: list[NewsItem] = []
        seen: set[str] = set()
        start = 1
        while start <= 1000 and len(result) < count:
            payload = self.http.get_json(
                self.URL,
                headers={
                    "X-Naver-Client-Id": self.client_id,
                    "X-Naver-Client-Secret": self.client_secret,
                },
                params={
                    "query": instrument.name,
                    "display": display,
                    "start": start,
                    "sort": "date",
                },
            )
            rows = payload.get("items") or []
            if not rows:
                break
            for row in rows:
                raw = str(row.get("pubDate", ""))
                try:
                    published = parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    continue
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                published = published.astimezone(kst)
                if published > cutoff:
                    continue
                link = str(row.get("originallink") or row.get("link") or "")
                key = link or _clean_html(str(row.get("title", "")))
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append(
                    NewsItem(
                        published_at=published,
                        title=_clean_html(str(row.get("title", ""))),
                        link=link,
                    )
                )
                if len(result) >= count:
                    break
            if len(result) >= count or len(rows) < display:
                break
            start += display
        return tuple(result[:count])


class EcosClient:
    BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
    DEFAULT_SERIES = {
        "bok_base_rate": ("722Y001", "D", "0101000"),
        "usdkrw": ("731Y001", "D", "0000001"),
    }

    def __init__(self, api_key: str, *, http: RetryHttpClient | None = None, base_url: str | None = None) -> None:
        if not api_key:
            raise ValueError("ECOS_API_KEY is required")
        self.api_key = api_key
        self.http = http or RetryHttpClient()
        self.base_url = (base_url or self.BASE).rstrip("/")

    @classmethod
    def from_env(cls, **kwargs: Any) -> "EcosClient":
        return cls(os.getenv("ECOS_API_KEY", ""), **kwargs)

    def _series(self, stat: str, cycle: str, item: str, as_of: date) -> tuple[date, float] | None:
        start = date(as_of.year - 1, 1, 1)
        if cycle == "D":
            start_s, end_s = start.strftime("%Y%m%d"), as_of.strftime("%Y%m%d")
        elif cycle == "M":
            start_s, end_s = start.strftime("%Y%m"), as_of.strftime("%Y%m")
        else:
            start_s, end_s = str(start.year), str(as_of.year)
        url = f"{self.base_url}/{self.api_key}/json/kr/1/100/{stat}/{cycle}/{start_s}/{end_s}/{item}"
        payload = self.http.get_json(url)
        if not isinstance(payload, dict):
            raise RuntimeError("ECOS returned a non-object response")
        top_result = payload.get("RESULT") or {}
        if isinstance(top_result, dict) and top_result:
            code = str(top_result.get("CODE") or "")
            if code == "INFO-200":
                return None
            if code and code != "INFO-000":
                raise RuntimeError(f"ECOS error {code}: {top_result.get('MESSAGE') or ''}".strip())
        search = payload.get("StatisticSearch") or {}
        inner_result = search.get("RESULT") or {} if isinstance(search, dict) else {}
        if isinstance(inner_result, dict) and inner_result:
            code = str(inner_result.get("CODE") or "")
            if code == "INFO-200":
                return None
            if code and code != "INFO-000":
                raise RuntimeError(f"ECOS error {code}: {inner_result.get('MESSAGE') or ''}".strip())
        rows = (search.get("row") or []) if isinstance(search, dict) else []
        best: tuple[date, float] | None = None
        for row in rows:
            raw = str(row.get("TIME", ""))
            try:
                if len(raw) == 8:
                    obs_date = datetime.strptime(raw, "%Y%m%d").date()
                elif len(raw) == 6:
                    obs_date = datetime.strptime(raw + "01", "%Y%m%d").date()
                else:
                    obs_date = date(int(raw), 1, 1)
            except (TypeError, ValueError):
                continue
            value = _float(row.get("DATA_VALUE"))
            if value is None or obs_date > as_of:
                continue
            if best is None or obs_date > best[0]:
                best = (obs_date, value)
        return best

    def macro(self, as_of: date) -> MacroSnapshot:
        korea_today = datetime.now(timezone(timedelta(hours=9))).date()
        if as_of < korea_today:
            raise RuntimeError("ECOS historical macro is non-vintage; point-in-time history unavailable")
        if as_of > korea_today:
            raise ValueError("ECOS as_of cannot be in the future")
        indicators: dict[str, float | None] = {}
        observed_dates: list[date] = []
        errors: dict[str, str] = {}
        for name, (stat, cycle, item) in self.DEFAULT_SERIES.items():
            try:
                point = self._series(stat, cycle, item, as_of)
            except Exception as exc:
                point = None
                errors[name] = str(exc)
            indicators[name] = point[1] if point else None
            if point:
                observed_dates.append(point[0])
        if errors and len(errors) == len(self.DEFAULT_SERIES):
            detail = "; ".join(f"{name}: {message}" for name, message in errors.items())
            raise RuntimeError(f"ECOS unavailable: {detail}")
        snapshot_date = max(observed_dates) if observed_dates else as_of
        return MacroSnapshot(snapshot_date, "ECOS", indicators)
