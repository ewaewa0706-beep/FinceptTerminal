# Personal Korean-market AI Research Terminal

This Fincept extension is a separate, on-demand Korean-market research path. It
does not turn the LLM into a whole-market screener and it does not submit live
brokerage orders.

## Workflow

```text
KIS Public Master Universe / External Quant Ranking
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
- `KRX_AUTH_KEY`
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

KRX follows the same managed-credential path: Settings stores `KRX_AUTH_KEY`
through `SecureStorage`, and `PythonRunner` injects it into the Personal-KR
subprocess environment. The status command reports only whether a key is
configured; it never returns the key value.

For local/headless development, KRX also supports the git-ignored file
`fincept-qt/scripts/KRX_KEY.local.txt`. Put exactly one authentication-key line
in that file with no `KRX_AUTH_KEY=` prefix. A managed/process `KRX_AUTH_KEY`
takes precedence. `KRX_AUTH_KEY_FILE` can point to an alternate local file.
Presence does not prove service authorization: KRX may still return HTTP 401
until the requested API service is approved for that key.

Whole-market discovery itself does not require a KIS API credential. The
current KOSPI/KOSDAQ membership is loaded from KIS public master archives.
Because those archives are current snapshots rather than historical data,
Personal-KR freezes the full eligible universe once per Korean calendar date
before request-specific filtering. Historical discovery first replays an exact
immutable snapshot captured on that date; when no such snapshot exists and KRX
is configured, it can use the explicitly labeled KRX historical-reconstruction
path described below. Today's master is never backfilled into a past analysis
date.

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
python personal_kr_terminal.py discover --limit 20 --min-trading-value-krw 1000000000
python personal_kr_terminal.py quant-rank --prefilter-limit 30 --limit 10 --profile balanced
python personal_kr_terminal.py quant-rank --prefilter-limit 30 --limit 10 --refresh
python personal_kr_terminal.py quant-research --prefilter-limit 30 --limit 5 --profile balanced
```

Without stdin, `llm-smoke` uses the headless `GOOGLE_API_KEY` fallback. The
desktop/MCP path instead sends the currently active Fincept LLM profile over
stdin, so OpenAI/Anthropic/Gemini/OpenRouter/Fincept and the other configured
providers are smoke-tested with the same provider/model used by deep research.
Credentials are never placed on argv and are not returned in the smoke result.

