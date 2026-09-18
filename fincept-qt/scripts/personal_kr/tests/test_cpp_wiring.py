from __future__ import annotations

import unittest
from pathlib import Path


QT_ROOT = Path(__file__).resolve().parents[3]


class CppWiringTests(unittest.TestCase):
    def test_personal_kr_mcp_is_registered_and_built(self):
        init = (QT_ROOT / "src/mcp/McpInit.cpp").read_text(encoding="utf-8")
        cmake = (QT_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        tools = (QT_ROOT / "src/mcp/tools/PersonalKrResearchTools.cpp").read_text(encoding="utf-8")

        self.assertIn('PersonalKrResearchTools.h', init)
        self.assertIn("get_personal_kr_research_tools()", init)
        self.assertGreaterEqual(cmake.count("src/mcp/tools/PersonalKrResearchTools.cpp"), 2)
        for name in (
            "kr_research_status",
            "kr_discover_market",
            "kr_llm_smoke",
            "kr_select_top_candidates",
            "kr_research_batch",
            "kr_analyze_stock",
            "kr_decision_log",
            "kr_evaluate_outcome",
            "kr_outcome_log",
            "kr_provider_smoke",
            "kr_paper_summary",
            "kr_paper_trade_log",
            "kr_paper_trade",
        ):
            self.assertIn(name, tools)

    def test_kr_credentials_are_managed_by_settings_and_python_runner(self):
        runner = (QT_ROOT / "src/python/PythonRunner.cpp").read_text(encoding="utf-8")
        settings = (QT_ROOT / "src/screens/settings/CredentialsSection.cpp").read_text(encoding="utf-8")
        catalogue = (QT_ROOT / "src/config/PersonalKrCredentials.inc").read_text(encoding="utf-8")

        self.assertIn("PersonalKrCredentials.inc", runner)
        self.assertIn("PersonalKrCredentials.inc", settings)
        for key in (
            "KIS_APP_KEY",
            "KIS_APP_SECRET",
            "KRX_AUTH_KEY",
            "DART_API_KEY",
            "NAVER_CLIENT_ID",
            "NAVER_CLIENT_SECRET",
            "ECOS_API_KEY",
            "GOOGLE_API_KEY",
        ):
            self.assertIn(key, catalogue)

    def test_equity_analysis_has_visible_research_only_entry_point(self):
        cpp = (QT_ROOT / "src/screens/equity_research/EquityAnalysisTab.cpp").read_text(encoding="utf-8")
        self.assertIn("RUN KR AI DEEP RESEARCH", cpp)
        self.assertIn("DISCOVER KR TOP-N", cpp)
        self.assertIn("RESEARCH SELECTED", cpp)
        self.assertIn("RESEARCH TOP-N (MAX 10)", cpp)
        self.assertIn('addItem(tr("Balanced"), "balanced")', cpp)
        self.assertIn('addItem(tr("All"), "ALL")', cpp)
        self.assertIn('kr_discovery_date_->setCalendarPopup(true)', cpp)
        self.assertIn('"--analysis-date", analysis_date.toString(Qt::ISODate)', cpp)
        self.assertIn('kr_discovery_min_value_->setSuffix(tr(" 억"))', cpp)
        self.assertIn('"--profile", profile', cpp)
        self.assertIn('"--market" << market', cpp)
        self.assertIn('"--min-trading-value-krw"', cpp)
        self.assertIn('data.value("scoring_profile")', cpp)
        self.assertIn('data.value("rank_overlay_count")', cpp)
        self.assertIn("KR RESEARCH HISTORY", cpp)
        self.assertIn("REFRESH DECISIONS", cpp)
        self.assertIn("EVALUATE 1/5/20/60D", cpp)
        self.assertIn("SHOW OUTCOMES", cpp)
        self.assertIn("PAPER SUMMARY", cpp)
        self.assertIn("PAPER TRADES", cpp)
        self.assertIn("RECORD PAPER TRADE", cpp)
        self.assertIn('"personal_kr_terminal.py", {"decisions", "--limit", "50"}', cpp)
        self.assertIn('"personal_kr_terminal.py", {"evaluate", decision_id', cpp)
        self.assertIn('"personal_kr_terminal.py", {"outcomes", decision_id}', cpp)
        self.assertIn('"personal_kr_terminal.py", {"paper-summary"}', cpp)
        self.assertIn('"personal_kr_terminal.py", {"paper-trades", "--limit", "100"}', cpp)
        self.assertIn('"personal_kr_terminal.py", {"paper-trade"}', cpp)
        self.assertIn("opts.stdin_data = QJsonDocument(payload).toJson(QJsonDocument::Compact)", cpp)
        self.assertIn("QUuid::createUuid()", cpp)
        self.assertIn("Confirm paper-only trade", cpp)
        self.assertIn("No live brokerage order will be sent", cpp)
        self.assertIn("pending request confirmed in ledger", cpp)
        self.assertIn("pending request confirmed absent; safe to enter a new paper trade", cpp)
        selection_handler = cpp.split("&QTableWidget::itemSelectionChanged", 1)[1].split("});", 1)[0]
        self.assertNotIn("kr_history_pending_paper_trade_ = {}", selection_handler)
        self.assertIn('QLatin1String("paper_only")', cpp)
        self.assertIn("kr_history_busy_ = busy", cpp)
        self.assertIn("selected && !kr_history_busy_", cpp)
        self.assertIn('tr("Liquidity")', cpp)
        self.assertIn('tr("Size")', cpp)
        self.assertIn('tr("Turnover")', cpp)
        self.assertIn('"discover", "--limit"', cpp)
        self.assertIn("kr_discovery_table_", cpp)
        self.assertIn('payload["strategy_id"] = "personal-kr-discovery-ui"', cpp)
        self.assertIn('payload["strategy_id"] = "personal-kr-discovery-batch-ui"', cpp)
        self.assertIn('"personal_kr_terminal.py", {"analyze"}', cpp)
        self.assertIn('"personal_kr_terminal.py", {"batch"}', cpp)
        self.assertIn("run_opts.timeout_ms = 60 * 60 * 1000", cpp)
        self.assertIn('"personal_kr_terminal.py"', cpp)
        self.assertIn("research_only", cpp)
        self.assertNotIn("pt_place_order", cpp)

    def test_desktop_research_uses_active_fincept_llm_over_stdin(self):
        helper = (QT_ROOT / "src/services/equity/PersonalKrLlmConfig.h").read_text(encoding="utf-8")
        tools = (QT_ROOT / "src/mcp/tools/PersonalKrResearchTools.cpp").read_text(encoding="utf-8")
        ui = (QT_ROOT / "src/screens/equity_research/EquityAnalysisTab.cpp").read_text(encoding="utf-8")
        self.assertIn("LlmService::instance()", helper)
        self.assertIn("ProviderCatalog::chat_endpoint", helper)
        self.assertIn('payload["llm"]', tools)
        self.assertIn('payload["llm"]', ui)
        self.assertIn('run_kr_tool({"llm-smoke"}', tools)
        self.assertIn('run_kr_tool({"batch"}', tools)
        self.assertIn('"analysis_cutoff_at"', tools)
        self.assertIn('"analysis_cutoff_at"', ui)
        self.assertIn('"analysis_cutoff_mode", "live_request"', ui)
        self.assertIn("toOffsetFromUtc(9 * 60 * 60)", ui)

    def test_research_calls_have_explicit_long_but_finite_timeouts(self):
        tools = (QT_ROOT / "src/mcp/tools/PersonalKrResearchTools.cpp").read_text(encoding="utf-8")
        ui = (QT_ROOT / "src/screens/equity_research/EquityAnalysisTab.cpp").read_text(encoding="utf-8")
        self.assertIn("kSingleResearchTimeoutMs = 20 * 60 * 1000", tools)
        self.assertIn("kBatchResearchTimeoutMs = 60 * 60 * 1000", tools)
        self.assertIn("t.default_timeout_ms = kBatchResearchTimeoutMs", tools)
        self.assertIn("t.default_timeout_ms = kSingleResearchTimeoutMs", tools)
        self.assertIn("run_opts.timeout_ms = 20 * 60 * 1000", ui)
        self.assertIn("run_with_options", ui)

    def test_market_discovery_preserves_snapshot_pit_and_exposes_krx_reconstruction(self):
        tools = (QT_ROOT / "src/mcp/tools/PersonalKrResearchTools.cpp").read_text(encoding="utf-8")
        section = tools.split('t.name = "kr_discover_market"', 1)[1].split('t.name = "kr_llm_smoke"', 1)[0]
        self.assertIn('"discover"', section)
        self.assertIn("exact snapshot", section)
        self.assertIn("KRX OpenAPI", section)
        self.assertIn("data_as_of", section)
        self.assertIn("cross-sectional v2", section)
        self.assertIn('enums({"balanced", "liquidity", "large_cap", "active"})', section)
        self.assertIn('enums({"ALL", "KOSPI", "KOSDAQ"})', section)
        self.assertNotIn('payload["llm"]', section)
        self.assertNotIn("paper", section.lower())

    def test_production_batch_is_bounded_to_ten_deep_research_names(self):
        tools = (QT_ROOT / "src/mcp/tools/PersonalKrResearchTools.cpp").read_text(encoding="utf-8")
        batch = tools.split('t.name = "kr_research_batch"', 1)[1].split('t.name = "kr_analyze_stock"', 1)[0]
        self.assertIn(".between(1, 10)", batch)

    def test_personal_kr_branch_runs_python_and_native_ci(self):
        repo_root = QT_ROOT.parent
        python_ci = (repo_root / ".github/workflows/personal-kr-python.yml").read_text(encoding="utf-8")
        native_ci = (repo_root / ".github/workflows/build-pr.yml").read_text(encoding="utf-8")

        # The long-lived development branch must get both the cheap Python
        # contract matrix and the release-style native build. Without the latter,
        # Personal-KR C++/MCP/UI changes can remain uncompiled until merge/release.
        self.assertIn("personal-kr-terminal", python_ci)
        self.assertIn("EquityAnalysisTab.h", python_ci)
        self.assertIn("branches: [main, personal-kr-terminal]", native_ci)
        for platform in ("windows-2022", "ubuntu-22.04", "macos-15"):
            self.assertIn(platform, native_ci)
        self.assertIn("Run all-screens smoke test (Linux)", native_ci)
        self.assertIn("ci_app_checks.sh smoke", native_ci)
        self.assertIn("if: runner.os == 'Linux'", native_ci)


if __name__ == "__main__":
    unittest.main()
