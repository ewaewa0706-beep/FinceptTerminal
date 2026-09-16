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
            "kr_llm_smoke",
            "kr_select_top_candidates",
            "kr_research_batch",
            "kr_analyze_stock",
            "kr_decision_log",
            "kr_evaluate_outcome",
            "kr_outcome_log",
            "kr_provider_smoke",
            "kr_paper_summary",
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
        self.assertIn("branches: [main, personal-kr-terminal]", native_ci)
        for platform in ("windows-2022", "ubuntu-22.04", "macos-15"):
            self.assertIn(platform, native_ci)
        self.assertIn("Run all-screens smoke test (Linux)", native_ci)
        self.assertIn("ci_app_checks.sh smoke", native_ci)
        self.assertIn("if: runner.os == 'Linux'", native_ci)


if __name__ == "__main__":
    unittest.main()