`discover` returns a deterministic Top-N cross-sectional shortlist plus a normal
Fincept ranking envelope (`ranking_source`, timezone-aware
`ranking_generated_at`, canonical payload hash and flat ranking rows). Without
KIS credentials the score uses the public master's `reference_price ×
previous_volume` as a clearly labeled previous-day liquidity proxy. When KIS
credentials are configured, positive current trading-value-rank values overlay
matching public-master members; zero pre-market values and rank-endpoint errors
fall back to the proxy. The v2 scorer then percentile-normalizes four fields over
the same frozen eligible universe: trading-value liquidity (50%), market-cap
size (25%), trading-value/market-cap turnover (15%), and share volume (10%).
Missing factors are omitted and their weights are renormalized instead of being
treated as zero. This is a deterministic prefilter/discovery signal, not an LLM
whole-market scan. The returned ranking envelope can be sent through the existing
production `batch` path, which applies the same immutable ranking provenance and
`research_only` safeguards as an external quant ranking.

The UI/CLI also expose four deterministic v2 profiles without changing the
underlying snapshot: `balanced` (50/25/15/10), `liquidity` (70/15/5/10),
`large_cap` (35/50/5/10), and `active` (35/10/35/20) for
liquidity/size/turnover/volume respectively. The selected profile is embedded in
`ranking_source`, so decisions produced from different profiles cannot silently
share the same ranking provenance.

The Qt/MCP discovery controls can additionally restrict the market to `KOSPI` or
`KOSDAQ` and apply a minimum trading-value threshold. The desktop expresses the
threshold in 억원 (KRW 100 million units) while the CLI/MCP contract uses exact
KRW. These filters are applied only after the canonical daily snapshot is frozen,
so changing a screen does not rewrite PIT membership or provenance.

The desktop date picker defaults to the current Korean civil date and never
allows a future date. Selecting an older date first replays an exact universe
snapshot genuinely captured on that date. If no exact snapshot exists and
`KRX_AUTH_KEY` is configured, discovery can reconstruct the historical universe
from dated KRX OpenAPI base-info and daily-trading rows. Reconstruction is never
stored as a backdated snapshot: `ranking_mode=historical_reconstruction`,
`ranking_data_as_of` preserves the resolved exchange session, and
`ranking_generated_at` / `reconstructed_at` preserve the actual later retrieval
time. Without either an exact snapshot or KRX access, historical discovery fails
closed rather than substituting current market membership.

`quant-rank` is the bounded feature-ranking stage between broad discovery and
deep research. It takes at most 50 names from the cheap whole-market prefilter,
then uses KIS daily bars and per-stock investor flow only for that slice. When
`DART_API_KEY` is configured, a KIS-only preliminary rank first narrows the set
to at most 15 finalists and only those names receive DART filing/statement
requests. DART then adds PIT-safe financial-statement factors (operating margin,
net margin and equity ratio) before the final Top-N rerank. The v1 score cross-sectionally
combines momentum (20/60-session returns), foreign and institution flow relative
to aligned share volume, DART fundamentals, 20-session trading-value liquidity,
and inverse 20-session annualized volatility. Profiles are `balanced`, `momentum`,
`flow`, and `defensive`; missing optional flow or DART data is omitted and the
remaining weights are renormalized. Raw feature metrics are frozen into a
separate SHA-256 audit hash, while the selected Top-N rows use the normal batch
ranking hash/provenance contract. This stage never calls an LLM and never places
an order. The 15-name DART cap prevents optional enrichment from turning a
30-50 name market prefilter into an unbounded disclosure-API fan-out.

Repeated identical `quant-rank` requests use a short SQLite cache by default
(`--cache-ttl-seconds 300`). The cache key includes analysis date, market scope,
Top-N/prefilter limits, lookback, liquidity floor, Quant/discovery profiles and
whether DART enrichment is enabled. A cache hit reuses the original ranking
envelope and its original `ranking_generated_at`/hash rather than pretending the
cache-read time is new PIT evidence. Use `--refresh` to bypass the read cache, or
`--cache-ttl-seconds 0` to disable caching. Expired or malformed cache rows are
discarded automatically and fresh provider calls are made. Cache hydration also
rebuilds selected candidates from the frozen ranking rows and rejects the entry
if the duplicated display candidates disagree with that ranking.

Quant Ranking v1 is intentionally current-date only. The KIS per-stock investor
flow quote used here does not accept an arbitrary historical date, so an old
analysis date is rejected rather than treating today's response as historical
PIT evidence. Historical whole-market work remains on exact snapshot replay or
the explicitly labeled KRX reconstruction path described above.

`quant-research` is the single-process production convenience path for the same
current-date flow: `discover -> quant-rank -> Top-N -> deep research -> frozen
decisions`. It does not introduce a second ranking or research implementation.
The command calls the same Quant Ranking function and feeds its exact ranking
envelope into the same bounded `batch` function, so ranking hash/timestamp,
cutoff semantics, candidate isolation and first-write-wins decision persistence
match the manual two-step workflow. The desktop exposes this as **RUN QUANT + AI
TOP-N**, and MCP exposes `kr_quant_research`. Completed candidates are persisted
one by one; a later candidate failure is returned in `errors` without erasing
earlier decisions. The pipeline remains `research_only` and never submits an
order. Re-running the same strategy/ticker/date with the same candidate ranking
provenance, explicit LLM provider/model/execution fingerprint and workflow version reuses the existing
immutable Decision instead of repeating KIS/DART/Naver/LLM calls. Any provenance
or LLM mismatch fails as a decision conflict before expensive provider/LLM work.
The execution fingerprint hashes only non-secret behavior/routing identity
(provider, model, sanitized endpoint and the effective token cap where that
provider sends one); API keys, session tokens, URL credentials and query strings
are never persisted in the fingerprint input record.
Each one-click execution also receives a fresh `run_id` in the SQLite
`kr_research_runs` ledger. The run record stores only non-secret orchestration
metadata, the frozen ranking reference, selected tickers, resulting decision ids,
reused tickers and isolated errors. If the outer process is interrupted, decisions
already checkpointed remain immutable. The run ledger is checkpointed after every
reused, stored or failed candidate as well, so an interrupted execution preserves
the latest completed decision refs and errors before a later resume marks the old
run partial. The desktop sends `resume=true`; when the
resume lookup runs, SQLite filters directly by run type, strategy, analysis date
and running status instead of scanning only the latest global run list. The
latest interrupted run has the same strategy/date/market/limits/scoring profiles,
DART mode and LLM provider/model fingerprint, its exact frozen ranking envelope is
reused even if the short Quant cache has already expired. A new audit run is then
created with `resumed_from_run_id`, completed decisions are reused, and only the
unfinished names consume provider/LLM work. MCP exposes the same behavior through
an explicit `resume` boolean, while ordinary CLI/MCP calls default to a fresh run
to avoid treating a concurrently running process as interrupted. The desktop consumes dedicated
`FINCEPT_KR_PROGRESS` stderr events and shows Quant preparation plus each Top-N
`n/N` checking/analyzing/reused/checkpointed/error state while the final stdout
remains the single authoritative JSON result.
The desktop and MCP one-click entry points use the same
`personal-kr-quant-research` strategy identity, so immutable Decision reuse and
interrupted-run resume remain consistent when the same workflow is continued
from either surface. A resumed result is labeled as a resumed frozen ranking in
the desktop instead of being reported as a newly generated ranking.
When the Analysis tab opens, the desktop also calls the lightweight `status`
command and surfaces current KIS Quant, KRX historical reconstruction and active
LLM readiness directly in the KR discovery panel.

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
python personal_kr_terminal.py paper-trades --limit 100
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

The same Analysis tab also exposes an always-visible **KR MARKET DISCOVERY**
panel. **DISCOVER KR TOP-N** loads the current KOSPI/KOSDAQ public-master
universe, freezes/reuses the PIT snapshot, and renders the deterministic Top-N
shortlist without invoking the LLM or any order path. If KIS credentials are
configured, positive current trading-value-rank rows are overlaid on the public
master membership; KIS rank failure or zero pre-market values fall back to the
previous-day public-master liquidity proxy.

After discovery, **RESEARCH SELECTED** is a separate explicit action for one
selected row. It forwards that candidate's frozen ranking source, generation
time, payload hash and analysis cutoff into the normal `analyze` path with the
active Fincept LLM profile. The resulting decision is persisted under the
`personal-kr-discovery-ui` strategy and remains `research_only`; discovery never
auto-starts deep research and neither action routes to a live order.

**RESEARCH TOP-N (MAX 10)** is the explicit batch counterpart. It forwards the
same frozen discovery ranking envelope to the existing production `batch`
command, preserving the hard maximum of 10 deep-research names, shared KIS
token/client reuse, per-candidate failure isolation and immediate decision
checkpointing. The UI uses a finite 60-minute outer watchdog and reports partial
successes/errors; the batch remains `research_only` and never submits an order.

The Analysis tab also exposes **KR RESEARCH HISTORY**. **REFRESH DECISIONS**
loads the latest frozen Personal-KR decision rows, **EVALUATE 1/5/20/60D** runs
the existing point-in-time forward-return and benchmark-alpha evaluator for the
selected decision, and **SHOW OUTCOMES** reads only already frozen outcome rows.
**PAPER SUMMARY** is read-only and requires the Python response to declare
`execution_mode=paper_only`.

The same history panel now has an explicit **RECORD PAPER TRADE** action for the
selected frozen decision. The user must manually choose BUY/SELL, quantity,
execution price, fee and tax, then accept a confirmation dialog that states the
action is paper-only. The request is sent as JSON on stdin to `paper-trade`; no
order data is routed to Fincept live-broker order code. A generated
`client_trade_id` is retained across an ambiguous child-process failure so an
exact retry reuses the same idempotency key instead of creating a duplicate.
Structured rejections clear that pending key, while successful responses must
declare `execution_mode=paper_only`. Paper execution is never inferred from an
AI signal and is never triggered by discovery, research, outcome evaluation or
paper-summary refresh.

An ambiguous paper request also survives decision-row selection changes. The UI
does not mint a replacement idempotency key merely because the selected row
changed. **PAPER TRADES** reconciles the retained `client_trade_id` against the
immutable local SQLite ledger after the original child process has finished. If
that exact row is present, the pending state is cleared as committed; if a
successful ledger read proves it absent, the pending state is also cleared as
not committed, making it safe to enter a corrected/new paper trade. A failed
ledger read never clears the pending key.

**PAPER TRADES** is the read-only ledger companion. It lists recent immutable
paper rows newest-first with `client_trade_id`, `decision_id`, ticker/company,
side, quantity, price, fees/tax, strategy and frozen research signal. CLI and MCP
expose the same data through `paper-trades` / `kr_paper_trade_log`; neither path
can submit or alter an order.

Internal MCP/agent tools:

- `kr_research_status`
- `kr_discover_market`
- `kr_llm_smoke`
- `kr_select_top_candidates`
- `kr_research_batch`
- `kr_analyze_stock`
- `kr_decision_log`
- `kr_evaluate_outcome`
- `kr_outcome_log`
- `kr_provider_smoke`
- `kr_paper_summary`
- `kr_paper_trade_log`
- `kr_paper_trade`

`kr_paper_trade` is marked destructive/confirmation-required even though it is
simulation-only. Paper execution is therefore an explicit user action rather
than a side effect of research.

## Point-in-time and failure behavior

- ordinary observed rankings freeze timezone-aware `ranking_generated_at` as
  the exact analysis cutoff for production batch research. KRX historical
  reconstruction instead records the later retrieval time honestly and uses
  `ranking_data_as_of` plus the requested date's conservative finality boundary
  as the research cutoff;
- manual/UI/MCP research for today's market also freezes a timezone-aware exact
  request timestamp as `analysis_cutoff_at`. Immediate requests are tagged
  `analysis_cutoff_mode=live_request`; explicit older/external timestamps are
  treated as strict external PIT cutoffs;
- keyless whole-market membership is current-only. The full current KIS public
  master universe is captured first-write-wins before liquidity thresholds or
  Top-N slicing. Historical requests prefer that exact same-date snapshot; when
  it is absent, an authenticated KRX OpenAPI reconstruction is allowed without
  pretending the data was captured historically. If neither source is
  available, discovery fails closed;
- market bars newer than the analysis cutoff are rejected. KIS daily price and
  investor-flow endpoints expose date-level data without a finality timestamp;
  before the conservative 17:00 KST daily-finality boundary the same calendar
  day's KIS rows are therefore excluded and the prior date is used. The extra
  buffer also covers exchange-designated delayed-close sessions;
- historical KIS research bars request **original/unadjusted prices**
  (`FID_ORG_ADJ_PRC=1`). Later corporate actions can restate adjusted history,
  so adjusted prices are not used as frozen research evidence;
- DART uses filing receipt date for availability and resolves the statement
  business year from the report period, preventing the common annual-report
  receipt-year error. Because the used DART interfaces do not provide a receipt
  time, an exact intraday batch cutoff conservatively excludes same-day filings;
- a later DART amendment cannot overwrite an earlier point-in-time filing;
- DART filing and financial-statement rows must carry the same nonblank
  `rcept_no`; missing or mixed receipt provenance fails the enrichment closed
  instead of attaching unprovenanced account values to a frozen decision;
- Naver search is current-index/non-vintage. Today-only on-demand search is
  allowed, articles later than the exact batch cutoff are filtered and duplicates
  are removed. If the API's maximum `start=1000` search window is exhausted
  before enough pre-cutoff results can be reached, the enrichment fails closed
  as incomplete. Historical analysis likewise fails closed instead of pretending
  the current search index is a historical snapshot;
- ECOS responses are non-vintage for this workflow. Historical/external exact
  PIT analysis fails the macro enrichment closed because the current-series
  response cannot prove what was visible at that old instant. A `live_request`
  may use the ECOS values actually observed during that run; those values and
  any per-series errors are then frozen in decision evidence. ECOS
  `StatisticSearch` rows are paged through the declared `list_total_count` before
  the latest observation is selected, avoiding stale first-page snapshots;
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
- forward outcomes only admit a current-day daily endpoint after 17:00 KST;
  before that cutoff the latest finalized prior session is used so an intraday
  partial KIS/Yahoo daily bar cannot become an immutable outcome;
- legacy outcome rows that predate source/price-mode/timestamp/input-hash
  provenance are moved to `kr_outcome_quarantine` during schema upgrade, freeing
  their decision/horizon key for a newly audited evaluation while preserving the
  old payload for inspection;
- legacy paper rows without required provenance, with duplicate idempotency
  keys, or with invalid ledger fields are quarantined to
  `kr_paper_trade_quarantine` during schema upgrade rather than being counted in
  the active ledger. The strict-table rebuild is transactional and recovers the
  temporary table left by an interrupted older migration.

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
cd "C:\Users\User\web gpt\fincept-terminal-personal\fincept-qt"
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

cd "C:\Users\User\web gpt\fincept-terminal-personal\fincept-qt"
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

Pushes to the `personal-kr-terminal` branch that touch `fincept-qt/**` also run
`.github/workflows/build-pr.yml`: a release-style native Qt matrix on Windows,
Linux and macOS followed by the application's headless self-tests, plus the
all-screens smoke walk on Linux. This is the preferred native verification path
when the development machine itself does not have the Qt/CMake/MSVC toolchain
installed.

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
