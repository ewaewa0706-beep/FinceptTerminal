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

        fincept_http = FakeHttp({"response": "fincept-answer"})
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


if __name__ == "__main__":
    unittest.main()
