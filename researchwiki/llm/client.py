from __future__ import annotations

import copy
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional


class StructuredOutputError(RuntimeError):
    """A failed completion that cannot safely be committed as structured data.

    Metadata is intentionally limited to counters and provider diagnostics. It
    never contains prompts, model response text, credentials, or request headers.
    """

    def __init__(self, message: str, *, reason: str, retryable: bool,
                 metadata: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.reason = reason
        self.retryable = bool(retryable)
        self.metadata = copy.deepcopy(metadata or {})


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite numbers are not valid JSON")


class LLMClient:
    """Small OpenAI-compatible client with no hard dependency on the SDK."""

    def __init__(self, config: Dict[str, Any]):
        self.provider = str(config.get("provider", "moonshot"))
        self.model = str(config.get("model", "kimi-k2-0711-preview"))
        self.base_url = str(config.get("base_url", "https://api.moonshot.cn/v1")).rstrip("/")
        try:
            self.timeout_seconds = float(config.get("timeout_seconds", 180))
        except (TypeError, ValueError):
            self.timeout_seconds = 180.0
        self.max_retries = _bounded_int(config.get("max_retries", 1), 1, 0, 5)
        self.json_max_retries = _bounded_int(config.get("json_max_retries", 1), 1, 0, 3)
        self.max_output_tokens = _bounded_int(config.get("max_output_tokens", 8192), 8192, 1, 393216)
        self.last_response_meta: Dict[str, Any] = {}
        provider_keys = {
            "deepseek": ("DEEPSEEK_API_KEY",),
            "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
            "openai": ("OPENAI_API_KEY",),
        }
        env_names = provider_keys.get(self.provider.lower(), (self.provider.upper() + "_API_KEY",))
        configured_key = config.get("api_key")
        self.api_key = str(configured_key).strip() if configured_key else ""
        if not self.api_key:
            for name in env_names:
                value = os.environ.get(name)
                if value:
                    self.api_key = value
                    break

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.provider not in {"none", "offline"}

    def _effective_temperature(self, requested: float) -> float:
        # Moonshot's kimi-k2.6 endpoint currently accepts only temperature=1.
        # Extraction and QA call this client with lower values for other models,
        # so normalize it here instead of letting the API reject the request.
        if self.provider.lower() == "moonshot" and self.model.lower() == "kimi-k2.6":
            return 1.0
        return requested

    def generate(self, system: str, user: str, temperature: float = 0.1, json_mode: bool = False,
                 *, max_output_tokens: Optional[int] = None) -> str:
        if not self.enabled:
            key_name = {
                "deepseek": "DEEPSEEK_API_KEY",
                "moonshot": "MOONSHOT_API_KEY",
                "kimi": "KIMI_API_KEY",
                "openai": "OPENAI_API_KEY",
            }.get(self.provider.lower(), self.provider.upper() + "_API_KEY")
            raise RuntimeError("LLM is disabled; set " + key_name + " (env or .env file) to enable it")
        body = {
            "model": self.model,
            "temperature": self._effective_temperature(temperature),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_output_tokens if max_output_tokens is None else
                          _bounded_int(max_output_tokens, self.max_output_tokens, 1, 393216),
        }
        if json_mode and self.provider.lower() == "deepseek":
            body["response_format"] = {"type": "json_object"}
            body["thinking"] = {"type": "disabled"}
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        events = []
        self.last_response_meta = {
            "provider": self.provider, "model": self.model,
            "max_output_tokens": body["max_tokens"], "request_attempts": 0,
            "requests": 0, "attempts": 0, "elapsed_seconds": 0.0,
            "total_elapsed_ms": 0, "finish_reason": None, "usage": {}, "events": events,
        }

        def snapshot() -> Dict[str, Any]:
            elapsed = time.monotonic() - started
            self.last_response_meta["elapsed_seconds"] = round(elapsed, 3)
            self.last_response_meta["total_elapsed_ms"] = round(elapsed * 1000)
            return copy.deepcopy(self.last_response_meta)

        def fail(message: str, reason: str, retryable: bool) -> StructuredOutputError:
            return StructuredOutputError(message, reason=reason, retryable=retryable, metadata=snapshot())

        attempts = self.max_retries + 1
        for attempt in range(attempts):
            attempt_started = time.monotonic()
            self.last_response_meta.update(request_attempts=attempt + 1, requests=attempt + 1, attempts=attempt + 1)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                events.append({"type": "request", "attempt": attempt + 1, "status": "completed",
                               "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                break
            except urllib.error.HTTPError as exc:
                # Do not expose a provider's echoed request/credential in logs.
                retryable = exc.code in {429, 500, 502, 503, 504}
                events.append({"type": "request", "attempt": attempt + 1, "status": "http_error",
                               "http_status": exc.code,
                               "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                exc.close()
                if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise fail("LLM request failed (HTTP " + str(exc.code) + ")", "request_failed", retryable) from None
            except (TimeoutError, socket.timeout, ConnectionError, http.client.IncompleteRead) as exc:
                events.append({"type": "request", "attempt": attempt + 1,
                               "status": "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "disconnected",
                               "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                if attempt + 1 >= attempts:
                    raise fail("LLM request timed out or disconnected (" + str(attempts) + " attempt(s))",
                               "request_failed", True) from None
            except urllib.error.URLError as exc:
                events.append({"type": "request", "attempt": attempt + 1, "status": "network_error",
                               "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                if attempt + 1 >= attempts:
                    raise fail("LLM request failed due to a network error (" + str(attempts) + " attempt(s))",
                               "request_failed", True) from None
            except (UnicodeError, json.JSONDecodeError) as exc:
                events.append({"type": "request", "attempt": attempt + 1, "status": "invalid_response",
                               "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                if attempt + 1 >= attempts:
                    raise fail("LLM provider returned an incomplete or invalid response body",
                               "invalid_response", True) from None
        else:
            raise fail("LLM request failed without a response", "request_failed", True)
        if not isinstance(payload, dict):
            raise fail("LLM provider response was not an object", "invalid_response", True)
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise fail("LLM response did not contain choices", "invalid_response", True)
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        usage = payload.get("usage")
        # Usage consists of token counters; whitelist these fields rather than
        # persisting arbitrary provider text in otherwise safe diagnostics.
        safe_usage = {key: value for key, value in (usage or {}).items()
                      if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
                                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                      and isinstance(value, (int, float)) and not isinstance(value, bool)} if isinstance(usage, dict) else {}
        if isinstance(usage, dict) and isinstance(usage.get("completion_tokens_details"), dict):
            reasoning_tokens = usage["completion_tokens_details"].get("reasoning_tokens")
            if isinstance(reasoning_tokens, (int, float)) and not isinstance(reasoning_tokens, bool):
                safe_usage["reasoning_tokens"] = reasoning_tokens
        known_finish_reasons = {"stop", "length", "max_tokens", "token_limit", "truncated", "content_filter",
                                "tool_calls", "insufficient_system_resource", "aborted", "incomplete"}
        safe_finish_reason = finish_reason if isinstance(finish_reason, str) and finish_reason in known_finish_reasons else None
        self.last_response_meta.update(finish_reason=safe_finish_reason, usage=safe_usage)
        message = choice.get("message") or {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        result = content if isinstance(content, str) else ""
        self.last_response_meta["response_chars"] = len(result)
        if safe_finish_reason in {"length", "max_tokens", "token_limit", "truncated"} or choice.get("truncated") is True or payload.get("truncated") is True:
            events.append({"type": "output", "status": "truncated", "finish_reason": safe_finish_reason})
            raise fail("LLM output was truncated at the token or context limit; retry a smaller document block",
                       "truncated", True)
        if safe_finish_reason in {"insufficient_system_resource", "aborted", "incomplete", "content_filter", "tool_calls"}:
            raise fail("LLM output ended before a complete answer (" + str(safe_finish_reason) + ")",
                       "incomplete_response", safe_finish_reason not in {"content_filter", "tool_calls"})
        if not result.strip():
            raise fail("LLM response content was empty", "empty", True)
        snapshot()
        return result

    def structured(self, system: str, user: str, *, max_output_tokens: Optional[int] = None,
                   json_max_retries: Optional[int] = None) -> Dict[str, Any]:
        """Parse only an entire JSON object, with a separate bounded format budget.

        Malformed JSON is regenerated from the original source, never completed
        by concatenating a suffix or salvaging a seemingly valid partial object.
        Token truncation bypasses this retry loop so the extractor can split the
        original source block instead of repeatedly hitting the same token cap.
        """
        retry_budget = self.json_max_retries if json_max_retries is None else _bounded_int(json_max_retries, self.json_max_retries, 0, 3)
        started = time.monotonic()
        generations = []
        format_events = []
        current_user = user
        structured_system = system + "\nReturn exactly one complete JSON object, with no prose outside the JSON."

        def metadata(reason: Optional[str] = None) -> Dict[str, Any]:
            latest = copy.deepcopy(generations[-1]) if generations else {}
            request_attempts = sum(int(item.get("request_attempts", item.get("requests", 1))) for item in generations)
            usage_totals: Dict[str, Any] = {}
            for item in generations:
                for key, value in (item.get("usage") or {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage_totals[key] = usage_totals.get(key, 0) + value
            elapsed = time.monotonic() - started
            latest.update(request_attempts=request_attempts, requests=request_attempts, attempts=request_attempts,
                          json_attempts=len(generations), json_retries=max(0, len(generations) - 1),
                          elapsed_seconds=round(elapsed, 3), total_elapsed_ms=round(elapsed * 1000),
                          usage_totals=usage_totals,
                          events=[event for item in generations for event in item.get("events", [])] + copy.deepcopy(format_events))
            latest.setdefault("usage", {})
            latest.setdefault("finish_reason", None)
            if reason:
                latest["structured_error"] = reason
            self.last_response_meta = latest
            return copy.deepcopy(latest)

        for attempt in range(retry_budget + 1):
            self.last_response_meta = {}
            try:
                options = {"temperature": 0, "json_mode": True}
                # Keep existing generate overrides/mocks working unless the
                # caller specifically opts into the new keyword argument.
                if max_output_tokens is not None:
                    options["max_output_tokens"] = max_output_tokens
                response = self.generate(structured_system, current_user, **options)
            except StructuredOutputError as exc:
                generations.append(copy.deepcopy(exc.metadata or self.last_response_meta))
                # Network retries have already consumed their own budget.
                if exc.reason != "empty":
                    raise StructuredOutputError(str(exc), reason=exc.reason, retryable=exc.retryable,
                                                metadata=metadata(exc.reason)) from exc
                response = ""
            else:
                generations.append(copy.deepcopy(self.last_response_meta))
            reported_finish = generations[-1].get("finish_reason")
            if (isinstance(reported_finish, str) and reported_finish in {"length", "max_tokens", "token_limit", "truncated"}) or generations[-1].get("truncated") is True:
                # This also protects subclasses that return a string and only
                # report completion metadata rather than raising themselves.
                raise StructuredOutputError("LLM output was truncated; retry a smaller document block",
                                            reason="truncated", retryable=True, metadata=metadata("truncated"))
            reason = "invalid_json"
            detail = "The entire response must be a valid JSON object"
            try:
                candidate = response.strip()
                if not candidate:
                    reason = "empty"
                    raise ValueError("No JSON object was returned")
                fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```", candidate, flags=re.S | re.I)
                if fenced:
                    candidate = fenced.group(1).strip()
                value = json.loads(candidate, parse_constant=_reject_constant)
                if not isinstance(value, dict):
                    reason = "invalid_object"
                    raise ValueError("Expected a JSON object, not an array or scalar")
                format_events.append({"type": "structured", "attempt": attempt + 1, "status": "valid"})
                metadata()
                return value
            except (json.JSONDecodeError, ValueError) as exc:
                if isinstance(exc, json.JSONDecodeError):
                    detail = "JSON syntax error at line " + str(exc.lineno) + ", column " + str(exc.colno)
                elif reason == "empty":
                    detail = "No JSON object was returned"
                elif reason == "invalid_object":
                    detail = "Expected a JSON object, not an array or scalar"
                format_events.append({"type": "structured", "attempt": attempt + 1, "status": reason,
                                      "will_retry": attempt < retry_budget})
                if attempt >= retry_budget:
                    raise StructuredOutputError("LLM structured output failed: " + detail +
                                                " (" + str(attempt + 1) + " JSON attempt(s))",
                                                reason=reason, retryable=True, metadata=metadata(reason)) from exc
                current_user = user + "\n\n[JSON format retry " + str(attempt + 1) + "]\n" + detail + ". " + \
                    "Regenerate the entire JSON object from the original source above. Preserve only source-supported facts, " + \
                    "do not invent missing facts or continue/patch the previous response. Use valid commas, escaped strings, " + \
                    "and closing delimiters. Return JSON only."
        raise StructuredOutputError("LLM structured output failed", reason="invalid_json", retryable=True, metadata=metadata("invalid_json"))

    def stream_generate(self, system: str, user: str, temperature: float = 0.1) -> Iterator[str]:
        """Yield provider deltas. Never retry a partly emitted response."""
        if not self.enabled:
            raise RuntimeError("LLM is disabled; configure the provider API key")
        body = {"model": self.model, "temperature": self._effective_temperature(temperature),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "stream": True}
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST",
        )
        emitted = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        if not emitted:
                            raise RuntimeError("LLM stream contained no answer content")
                        return
                    if not data:
                        continue
                    payload = json.loads(data)
                    if payload.get("error"):
                        raise RuntimeError("LLM provider reported a streaming error")
                    for choice in payload.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if isinstance(delta, str) and delta:
                            emitted = True
                            yield delta
        except urllib.error.HTTPError as exc:
            raise RuntimeError("LLM streaming request failed (HTTP " + str(exc.code) + ")") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, UnicodeError, ValueError) as exc:
            raise RuntimeError("LLM stream disconnected or returned invalid data") from exc
        raise RuntimeError("LLM stream disconnected before completion")
