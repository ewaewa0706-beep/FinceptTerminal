from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from email.utils import format_datetime
from datetime import datetime, timezone

from personal_kr.http import HttpResponse, HttpStatusError
from personal_kr.models import Instrument
from personal_kr.providers import DartClient, EcosClient, KisClient, NaverNewsClient, _map_dart_accounts


class KisHttp:
    def __init__(
        self,
        *,
        first_401: bool = False,
        first_expired_envelope: bool = False,
        first_rate_limit: bool = False,
    ):
        self.token_calls = 0
        self.get_calls = []
        self.first_401 = first_401
        self.first_expired_envelope = first_expired_envelope
        self.first_rate_limit = first_rate_limit
        self.failed_once = False

    def post_json(self, url, **kwargs):
        self.token_calls += 1
        self.last_token_body = kwargs.get("json_body")
        return {"access_token": f"token-{self.token_calls}"}

    def get_json(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.first_401 and not self.failed_once:
            self.failed_once = True
            raise HttpStatusError(401, "expired")
        if self.first_expired_envelope and not self.failed_once:
            self.failed_once = True
            return {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}
        if self.first_rate_limit and not self.failed_once:
            self.failed_once = True
            return {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"}
        params = kwargs.get("params") or {}
        if "inquire-daily-itemchartprice" in url:
            # Include one valid row per requested window and one impossible
            # future row; the provider must never leak the future row.
            day = params["FID_INPUT_DATE_2"]
            return {
                "rt_cd": "0",
                "output2": [
                    {
                        "stck_bsop_date": day,
                        "stck_oprc": "100",
                        "stck_hgpr": "110",
                        "stck_lwpr": "90",
                        "stck_clpr": "105",
                        "acml_vol": "1000",
                    },
                    {
                        "stck_bsop_date": "20991231",
                        "stck_oprc": "999",
                        "stck_hgpr": "999",
                        "stck_lwpr": "999",
                        "stck_clpr": "999",
                        "acml_vol": "1",
                    },
                ],
            }
        if "inquire-investor" in url:
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "stck_bsop_date": "20260916",
                        "frgn_ntby_qty": "1200",
                        "orgn_ntby_qty": "900",
                    },
                    {
                        "stck_bsop_date": "20260917",
                        "frgn_ntby_qty": "99999",
                        "orgn_ntby_qty": "99999",
                    },
                ],
            }
        raise AssertionError(url)


