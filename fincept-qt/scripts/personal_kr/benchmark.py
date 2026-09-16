"""Server-side benchmark history for audited KR outcome evaluation."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote

from .http import RetryHttpClient
from .models import OHLCVBar


def load_yahoo_benchmark(
    symbol: str,
    start: date,
    end: date,
    *,
    http: RetryHttpClient | None = None,
) -> tuple[OHLCVBar, ...]:
    """Load KOSPI/KOSDAQ index bars from Yahoo's chart endpoint.

    Caller-supplied bars are intentionally not accepted by the audited outcome
    path; otherwise arbitrary fixture data could become a permanent track record.
    This uses the small JSON chart endpoint directly so outcome evaluation does
    not depend on pandas/yfinance being installed in the selected Python runtime.
    """

    client = http or RetryHttpClient(attempts=3, timeout=20.0, backoff=0.5)
    period1 = int(datetime.combine(start, time.min, timezone.utc).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), time.min, timezone.utc).timestamp())
    payload = client.get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
        headers={"User-Agent": "Mozilla/5.0 FinceptTerminal/PersonalKR"},
        params={
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        },
    )
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo benchmark error for {symbol}: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"no benchmark rows for {symbol}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_rows = ((result.get("indicators") or {}).get("quote") or [])
    if not quote_rows:
        raise RuntimeError(f"no benchmark OHLC rows for {symbol}")
    quotes = quote_rows[0]
    bars: list[OHLCVBar] = []
    for index, timestamp in enumerate(timestamps):
        try:
            open_ = quotes.get("open", [])[index]
            high = quotes.get("high", [])[index]
            low = quotes.get("low", [])[index]
            close = quotes.get("close", [])[index]
            volume = quotes.get("volume", [])[index]
        except IndexError:
            continue
        if None in (open_, high, low, close):
            continue
        trade_date = datetime.fromtimestamp(int(timestamp), timezone.utc).date()
        if trade_date < start or trade_date > end:
            continue
        bars.append(
            OHLCVBar(
                trade_date=trade_date,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=max(0, int(float(volume or 0))),
            )
        )
    if not bars:
        raise RuntimeError(f"no usable benchmark rows for {symbol}")
    return tuple(bars)
