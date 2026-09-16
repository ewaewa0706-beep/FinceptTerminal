# Personal Korean-market AI Research Terminal

This Fincept extension is a separate, on-demand Korean-market research path. It
does not turn the LLM into a whole-market screener and it does not submit live
brokerage orders.

## Workflow

```text
External Quant Ranking
        ↓
Deterministic Top N
        ↓
KIS market + foreign/institution flow   [core]
        ├── DART fundamentals/disclosures [optional enrichment]
        ├── Naver News                    [optional enrichment]
        └── ECOS macro                    [optional enrichment]
        ↓
Market / Fundamentals / News-Macro Analysts
        ↓
Bull ↔ Bear
        ↓
Research Manager → Trader → Risk Manager → Portfolio Manager
        ↓
Frozen Decision Log
        ↓
KIS + KOSPI/KOSDAQ benchmark Outcome / Alpha
        ↓
Personal-KR Paper Portfolio (explicit user action only)
```

The refresh mode is `on_demand`. The execution mode of this path is always
`research_only`. The KR paper ledger is an isolated simulation path with
explicit confirmation, cash/oversell checks and required decision/idempotency
provenance.
The research engine never submits a live-broker order itself.

## Credentials

Open **Settings → API Credentials** and configure the providers you use:

- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- `DART_API_KEY`
- `NAVER_CLIENT_ID`
- `NAVER_CLIENT_SECRET`
- `ECOS_API_KEY`
- `GOOGLE_API_KEY`

Fincept stores these through `SecureStorage`. `PythonRunner` injects only the
managed credential allow-list into child processes; secrets are not passed on
the command line. KIS and the LLM are required for a full deep-research run.
DART, Naver and ECOS are enrichment providers and degrade independently when
unavailable.

KIS access tokens are cached under `FINCEPT_DATA_DIR` with an app-key-derived
filename, expiry check and a small cross-process issuance lock. The cache is
never used as a substitute for credential validation; expired/invalid tokens are
refreshed through the normal KIS flow.

The status command reports **credential presence**, not proof that an external
API accepts the key. Use provider/full smoke commands to verify live readiness.

## Headless commands

Run from `fincept-qt/scripts`.

```powershell
python personal_kr_terminal.py status
python personal_kr_terminal.py llm-smoke
```

External Quant Ranking → Top N selection uses JSON over stdin:

```powershell
$ranking = @'
{
  "analysis_date": "2026-08-20",
  "limit": 2,
  "rows": [
    {"ticker":"005930","name":"삼성전자","market":"KOSPI","score":92.5,"technical":90,"flow":95,"fundamental":88},
    {"ticker":"000660","name":"SK하이닉스","market":"KOSPI","score":89.0,"technical":91,"flow":84,"fundamental":86}
  ]
}
'@
$ranking | python personal_kr_terminal.py select
```

The production batch path additionally requires immutable ranking provenance:

```powershell
$batch = @'
{
  "analysis_date": "2026-09-16",
  "ranking_source": "my-quant-v3",
  "ranking_generated_at": "2026-09-16T08:55:00+09:00",
  "limit": 2,
  "rows": [
    {"ticker":"005930","name":"삼성전자","market":"KOSPI","score":92.5,"technical":90,"flow":95,"fundamental":88},
    {"ticker":"000660","name":"SK하이닉스","market":"KOSPI","score":89.0,"technical":91,"flow":84,"fundamental":86}
  ]
}
'@
$batch | python personal_kr_terminal.py batch
```

The batch freezes a SHA-256 hash of the canonical ranking payload. Re-running
the same strategy/ticker/date with different ranking source, generation time or
payload hash is rejected as a provenance conflict for that candidate instead of
silently mixing two ranking runs. Production deep-research batches are capped at
10 names. Each completed candidate is persisted immediately before the next name
starts, so a later candidate failure or outer watchdog timeout does not erase
already completed research.

Live Korean data only, without LLM analysis:

```powershell
python personal_kr_terminal.py providers-only --ticker 005930 --name 삼성전자 --market KOSPI
```

Full Samsung Electronics research:

```powershell
python personal_kr_terminal.py full --ticker 005930 --name 삼성전자 --market KOSPI
```