def corp_zip() -> bytes:
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
<result><list><corp_code>00126380</corp_code><corp_name>Samsung</corp_name>
<stock_code>005930</stock_code></list></result>"""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("CORPCODE.xml", xml)
    return out.getvalue()


class DartHttp:
    def __init__(self, *, amendment_mismatch: bool = False):
        self.amendment_mismatch = amendment_mismatch
        self.financial_params = None

    def request(self, method, url, **kwargs):
        self.corp_params = kwargs.get("params")
        return HttpResponse(200, corp_zip(), {})

    def get_json(self, url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/list.json"):
            self.list_params = params
            return {
                "status": "000",
                "list": [
                    {
                        "rcept_dt": "20260315",
                        "rcept_no": "202603150001",
                        "report_nm": "사업보고서 (2025.12)",
                    },
                    {
                        "rcept_dt": "20260320",
                        "rcept_no": "202603200001",
                        "report_nm": "[기재정정]사업보고서 (2025.12)",
                    },
                ],
            }
        if url.endswith("/fnlttSinglAcntAll.json"):
            self.financial_params = params
            receipt = "202603200001" if self.amendment_mismatch else "202603150001"
            return {
                "status": "000",
                "list": [
                    {"rcept_no": receipt, "account_nm": "매출액", "thstrm_amount": "1000000"},
                    {"rcept_no": receipt, "account_nm": "영업이익", "thstrm_amount": "100000"},
                    {"rcept_no": receipt, "account_nm": "당기순이익", "thstrm_amount": "80000"},
                    {"rcept_no": receipt, "account_nm": "자산총계", "thstrm_amount": "5000000"},
                    {"rcept_no": receipt, "account_nm": "부채총계", "thstrm_amount": "2000000"},
                    {"rcept_no": receipt, "account_nm": "자본총계", "thstrm_amount": "3000000"},
                ],
            }
        raise AssertionError(url)


class NaverHttp:
    def get_json(self, url, **kwargs):
        self.params = kwargs.get("params")
        self.headers = kwargs.get("headers")
        return {
            "items": [
                {
                    "pubDate": format_datetime(datetime(2026, 9, 15, 8, tzinfo=timezone.utc)),
                    "title": "<b>Samsung</b> today",
                    "originallink": "https://news/1",
                },
                {
                    "pubDate": format_datetime(datetime(2026, 9, 15, 9, tzinfo=timezone.utc)),
                    "title": "duplicate",
                    "originallink": "https://news/1",
                },
                {
                    "pubDate": format_datetime(datetime(2026, 9, 17, 1, tzinfo=timezone.utc)),
                    "title": "future",
                    "originallink": "https://news/2",
                },
            ]
        }


class EcosHttp:
    def get_json(self, url, **kwargs):
        if "731Y001" in url:
            raise RuntimeError("one ECOS series unavailable")
        return {
            "StatisticSearch": {
                "row": [
                    {"TIME": "20260915", "DATA_VALUE": "2.5"},
                    {"TIME": "20260917", "DATA_VALUE": "9.9"},
                ]
            }
        }


class EcosErrorHttp:
    def get_json(self, url, **kwargs):
        return {"RESULT": {"CODE": "INFO-100", "MESSAGE": "invalid api key"}}


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("005930", "삼성전자", "KOSPI")
        self.kst = timezone(timedelta(hours=9))
        self.now_kst = datetime(2026, 9, 16, 15, 0, tzinfo=self.kst)

    def test_kis_daily_contract_chunks_long_range_and_filters_future(self):
        http = KisHttp()
        client = KisClient("app", "secret", http=http, base_url="https://kis.test")
        snapshot = client.daily_bars(self.instrument, date(2026, 9, 16), lookback_days=180)

        chart_calls = [call for call in http.get_calls if "itemchartprice" in call[0]]
        self.assertGreaterEqual(len(chart_calls), 3)
        for _, kwargs in chart_calls:
            params = kwargs["params"]
            self.assertEqual(params["FID_INPUT_ISCD"], "005930")
            self.assertEqual(params["FID_PERIOD_DIV_CODE"], "D")
            self.assertEqual(params["FID_ORG_ADJ_PRC"], "1")
        self.assertTrue(all(bar.trade_date <= date(2026, 9, 16) for bar in snapshot.bars))
        self.assertEqual(snapshot.price_mode, "original")
        self.assertEqual(http.token_calls, 1)

        adjusted_http = KisHttp()
        adjusted = KisClient("app", "secret", http=adjusted_http, base_url="https://kis.test").daily_bars(
            self.instrument,
            date(2026, 9, 16),
            lookback_days=1,
            price_mode="adjusted",
        )
        adjusted_calls = [call for call in adjusted_http.get_calls if "itemchartprice" in call[0]]
        self.assertTrue(adjusted_calls)
        self.assertTrue(all(call[1]["params"]["FID_ORG_ADJ_PRC"] == "0" for call in adjusted_calls))
        self.assertEqual(adjusted.price_mode, "adjusted")

    def test_kis_401_refreshes_token_once_and_investor_flow_is_point_in_time(self):
        http = KisHttp(first_401=True)
        client = KisClient("app", "secret", http=http, base_url="https://kis.test")
        flow = client.investor_flow(self.instrument, date(2026, 9, 16))

        self.assertEqual(http.token_calls, 2)
        self.assertEqual(flow.as_of, date(2026, 9, 16))
        self.assertEqual(flow.foreign_net_buy, 1200)
        self.assertEqual(flow.institution_net_buy, 900)

    def test_kis_200_expired_envelope_refreshes_token_once(self):
        http = KisHttp(first_expired_envelope=True)
        client = KisClient("app", "secret", http=http, base_url="https://kis.test")
        flow = client.investor_flow(self.instrument, date(2026, 9, 16))
        self.assertEqual(http.token_calls, 2)
        self.assertEqual(flow.foreign_net_buy, 1200)

    def test_kis_200_rate_limit_envelope_is_bounded_retry(self):
        http = KisHttp(first_rate_limit=True)
        sleeps = []
        client = KisClient(
            "app",
            "secret",
            http=http,
            base_url="https://kis.test",
            sleep=sleeps.append,
            min_request_interval=0,
            rate_limit_wait=0.25,
        )
        flow = client.investor_flow(self.instrument, date(2026, 9, 16))
        self.assertEqual(flow.foreign_net_buy, 1200)
        self.assertEqual(len(http.get_calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_kis_token_cache_is_reused_across_client_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_http = KisHttp()
            first = KisClient(
                "app", "secret", http=first_http, base_url="https://kis.test", token_cache_dir=tmp
            )
            self.assertEqual(first._auth(), "token-1")
            self.assertEqual(first_http.token_calls, 1)

            second_http = KisHttp()
            second = KisClient(
                "app", "secret", http=second_http, base_url="https://kis.test", token_cache_dir=tmp
            )
            self.assertEqual(second._auth(), "token-1")
            self.assertEqual(second_http.token_calls, 0)

    def test_dart_uses_filing_business_year_not_receipt_year(self):
        http = DartHttp()
        client = DartClient("dart", http=http, base_url="https://dart.test/api")
        fundamentals = client.fundamentals(self.instrument, date(2026, 3, 16))

        self.assertEqual(http.list_params["end_de"], "20260316")
        self.assertEqual(http.financial_params["bsns_year"], 2025)
        self.assertEqual(http.financial_params["reprt_code"], "11011")
        self.assertEqual(fundamentals.as_of, date(2026, 3, 15))
        self.assertEqual(fundamentals.revenue, 1_000_000)

    def test_dart_rejects_later_amendment_statement(self):
        client = DartClient(
            "dart", http=DartHttp(amendment_mismatch=True), base_url="https://dart.test/api"
        )
        with self.assertRaisesRegex(RuntimeError, "later amendment"):
            client.fundamentals(self.instrument, date(2026, 3, 16))

    def test_dart_interim_income_statement_prefers_cumulative_amount(self):
        values = _map_dart_accounts(
            [
                {
                    "account_nm": "매출액",
                    "thstrm_amount": "100",
                    "thstrm_add_amount": "900",
                },
                {
                    "account_nm": "자산총계",
                    "thstrm_amount": "5000",
                    "thstrm_add_amount": "999999",
                },
            ]
        )
        self.assertEqual(values["revenue"], 900)
        self.assertEqual(values["assets"], 5000)

    def test_naver_queries_company_name_filters_future_and_dedupes(self):
        http = NaverHttp()
        client = NaverNewsClient("id", "secret", http=http, now=lambda: self.now_kst)
        items = client.news(self.instrument, date(2026, 9, 16))

        self.assertEqual(http.params["query"], "삼성전자")
        self.assertEqual(http.params["sort"], "date")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Samsung today")

    def test_naver_exact_intraday_cutoff_excludes_later_same_day_news(self):
        kst = timezone(timedelta(hours=9))

        class IntradayHttp:
            def get_json(self, url, **kwargs):
                return {
                    "items": [
                        {
                            "pubDate": format_datetime(datetime(2026, 9, 16, 9, 0, tzinfo=kst)),
                            "title": "known before cutoff",
                            "originallink": "https://news/before",
                        },
                        {
                            "pubDate": format_datetime(datetime(2026, 9, 16, 14, 0, tzinfo=kst)),
                            "title": "later same day",
                            "originallink": "https://news/after",
                        },
                    ]
                }

        client = NaverNewsClient("id", "secret", http=IntradayHttp(), now=lambda: self.now_kst)
        items = client.news(
            self.instrument,
            date(2026, 9, 16),
            cutoff_at=datetime(2026, 9, 16, 10, 0, tzinfo=kst),
        )
        self.assertEqual([item.title for item in items], ["known before cutoff"])

    def test_naver_historical_news_fails_closed_without_vintage_snapshot(self):
        class MustNotCall:
            def get_json(self, url, **kwargs):
                raise AssertionError("historical non-vintage request must fail before HTTP")

        client = NaverNewsClient("id", "secret", http=MustNotCall(), now=lambda: self.now_kst)
        with self.assertRaisesRegex(RuntimeError, "non-vintage"):
            client.news(self.instrument, date(2026, 8, 1))

    def test_naver_future_as_of_is_rejected_before_http(self):
        class MustNotCall:
            def get_json(self, url, **kwargs):
                raise AssertionError("future request must fail before HTTP")

        client = NaverNewsClient("id", "secret", http=MustNotCall(), now=lambda: self.now_kst)
        with self.assertRaisesRegex(ValueError, "future"):
            client.news(self.instrument, date(2026, 9, 17))

    def test_ecos_partial_series_failure_does_not_abort_snapshot(self):
        client = EcosClient("ecos", http=EcosHttp(), base_url="https://ecos.test")
        snapshot = client.macro(date(2026, 9, 16))

        self.assertEqual(snapshot.indicators["bok_base_rate"], 2.5)
        self.assertIsNone(snapshot.indicators["usdkrw"])
        self.assertLessEqual(snapshot.as_of, date(2026, 9, 16))

    def test_ecos_http_200_error_envelope_is_not_false_success(self):
        client = EcosClient("bad", http=EcosErrorHttp(), base_url="https://ecos.test")
        with self.assertRaisesRegex(RuntimeError, "ECOS unavailable"):
            client.macro(date(2026, 9, 16))

    def test_ecos_backdated_history_fails_closed_without_vintage_data(self):
        client = EcosClient("ecos", http=EcosHttp(), base_url="https://ecos.test")
        with self.assertRaisesRegex(RuntimeError, "non-vintage"):
            client.macro(date(2026, 9, 15))


if __name__ == "__main__":
    unittest.main()
