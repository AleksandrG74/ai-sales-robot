from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Optional, Union

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings

log = logging.getLogger("core.llm")
log.setLevel(settings.log_level.upper())


# ============================================================
# Dataclasses
# ============================================================
@dataclass
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    text: str
    model: str
    finish_reason: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    raw: Optional[dict[str, Any]] = field(default=None, repr=False)

    def __str__(self) -> str:
        return self.text


MessageLike = Union[ChatMessage, dict[str, str]]
MessagesInput = Iterable[MessageLike]


# ============================================================
# Helpers
# ============================================================
def _normalize_messages(
    messages: MessagesInput,
    system: Optional[str] = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    has_system = False
    for m in messages:
        if isinstance(m, ChatMessage):
            d = m.to_dict()
        elif isinstance(m, dict):
            if "role" not in m or "content" not in m:
                raise ValueError(f"Bad message dict: {m!r}")
            d = {"role": str(m["role"]), "content": str(m["content"])}
        else:
            raise TypeError(f"Unsupported message type: {type(m)!r}")
        if d["role"] == "system":
            has_system = True
        out.append(d)

    if system and not has_system:
        out.insert(0, {"role": "system", "content": system})

    if not out:
        raise ValueError("messages is empty")
    return out


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return False


def _truncate(s: str, n: int = 500) -> str:
    if s is None:
        return ""
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n})"


# ============================================================
# LLMClient
# ============================================================
class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self.model = model or settings.deepseek_model
        self.timeout = timeout if timeout is not None else settings.deepseek_timeout
        self.max_retries = (
            max_retries if max_retries is not None else settings.deepseek_max_retries
        )

        if not self.api_key:
            log.warning("LLMClient created without DEEPSEEK_API_KEY - calls will fail")

        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None
        self._lock = asyncio.Lock()

    # ---------- internal ----------
    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def _payload(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
        model: Optional[str],
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    async def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self.timeout)
        return self._async_client

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=self.timeout)
        return self._sync_client

    # ---------- async chat ----------
    async def chat(
        self,
        messages: MessagesInput,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        normalized = _normalize_messages(messages, system=system)
        payload = self._payload(normalized, temperature, max_tokens, model, stream=False)
        url = self._url()
        headers = self._headers()
        client = await self._get_async_client()

        if log.isEnabledFor(logging.DEBUG):
            log.debug("LLM request: model=%s, msgs=%d, temp=%.2f",
                      payload["model"], len(normalized), temperature)
            for m in normalized:
                log.debug("  [%s] %s", m["role"], _truncate(m["content"]))

        t0 = time.perf_counter()
        response: Optional[httpx.Response] = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max(1, self.max_retries)),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
        except RetryError as exc:  # pragma: no cover
            log.error("LLM chat failed after retries: %s", exc)
            raise
        except Exception as exc:
            log.exception("LLM chat failed: %s", exc)
            raise

        latency_ms = (time.perf_counter() - t0) * 1000.0
        assert response is not None
        data = response.json()

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            log.error("Bad LLM response shape: %s", data)
            raise RuntimeError(f"Bad LLM response: {exc}") from exc

        usage = data.get("usage") or {}
        result = LLMResponse(
            text=text,
            model=data.get("model", payload["model"]),
            finish_reason=finish_reason,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            raw=data,
        )
        log.info(
            "LLM chat ok: model=%s, in=%d, out=%d, %.0fms",
            result.model, result.tokens_in, result.tokens_out, result.latency_ms,
        )
        return result

    # ---------- async stream ----------
    async def chat_stream(
        self,
        messages: MessagesInput,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncIterator[str]:
        normalized = _normalize_messages(messages, system=system)
        payload = self._payload(normalized, temperature, max_tokens, model, stream=True)
        url = self._url()
        headers = self._headers()
        client = await self._get_async_client()

        log.info("LLM stream start: model=%s, msgs=%d", payload["model"], len(normalized))

        try:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        log.debug("Bad SSE chunk: %s", _truncate(data_str))
                        continue
                    try:
                        delta = chunk["choices"][0].get("delta") or {}
                        content = delta.get("content")
                    except (KeyError, IndexError, TypeError):
                        content = None
                    if content:
                        yield content
        except Exception as exc:
            log.exception("LLM stream failed: %s", exc)
            raise
        log.info("LLM stream done")

    # ---------- sync ----------
    def chat_sync(
        self,
        messages: MessagesInput,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        normalized = _normalize_messages(messages, system=system)
        payload = self._payload(normalized, temperature, max_tokens, model, stream=False)
        url = self._url()
        headers = self._headers()
        client = self._get_sync_client()

        t0 = time.perf_counter()
        response: Optional[httpx.Response] = None
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(max(1, self.max_retries)),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    response = client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
        except RetryError as exc:  # pragma: no cover
            log.error("LLM chat_sync failed after retries: %s", exc)
            raise
        except Exception as exc:
            log.exception("LLM chat_sync failed: %s", exc)
            raise

        latency_ms = (time.perf_counter() - t0) * 1000.0
        assert response is not None
        data = response.json()

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            log.error("Bad LLM response shape: %s", data)
            raise RuntimeError(f"Bad LLM response: {exc}") from exc

        usage = data.get("usage") or {}
        result = LLMResponse(
            text=text,
            model=data.get("model", payload["model"]),
            finish_reason=finish_reason,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            raw=data,
        )
        log.info(
            "LLM chat_sync ok: model=%s, in=%d, out=%d, %.0fms",
            result.model, result.tokens_in, result.tokens_out, result.latency_ms,
        )
        return result

    # ---------- lifecycle ----------
    async def aclose(self) -> None:
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception as exc:
                log.warning("Error closing async client: %s", exc)
            finally:
                self._async_client = None

    def close(self) -> None:
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception as exc:
                log.warning("Error closing sync client: %s", exc)
            finally:
                self._sync_client = None


# ============================================================
# Singleton
# ============================================================
_llm_instance: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMClient()
    return _llm_instance
