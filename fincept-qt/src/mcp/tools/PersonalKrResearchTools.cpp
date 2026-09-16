// PersonalKrResearchTools.cpp — Fincept-native bridge to the personal KR engine.
//
// The Python package owns provider/PIT/research orchestration.  These MCP tools
// only validate compact inputs and invoke it through PythonRunner using stdin,
// keeping API credentials out of argv/process listings.

#include "mcp/tools/PersonalKrResearchTools.h"

#include "core/logging/Logger.h"
#include "mcp/AsyncDispatch.h"
#include "mcp/ToolSchemaBuilder.h"
#include "python/PythonRunner.h"
#include "services/equity/PersonalKrLlmConfig.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPromise>

#include <memory>

namespace fincept::mcp::tools {

namespace {

static constexpr const char* TAG = "PersonalKrResearchTools";
static constexpr int kStatusTimeoutMs = 15000;
static constexpr int kProviderTimeoutMs = 5 * 60 * 1000;
// One stock performs nine sequential LLM stages; Top-N batches can perform
// dozens. Keep finite watchdogs, but budget them separately so healthy research
// is not killed by the generic five-minute subprocess limit.
static constexpr int kSingleResearchTimeoutMs = 20 * 60 * 1000;
static constexpr int kBatchResearchTimeoutMs = 60 * 60 * 1000;

ToolResult parse_kr_envelope(const python::PythonResult& result) {
    if (!result.success)
        return ToolResult::fail("Personal KR script failed: " + result.error);

    const QString json_text = python::extract_json(result.output);
    const QJsonDocument doc = QJsonDocument::fromJson(json_text.toUtf8());
    if (!doc.isObject())
        return ToolResult::fail("Personal KR script returned invalid JSON");

    const QJsonObject envelope = doc.object();
    if (!envelope.value("success").toBool(false)) {
        const QString error = envelope.value("error").toString("Personal KR request failed");
        return ToolResult::fail(error);
    }
    return ToolResult::ok_data(envelope.value("data"));
}

void run_kr_tool(const QStringList& args, const QJsonObject& stdin_payload, ToolContext ctx,
                 const std::shared_ptr<QPromise<ToolResult>>& promise) {
    auto* runner = &python::PythonRunner::instance();
    if (!runner->is_available()) {
        promise->addResult(ToolResult::fail("Python is not available — run Fincept setup first"));
        promise->finish();
        return;
    }

    const QByteArray stdin_data = stdin_payload.isEmpty()
                                      ? QByteArray{}
                                      : QJsonDocument(stdin_payload).toJson(QJsonDocument::Compact);
    AsyncDispatch::callback_to_promise(runner, ctx, promise,
                                       [runner, args, stdin_data, ctx](auto resolve) {
                                           python::PythonRunner::RunOptions opts;
                                           opts.timeout_ms = ctx.timeout_ms;
                                           opts.stdin_data = stdin_data;
                                           runner->run_with_options(
                                               "personal_kr_terminal.py", args, opts,
                                               [resolve, ctx](python::PythonResult result) {
                                                   if (ctx.cancelled()) {
                                                       resolve(ToolResult::fail("cancelled"));
                                                       return;
                                                   }
                                                   resolve(parse_kr_envelope(result));
                                               },
                                               AsyncDispatch::line_progress_bridge(ctx));
                                       });
}

QJsonObject candidate_payload(const QJsonObject& args) {
    QJsonObject instrument{
        {"ticker", args.value("ticker")},
        {"name", args.value("company_name")},
        {"market", args.value("market")},
        {"currency", "KRW"},
    };
    QJsonObject payload{
        {"instrument", instrument},
        {"analysis_date", args.value("analysis_date")},
        {"score", args.value("score")},
        {"rank", args.value("rank")},
        {"strategy_id", "personal-kr-single"},
    };
    if (args.value("factors").isObject())
        payload["factors"] = args.value("factors").toObject();
    const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
    if (!llm.isEmpty())
        payload["llm"] = llm;
    return payload;
}

} // namespace

std::vector<ToolDef> get_personal_kr_research_tools() {
    std::vector<ToolDef> tools;

    // ── kr_research_status ──────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_research_status";
        t.description = "Check readiness of the personal Korean-market research engine. Returns research_only "
                        "execution mode and boolean readiness for KIS, DART, Naver, ECOS, and the active Fincept "
                        "LLM profile. Never returns secret values.";
        t.category = "equity-research";
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject&, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            QStringList script_args{"status"};
            const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
            const QString provider = llm.value("provider").toString();
            if (!provider.isEmpty())
                script_args << "--llm-provider" << provider;
            run_kr_tool(script_args, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_select_top_candidates ────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_select_top_candidates";
        t.description = "Convert an external Korean-stock quant ranking into a deterministic Top-N shortlist. "
                        "This is the intended universe-discovery boundary: the LLM does not scan the full market.";
        t.category = "equity-research";
        t.input_schema =
            ToolSchemaBuilder()
                .string("analysis_date", "Point-in-time analysis date in YYYY-MM-DD format")
                .required()
                .pattern("^\\d{4}-\\d{2}-\\d{2}$")
                .array("rows", "External quant ranking rows (ticker/name/market/score plus numeric factors)",
                       QJsonObject{{"type", "object"}})
                .required()
                .integer("limit", "Maximum deep-analysis candidates")
                .default_int(5)
                .between(1, 50)
                .build();
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            if (!args.value("rows").isArray() || args.value("rows").toArray().isEmpty()) {
                promise->addResult(ToolResult::fail("rows must contain at least one quant candidate"));
                promise->finish();
                return;
            }
            QJsonObject payload{
                {"analysis_date", args.value("analysis_date")},
                {"rows", args.value("rows")},
                {"limit", args.value("limit").toInt(5)},
                {"strategy_id", "personal-kr-quant"},
            };
            run_kr_tool({"select"}, payload, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_analyze_stock ────────────────────────────────────────────────
    // ── kr_research_batch ───────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_research_batch";
        t.description = "Run the production Korean-market research path in one process: external Quant Ranking -> "
                        "deterministic Top-N -> KIS/DART/Naver/ECOS -> Korean analyst chain -> frozen decisions. "
                        "One KIS client/token is reused across the batch; malformed ranking rows are returned as "
                        "input_errors and one failed deep-analysis candidate is isolated from the others. Research "
                        "only; no paper or live order is submitted.";
        t.category = "equity-research";
        t.input_schema =
            ToolSchemaBuilder()
                .string("analysis_date", "Point-in-time analysis date YYYY-MM-DD")
                .required()
                .pattern("^\\d{4}-\\d{2}-\\d{2}$")
                .array("rows", "External quant ranking rows", QJsonObject{{"type", "object"}})
                .required()
                .string("ranking_source", "Name/version of the external Quant ranking source")
                .required()
                .length(1, 160)
                .string("ranking_generated_at", "Timezone-aware ISO-8601 timestamp when the ranking was generated")
                .required()
                .length(20, 64)
                .integer("limit", "Maximum deep-analysis candidates")
                .default_int(5)
                .between(1, 10)
                .build();
        t.default_timeout_ms = kBatchResearchTimeoutMs;
        t.supports_async = true;
        t.auth_required = AuthLevel::Authenticated;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            if (!args.value("rows").isArray() || args.value("rows").toArray().isEmpty()) {
                promise->addResult(ToolResult::fail("rows must contain at least one quant candidate"));
                promise->finish();
                return;
            }
            QJsonObject payload{
                {"analysis_date", args.value("analysis_date")},
                {"rows", args.value("rows")},
                {"ranking_source", args.value("ranking_source")},
                {"ranking_generated_at", args.value("ranking_generated_at")},
                {"limit", args.value("limit").toInt(5)},
            };
            const QJsonObject llm = fincept::services::equity::personal_kr_active_llm_config();
            if (!llm.isEmpty())
                payload["llm"] = llm;
            run_kr_tool({"batch"}, payload, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_analyze_stock ────────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_analyze_stock";
        t.description = "Run on-demand deep research for one Top-N Korean stock using KIS market/foreign-institution "
                        "flow, optional DART/Naver/ECOS enrichments, Korean analysts, Bull/Bear debate, Research, "
                        "Trader, Risk and Portfolio stages. Persists a decision journal. Research only; does not "
                        "submit brokerage orders.";
        t.category = "equity-research";
        t.input_schema =
            ToolSchemaBuilder()
                .string("ticker", "Six-digit Korean listing code, e.g. 005930")
                .required()
                .pattern("^(?!000000)\\d{6}$")
                .string("company_name", "Korean company name, e.g. 삼성전자")
                .required()
                .length(1, 120)
                .string("market", "Listing market")
                .required()
                .enums({"KOSPI", "KOSDAQ"})
                .string("analysis_date", "Point-in-time analysis date YYYY-MM-DD")
                .required()
                .pattern("^\\d{4}-\\d{2}-\\d{2}$")
                .number("score", "Quant ranking score carried into deep research")
                .default_num(0.0)
                .integer("rank", "Rank within the external Top-N shortlist")
                .default_int(1)
                .min(1)
                .object("factors", "Optional numeric quant factor map")
                .build();
        t.default_timeout_ms = kSingleResearchTimeoutMs;
        t.supports_async = true;
        t.auth_required = AuthLevel::Authenticated;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            run_kr_tool({"analyze"}, candidate_payload(args), ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_decision_log ────────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_decision_log";
        t.description = "List frozen personal-KR research decisions from the local decision journal. "
                        "Repeated analysis of the same strategy/ticker/date is first-write-wins.";
        t.category = "equity-research";
        t.input_schema = ToolSchemaBuilder()
                             .integer("limit", "Maximum decisions to return")
                             .default_int(50)
                             .between(1, 500)
                             .build();
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            run_kr_tool({"decisions", "--limit", QString::number(args.value("limit").toInt(50))}, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_evaluate_outcome ────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_evaluate_outcome";
        t.description = "Evaluate a stored Korean-stock decision using server-side KIS bars and the matching "
                        "KOSPI/KOSDAQ benchmark. Computes raw return, benchmark return and alpha on common "
                        "trading dates and persists first-write-wins outcomes. Caller-supplied price bars are "
                        "not accepted.";
        t.category = "equity-research";
        t.input_schema =
            ToolSchemaBuilder()
                .string("decision_id", "Decision ID returned by kr_analyze_stock")
                .required()
                .length(1, 128)
                .array("horizons", "Trading-session horizons, normally [1,5,20,60]",
                       QJsonObject{{"type", "integer"}, {"minimum", 1}, {"maximum", 252}})
                .build();
        t.default_timeout_ms = kProviderTimeoutMs;
        t.supports_async = true;
        t.auth_required = AuthLevel::Authenticated;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            QStringList script_args{"evaluate", args.value("decision_id").toString()};
            if (args.value("horizons").isArray() && !args.value("horizons").toArray().isEmpty()) {
                script_args << "--horizons";
                for (const auto& value : args.value("horizons").toArray())
                    script_args << QString::number(value.toInt());
            }
            run_kr_tool(script_args, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_provider_smoke ───────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_outcome_log";
        t.description = "Read frozen forward outcomes and benchmark alpha for one stored Korean-stock research "
                        "decision. Results are ordered by trading-session horizon and are read-only.";
        t.category = "equity-research";
        t.input_schema = ToolSchemaBuilder()
                             .string("decision_id", "Stored personal-KR decision ID")
                             .required()
                             .length(1, 128)
                             .build();
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            run_kr_tool({"outcomes", args.value("decision_id").toString()}, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_provider_smoke ───────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_provider_smoke";
        t.description = "Fetch real Korean provider data on demand for one stock (KIS market + foreign/institution "
                        "flow, and DART/Naver/ECOS when configured). No LLM analysis and no order execution.";
        t.category = "equity-research";
        t.input_schema =
            ToolSchemaBuilder()
                .string("ticker", "Six-digit Korean listing code")
                .required()
                .pattern("^(?!000000)\\d{6}$")
                .string("company_name", "Korean company name")
                .required()
                .length(1, 120)
                .string("market", "Listing market")
                .required()
                .enums({"KOSPI", "KOSDAQ"})
                .string("analysis_date", "Analysis date YYYY-MM-DD; omit to use today")
                .pattern("^\\d{4}-\\d{2}-\\d{2}$")
                .build();
        t.default_timeout_ms = kProviderTimeoutMs;
        t.supports_async = true;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            QStringList script_args{
                "providers-only", "--ticker", args.value("ticker").toString(), "--name",
                args.value("company_name").toString(), "--market", args.value("market").toString(),
            };
            const QString analysis_date = args.value("analysis_date").toString();
            if (!analysis_date.isEmpty())
                script_args << "--analysis-date" << analysis_date;
            run_kr_tool(script_args, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_paper_summary ────────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_paper_summary";
        t.description = "Read the isolated personal-KR paper portfolio cash and positions. This ledger never "
                        "submits a live brokerage order.";
        t.category = "paper-trading";
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject&, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            run_kr_tool({"paper-summary"}, {}, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    // ── kr_paper_trade ──────────────────────────────────────────────────
    {
        ToolDef t;
        t.name = "kr_paper_trade";
        t.description = "Record a simulated Korean-stock trade in the personal-KR paper ledger. Requires explicit "
                        "confirmation, validates decision/ticker/date provenance, cash and oversell limits, and "
                        "never routes to a live broker.";
        t.category = "paper-trading";
        t.auth_required = AuthLevel::Authenticated;
        t.is_destructive = true;
        t.input_schema =
            ToolSchemaBuilder()
                .string("ticker", "Six-digit Korean listing code")
                .required()
                .pattern("^(?!000000)\\d{6}$")
                .string("side", "Paper trade side")
                .required()
                .enums({"BUY", "SELL"})
                .integer("quantity", "Share quantity")
                .required()
                .min(1)
                .number("price", "Simulated KRW execution price")
                .required()
                .min(0.000001)
                .string("trade_date", "Paper execution date YYYY-MM-DD; defaults to today")
                .pattern("^\\d{4}-\\d{2}-\\d{2}$")
                .string("decision_id", "Required frozen research decision ID")
                .required()
                .length(1, 128)
                .string("client_trade_id", "Required idempotency key for safe replay")
                .required()
                .length(1, 128)
                .number("fee", "Paper fee in KRW")
                .default_num(0.0)
                .min(0.0)
                .number("tax", "Paper tax in KRW")
                .default_num(0.0)
                .min(0.0)
                .build();
        t.default_timeout_ms = kStatusTimeoutMs;
        t.async_handler = [](const QJsonObject& args, ToolContext ctx,
                             std::shared_ptr<QPromise<ToolResult>> promise) {
            QJsonObject payload{
                {"ticker", args.value("ticker")},
                {"side", args.value("side")},
                {"quantity", args.value("quantity")},
                {"price", args.value("price")},
                {"fee", args.value("fee").toDouble(0.0)},
                {"tax", args.value("tax").toDouble(0.0)},
            };
            for (const char* key : {"trade_date", "decision_id", "client_trade_id"}) {
                const QString key_name = QString::fromLatin1(key);
                if (!args.value(key_name).toString().isEmpty())
                    payload[key_name] = args.value(key_name);
            }
            run_kr_tool({"paper-trade"}, payload, ctx, promise);
        };
        tools.push_back(std::move(t));
    }

    LOG_INFO(TAG, QString("Registered %1 personal KR research tools").arg(tools.size()));
    return tools;
}

} // namespace fincept::mcp::tools