List frozen decisions and evaluate forward outcomes using server-side data:

```powershell
python personal_kr_terminal.py decisions --limit 20
python personal_kr_terminal.py evaluate <decision-id> --horizons 1 5 20 60
python personal_kr_terminal.py outcomes <decision-id>
python personal_kr_terminal.py paper-summary
```

Paper trades take JSON over stdin. Both `decision_id` and `client_trade_id` are
required. Exact `client_trade_id` replay is idempotent; a conflicting replay is
rejected:

```powershell
'{"decision_id":"<decision-id>","client_trade_id":"demo-1","ticker":"005930","side":"BUY","quantity":1,"price":70000}' |
  python personal_kr_terminal.py paper-trade
```

`evaluate` does not accept caller-supplied OHLCV. Stock bars are fetched from
KIS, and `^KS11` / `^KQ11` benchmark bars are fetched server-side from Yahoo
Finance's chart endpoint before raw return, benchmark return and alpha are
persisted. The stock horizon is counted on stock trading sessions, never by
dropping dates that are missing from the benchmark. Missing benchmark horizon
endpoints fail closed. Horizons that do not yet have enough future stock sessions
are returned in `pending_horizons` rather than written as failures. Each frozen
outcome stores the stock/benchmark source, price mode, evaluation version,
`evaluated_at`, and canonical SHA-256 fingerprints of both price inputs. A later
replay with different immutable provenance is surfaced as a provenance conflict.
KIS outcome stock bars use adjusted-price history, and that exact price mode is
frozen in outcome provenance. This prevents an in-horizon split/reverse-split
from appearing as a fictitious investment gain or loss. Dividend-aware total
return accounting remains a separate enhancement.

## Fincept UI and MCP

For Korean symbols (`005930.KS`, `247540.KQ`, or a six-digit listing code), the
existing **Equity Research → Analysis** tab shows **RUN KR AI DEEP RESEARCH**.
The result is displayed in the same tab and remains research-only.

Internal MCP/agent tools:

- `kr_research_status`
- `kr_select_top_candidates`
- `kr_research_batch`
- `kr_analyze_stock`
- `kr_decision_log`
- `kr_evaluate_outcome`
- `kr_outcome_log`
- `kr_provider_smoke`
- `kr_paper_summary`
- `kr_paper_trade`

`kr_paper_trade` is marked destructive/confirmation-required even though it is
simulation-only. Paper execution is therefore an explicit user action rather
than a side effect of research.

## Point-in-time and failure behavior

- the timezone-aware external `ranking_generated_at` is frozen as the exact
  analysis cutoff for production batch research;
- manual/UI/MCP research for today's market also freezes a timezone-aware exact
  request timestamp as `analysis_cutoff_at`. Immediate requests are tagged
  `analysis_cutoff_mode=live_request`; explicit older/external timestamps are
  treated as strict external PIT cutoffs;
- market bars newer than the analysis cutoff are rejected. KIS daily price and
  investor-flow endpoints expose date-level data without a finality timestamp;
  before 16:00 KST the same calendar day's KIS rows are therefore excluded and
  the prior date is used conservatively;
- historical KIS research bars request **original/unadjusted prices**
  (`FID_ORG_ADJ_PRC=1`). Later corporate actions can restate adjusted history,
  so adjusted prices are not used as frozen research evidence;
- DART uses filing receipt date for availability and resolves the statement
  business year from the report period, preventing the common annual-report
  receipt-year error. Because the used DART interfaces do not provide a receipt
  time, an exact intraday batch cutoff conservatively excludes same-day filings;
- a later DART amendment cannot overwrite an earlier point-in-time filing;
- Naver search is current-index/non-vintage. Today-only on-demand search is
  allowed, articles later than the exact batch cutoff are filtered and duplicates
  are removed. Historical analysis fails this enrichment closed instead of
  pretending the current search index is a historical snapshot;
- ECOS responses are non-vintage for this workflow. Historical/external exact
  PIT analysis fails the macro enrichment closed because the current-series
  response cannot prove what was visible at that old instant. A `live_request`
  may use the ECOS values actually observed during that run; those values and
  any per-series errors are then frozen in decision evidence;
