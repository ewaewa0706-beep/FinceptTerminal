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
    if not isinstance(quotes, dict):
        raise RuntimeError(f"malformed benchmark OHLC rows for {symbol}")
    required_series = {name: quotes.get(name) or [] for name in ("open", "high", "low", "close")}
    if any(not isinstance(values, list) for values in required_series.values()):
        raise RuntimeError(f"malformed benchmark OHLC arrays for {symbol}")
    if any(len(values) != len(timestamps) for values in required_series.values()):
        raise RuntimeError(f"misaligned benchmark OHLC arrays for {symbol}")
    volumes = quotes.get("volume") or []
    if not isinstance(volumes, list):
        raise RuntimeError(f"malformed benchmark volume array for {symbol}")
    bars: list[OHLCVBar] = []
    for index, timestamp in enumerate(timestamps):
        open_ = required_series["open"][index]
        high = required_series["high"][index]
        low = required_series["low"][index]
        close = required_series["close"][index]
        volume = volumes[index] if index < len(volumes) else 0
        if None in (open_, high, low, close):
            continue
        try:
            trade_date = datetime.fromtimestamp(int(timestamp), timezone.utc).date()
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise RuntimeError(f"invalid benchmark timestamp for {symbol}") from exc
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
