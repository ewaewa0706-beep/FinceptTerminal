"""LLM adapters for the Fincept personal-KR research pipeline.

The desktop app passes its currently active LLM profile through PythonRunner
stdin.  Headless runs can still fall back to ``GOOGLE_API_KEY`` for a small,
dependency-free smoke path.  Keeping the profile payload on stdin is important:
API keys must never be placed on argv where they are visible in process lists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any, Protocol

from .http import RetryHttpClient


class Llm(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    model_id: str
    api_key: str = ""
    base_url: str = ""
    endpoint: str = ""
    session_token: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "LlmConfig":
        provider = str(data.get("provider") or "").strip().lower()
        model = str(data.get("model_id") or data.get("model") or "").strip()
        if not provider:
            raise ValueError("LLM provider is required")
        if not model and provider != "fincept":
            raise ValueError("LLM model_id is required")
        max_tokens = int(data.get("max_tokens") or 4096)
        if max_tokens < 1:
            max_tokens = 4096
        return cls(
            provider=provider,
            model_id=model,
            api_key=str(data.get("api_key") or ""),
            base_url=str(data.get("base_url") or ""),
            endpoint=str(data.get("endpoint") or ""),
            session_token=str(data.get("session_token") or ""),
            temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.2),
            max_tokens=max_tokens,
        )


def _join_chat_endpoint(base_url: str, provider: str, model: str) -> str:
    base = base_url.rstrip("/")
    if not base:
        return ""
    if provider in {"gemini", "google"}:
        if ":generateContent" in base:
            return base
        if not base.rsplit("/", 1)[-1].startswith("v1"):
            base += "/v1beta"
        return f"{base}/models/{model}:generateContent"
    suffix = "/messages" if provider == "anthropic" else "/chat/completions"
    if base.endswith(suffix):
        return base
    if base.rsplit("/", 1)[-1].startswith("v1"):
        return base + suffix
    return base + "/v1" + suffix


_DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "google": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "minimax": "https://api.minimax.io/v1/chat/completions",
    "kimi": "https://api.moonshot.ai/v1/chat/completions",
    "ollama": "http://localhost:11434/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
}


class FinceptConfiguredLlm:
    """Small HTTP client mirroring Fincept's active LLM provider contract."""

    def __init__(self, config: LlmConfig, *, http: RetryHttpClient | None = None) -> None:
        self.config = config
        self.http = http or RetryHttpClient(attempts=3, timeout=60.0, backoff=0.5)

    @property
    def provider(self) -> str:
        return self.config.provider

    @property
    def model(self) -> str:
        return self.config.model_id

    def _endpoint(self) -> str:
        if self.config.endpoint:
            return self.config.endpoint
        if self.config.base_url:
            return _join_chat_endpoint(self.config.base_url, self.provider, self.model)
        template = _DEFAULT_ENDPOINTS.get(self.provider, "")
        return template.format(model=self.model) if template else ""

    def complete(self, system: str, user: str) -> str:
        endpoint = self._endpoint()
        if not endpoint:
            raise ValueError(f"no LLM endpoint configured for provider {self.provider}")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("LLM endpoint must use http or https")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        provider = self.provider
        if provider == "anthropic":
            if self.config.api_key:
                headers["x-api-key"] = self.config.api_key
            headers["anthropic-version"] = "2023-06-01"
            body = {
                "model": self.model,
                "max_tokens": self.config.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        elif provider in {"gemini", "google"}:
            if self.config.api_key:
                headers["x-goog-api-key"] = self.config.api_key
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "maxOutputTokens": self.config.max_tokens,
                },
            }
        else:
            if provider == "fincept":
                if self.config.api_key:
                    headers["X-API-Key"] = self.config.api_key
                if self.config.session_token:
                    headers["X-Session-Token"] = self.config.session_token
                headers["User-Agent"] = "FinceptTerminal/4.0"
            elif self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            body = {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if provider != "fincept" or (self.model and self.model != "fincept-llm"):
                body["model"] = self.model
            if provider != "fincept":
                token_key = "max_completion_tokens" if provider in {"openai", "xai"} else "max_tokens"
                body[token_key] = self.config.max_tokens

        payload = self.http.post_json(endpoint, headers=headers, json_body=body)
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            if isinstance(error, dict):
                error = error.get("message") or error
            raise RuntimeError(f"LLM provider error: {error}")

        text = ""
        if provider == "anthropic":
            for block in payload.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += str(block.get("text") or "")
        elif provider in {"gemini", "google"}:
            candidates = payload.get("candidates") or []
            if candidates:
                for part in ((candidates[0].get("content") or {}).get("parts") or []):
                    if isinstance(part, dict) and not part.get("thought"):
                        text += str(part.get("text") or "")
        else:
            choices = payload.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(
                        str(part.get("text") or "") for part in content if isinstance(part, dict)
                    )
            if not text:
                text = str(payload.get("content") or payload.get("response") or "")
        text = text.strip()
        if not text:
            raise RuntimeError(f"{provider} returned an empty response")
        return text


class GoogleGeminiLlm:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash",
        http: RetryHttpClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is required")
        self.api_key = api_key
        self.model = model
        self.http = http or RetryHttpClient(attempts=3, timeout=60.0, backoff=0.5)

    @property
    def provider(self) -> str:
        return "google"

    @classmethod
    def from_env(cls, *, model: str | None = None, **kwargs: Any) -> "GoogleGeminiLlm":
        return cls(
            os.getenv("GOOGLE_API_KEY", ""),
            model=model or os.getenv("FINCEPT_KR_GEMINI_MODEL", "gemini-2.5-flash"),
            **kwargs,
        )

    def complete(self, system: str, user: str) -> str:
        configured = FinceptConfiguredLlm(
            LlmConfig(provider="gemini", model_id=self.model, api_key=self.api_key, temperature=0.2),
            http=self.http,
        )
        return configured.complete(system, user)


def llm_from_payload(config: dict[str, Any] | None) -> Llm:
    """Resolve the desktop active profile, falling back to headless Gemini."""

    if config:
        return FinceptConfiguredLlm(LlmConfig.from_mapping(config))
    return GoogleGeminiLlm.from_env()


class ScriptedLlm:
    """Deterministic test double that consumes a list of responses."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []
        self.provider = "scripted"
        self.model = "deterministic-test-double"

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.responses:
            return self.responses.pop(0)
        return "분석 완료. SIGNAL: HOLD"