- KIS HTTP 401 refreshes authentication once; 429/5xx/timeouts are bounded
  retries;
- KIS daily history is split into bounded date windows to avoid silent provider
  row caps on long lookbacks;
- KIS market data is the core dependency; DART/Naver/ECOS failures are reported
  as unavailable enrichments rather than discarding an otherwise valid result.
  Sanitized failure reasons are frozen in `unavailable_reasons` without URLs or
  credential/token values;
- ECOS partial-series failures are retained in `macro.series_errors`, so a
  missing observation is distinguishable from a failed series request. Obvious
  programming/schema exceptions in optional providers fail the candidate rather
  than being silently frozen as ordinary partial-data availability;
- the final nonblank Portfolio Manager line must be exactly `SIGNAL: BUY`,
  `SIGNAL: HOLD`, or `SIGNAL: SELL`. Missing markers or trailing prose fail closed;
- ranking rows used by the production batch require an explicit Korean company
  name and `KOSPI`/`KOSDAQ` market instead of silently defaulting either field;
- one failed Top-N candidate does not discard successful candidates, and each
  success is checkpointed before analysis proceeds to the next candidate;
- repeated decision/outcome writes are first-write-wins only when immutable
  provenance matches; conflicting ranking, exact cutoff/mode, LLM/workflow, or
  frozen evidence fingerprint / outcome input provenance is rejected;
- legacy paper rows without required decision/client provenance are quarantined
  to `kr_paper_trade_quarantine` during schema upgrade rather than being counted
  in the active ledger.

When launched through the Fincept desktop UI/MCP bridge, single-stock KR research
has a finite 20-minute outer watchdog and Top-N batch research has a finite
60-minute outer watchdog. Provider-only MCP calls keep the shorter provider
budget. These values are intentionally long enough for the nine sequential LLM
stages while still preventing permanently wedged Python subprocess slots. Direct
command-line invocation of `personal_kr_terminal.py` does not add a second
process-level watchdog; use the Fincept UI/MCP path for managed orchestration.

## Windows portability / another server

The repository includes two safe PowerShell helpers under `fincept-qt/scripts`:

```powershell
cd C:\webgpt\fincept\fincept-qt
powershell -ExecutionPolicy Bypass -File .\scripts\windows_dev_doctor.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\validate_personal_kr.ps1
```

`windows_dev_doctor.ps1` is diagnostic only; it does not install or modify the
toolchain. For a native Windows build, prepare at least:

- Visual Studio 2022 / MSVC 19.40+ (VS 17.10+)
- CMake 3.27+
- Ninja
- Qt 6.8.x (CI pins 6.8.3)
- vcpkg with OpenSSL/zlib dependencies
- Python 3.11 for the managed Fincept Python environment

Typical native commands on a prepared machine:

```powershell
$env:QT_DIR='C:\Qt\6.8.3\msvc2022_64'
$env:VCPKG_ROOT='C:\vcpkg'
$env:OPENSSL_ROOT_DIR="$env:VCPKG_ROOT\installed\x64-windows"

cd C:\webgpt\fincept\fincept-qt
cmake --preset win-dev `
  -DFINCEPT_BUILD_TESTS=ON `
  -DCMAKE_PREFIX_PATH="$env:QT_DIR" `
  -DOPENSSL_ROOT_DIR="$env:OPENSSL_ROOT_DIR"
cmake --build --preset win-dev --parallel 4
ctest --test-dir build\win-dev --output-on-failure
```

The lightweight GitHub Actions workflow `.github/workflows/personal-kr-python.yml`
runs the Personal KR regression/syntax/status gate on Ubuntu 24.04 and Windows
Server 2022 with Python 3.11 and 3.12, independently from the much heavier native
Qt workflows. It requires no provider secrets and makes no live API calls.

## Tests

From `fincept-qt/scripts`:

```powershell
python -m unittest discover -s personal_kr\tests -v
python -m compileall -q personal_kr personal_kr_terminal.py
```

The test suite includes provider request contracts, point-in-time leakage
guards, retry/auth refresh, partial-data behavior, external ranking → Top N,
the nine-stage analyst chain, decision persistence, benchmark alpha, paper-ledger
safety and static Fincept C++/MCP/settings wiring checks.
