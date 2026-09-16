"""Small bounded-retry HTTP client used by Korean data providers."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpStatusError(RuntimeError):
    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class RetryHttpClient:
    def __init__(
        self,
        *,
        attempts: int = 3,
        timeout: float = 15.0,
        backoff: float = 0.25,
        transport: Callable[..., HttpResponse] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.attempts = max(1, attempts)
        self.timeout = timeout
        self.backoff = backoff
        self.transport = transport or self._transport
        self.sleep = sleep

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if params:
            from urllib.parse import urlencode

            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        body = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                response = self.transport(method, url, request_headers, body, self.timeout)
                if response.status == 429 or response.status >= 500:
                    raise HttpStatusError(response.status, response.body.decode("utf-8", "replace"))
                if response.status >= 400:
                    raise HttpStatusError(response.status, response.body.decode("utf-8", "replace"))
                return response
            except HttpStatusError as exc:
                last_error = exc
                if exc.status != 429 and exc.status < 500:
                    raise
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                last_error = exc
            if attempt + 1 < self.attempts:
                self.sleep(self.backoff * (2**attempt))
        assert last_error is not None
        raise last_error

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs).json()

    @staticmethod
    def _transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers={key: value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                body=exc.read(),
                headers={key: value for key, value in exc.headers.items()},
            )
