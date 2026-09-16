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
            "kr_select_top_candidates",
            "kr_research_batch",
            "kr_analyze_stock",
            "kr_decision_log",
            "kr_evaluate_outcome",
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
        self.assertIn('run_kr_tool({"batch"}', tools)


if __name__ == "__main__":
    unittest.main()
