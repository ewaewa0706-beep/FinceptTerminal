from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from personal_kr.benchmark import load_yahoo_benchmark


def ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


class RecordingHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


def payload(timestamps, *, opens, highs, lows, closes, volumes=None):
    quote = {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
    }
    if volumes is not None:
        quote["volume"] = volumes
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {"quote": [quote]},
                }
            ],
            "error": None,
        }
    }


class BenchmarkContractTests(unittest.TestCase):
    def test_yahoo_contract_uses_inclusive_end_and_encoded_symbol(self):
        start = date(2026, 9, 14)
        end = date(2026, 9, 18)
        http = RecordingHttp(
            payload(
                [ts(start)],
                opens=[100],
                highs=[101],
                lows=[99],
                closes=[100.5],
                volumes=[123],
            )
        )

        bars = load_yahoo_benchmark("^KS11", start, end, http=http)

        self.assertEqual(len(bars), 1)
        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/%5EKS11"))
        self.assertEqual(kwargs["params"]["period1"], ts(start))
        self.assertEqual(kwargs["params"]["period2"], ts(date(2026, 9, 19)))
        self.assertEqual(kwargs["params"]["interval"], "1d")

    def test_null_ohlc_and_out_of_range_rows_are_filtered_without_date_shift(self):
        start = date(2026, 9, 15)
        end = date(2026, 9, 16)
        days = [date(2026, 9, 14), start, end]
        http = RecordingHttp(
            payload(
                [ts(day) for day in days],
                opens=[90, None, 110],
                highs=[91, 101, 112],
                lows=[89, 99, 109],
                closes=[90, 100, 111],
                volumes=[1, 2, 3],
            )
        )

        bars = load_yahoo_benchmark("^KQ11", start, end, http=http)

        self.assertEqual([bar.trade_date for bar in bars], [end])
        self.assertEqual(bars[0].close, 111.0)

    def test_misaligned_required_arrays_fail_closed(self):
        day = date(2026, 9, 18)
        http = RecordingHttp(
            payload(
                [ts(day), ts(day)],
                opens=[100],
                highs=[101, 101],
                lows=[99, 99],
                closes=[100, 100],
            )
        )

        with self.assertRaisesRegex(RuntimeError, "misaligned benchmark OHLC arrays"):
            load_yahoo_benchmark("^KS11", day, day, http=http)

    def test_chart_error_and_empty_result_fail_closed(self):
        day = date(2026, 9, 18)
        error_http = RecordingHttp({"chart": {"result": None, "error": {"code": "Not Found"}}})
        with self.assertRaisesRegex(RuntimeError, "Yahoo benchmark error"):
            load_yahoo_benchmark("^KS11", day, day, http=error_http)

        empty_http = RecordingHttp({"chart": {"result": [], "error": None}})
        with self.assertRaisesRegex(RuntimeError, "no benchmark rows"):
            load_yahoo_benchmark("^KS11", day, day, http=empty_http)

    def test_invalid_timestamp_fails_closed_without_realigning_prices(self):
        day = date(2026, 9, 18)
        http = RecordingHttp(
            payload(
                ["not-a-timestamp"],
                opens=[100],
                highs=[101],
                lows=[99],
                closes=[100],
            )
        )

        with self.assertRaisesRegex(RuntimeError, "invalid benchmark timestamp"):
            load_yahoo_benchmark("^KS11", day, day, http=http)


if __name__ == "__main__":
    unittest.main()
