from __future__ import annotations

import unittest

from personal_kr.llm import FinceptConfiguredLlm, LlmConfig


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class LlmContractTests(unittest.TestCase):
    def test_gemini_matches_fincept_native_request_contract(self):
        http = FakeHttp(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )
        llm = FinceptConfiguredLlm(
            LlmConfig(
                provider="gemini",
                model_id="gemini-2.5-flash",
                api_key="secret",
                endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            ),
            http=http,
        )
        self.assertEqual(llm.complete("system", "user"), "ok")
        _, kwargs = http.calls[0]
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "secret")
        self.assertIn("systemInstruction", kwargs["json_body"])
        self.assertNotIn("system_instruction", kwargs["json_body"])

    def test_openai_compatible_and_fincept_response_shapes(self):
        openai_http = FakeHttp({"choices": [{"message": {"content": "answer"}}]})
        openai = FinceptConfiguredLlm(
            LlmConfig(
                provider="openai",
                model_id="gpt-4o-mini",
                api_key="k",
                endpoint="https://api.openai.com/v1/chat/completions",
            ),
            http=openai_http,
        )
        self.assertEqual(openai.complete("s", "u"), "answer")
        _, kwargs = openai_http.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer k")
        self.assertIn("max_completion_tokens", kwargs["json_body"])

        fincept_http = FakeHttp(
            {"success": True, "data": {"choices": [{"message": {"content": "fincept-answer"}}]}}
        )
        fincept = FinceptConfiguredLlm(
            LlmConfig(
                provider="fincept",
                model_id="MiniMax-M2.7",
                api_key="fk",
                endpoint="https://api.fincept.in/research/chat",
                session_token="session",
            ),
            http=fincept_http,
        )
        self.assertEqual(fincept.complete("s", "u"), "fincept-answer")
        _, kwargs = fincept_http.calls[0]
        self.assertEqual(kwargs["headers"]["X-API-Key"], "fk")
        self.assertEqual(kwargs["headers"]["X-Session-Token"], "session")

    def test_aggregator_contracts_match_fincept_native_routing(self):
        aihub_http = FakeHttp({"choices": [{"message": {"content": "ok"}}]})
        aihub = FinceptConfiguredLlm(
            LlmConfig(provider="aihubmix", model_id="gpt-5", api_key="k", max_tokens=1234),
            http=aihub_http,
        )
        self.assertEqual(aihub.complete("s", "u"), "ok")
        url, kwargs = aihub_http.calls[0]
        self.assertEqual(url, "https://aihubmix.com/v1/chat/completions")
        self.assertEqual(kwargs["json_body"]["max_completion_tokens"], 1234)
        self.assertNotIn("max_tokens", kwargs["json_body"])

        claude_http = FakeHttp({"choices": [{"message": {"content": "ok"}}]})
        claude = FinceptConfiguredLlm(
            LlmConfig(provider="aihubmix", model_id="claude-sonnet-4-5", api_key="k", max_tokens=777),
            http=claude_http,
        )
        self.assertEqual(claude.complete("s", "u"), "ok")
        self.assertEqual(claude_http.calls[0][1]["json_body"]["max_tokens"], 777)

        openrouter_http = FakeHttp({"choices": [{"message": {"content": "ok"}}]})
        openrouter = FinceptConfiguredLlm(
            LlmConfig(provider="openrouter", model_id="openai/gpt-4o", api_key="k"),
            http=openrouter_http,
        )
        self.assertEqual(openrouter.complete("s", "u"), "ok")
        headers = openrouter_http.calls[0][1]["headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://fincept.in")
        self.assertEqual(headers["X-Title"], "Fincept Terminal")

    def test_reasoning_and_refusal_shapes_do_not_become_false_empty_responses(self):
        reasoning_http = FakeHttp(
            {"choices": [{"message": {"content": "", "reasoning_content": "reasoned answer"}}]}
        )
        reasoning = FinceptConfiguredLlm(
            LlmConfig(provider="deepseek", model_id="deepseek-reasoner", api_key="k"),
            http=reasoning_http,
        )
        self.assertEqual(reasoning.complete("s", "u"), "reasoned answer")

        refusal_http = FakeHttp({"choices": [{"message": {"content": None, "refusal": "cannot comply"}}]})
        refusal = FinceptConfiguredLlm(
            LlmConfig(provider="openai", model_id="gpt-5", api_key="k"),
            http=refusal_http,
        )
        self.assertEqual(refusal.complete("s", "u"), "cannot comply")

    def test_anthropic_and_gemini_thinking_fallbacks_match_native_extractors(self):
        anthropic_http = FakeHttp({"content": [{"type": "thinking", "thinking": "anthropic thought"}]})
        anthropic = FinceptConfiguredLlm(
            LlmConfig(provider="anthropic", model_id="claude-sonnet-5", api_key="k"),
            http=anthropic_http,
        )
        self.assertEqual(anthropic.complete("s", "u"), "anthropic thought")

        gemini_http = FakeHttp(
            {"candidates": [{"content": {"parts": [{"thought": True, "text": "gemini thought"}]}}]}
        )
        gemini = FinceptConfiguredLlm(
            LlmConfig(provider="gemini", model_id="gemini-2.5-pro", api_key="k"),
            http=gemini_http,
        )
        self.assertEqual(gemini.complete("s", "u"), "gemini thought")


if __name__ == "__main__":
    unittest.main()
