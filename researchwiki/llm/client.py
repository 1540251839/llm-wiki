from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


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
        try:
            self.max_retries = max(0, int(config.get("max_retries", 1)))
        except (TypeError, ValueError):
            self.max_retries = 1
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

    def generate(self, system: str, user: str, temperature: float = 0.1, json_mode: bool = False) -> str:
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
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise RuntimeError("LLM request failed: " + str(exc.code) + " " + detail[:500]) from exc
            except (TimeoutError, socket.timeout) as exc:
                if attempt + 1 >= attempts:
                    raise RuntimeError(
                        "LLM request timed out after " + str(self.timeout_seconds) + " seconds "
                        "(" + str(attempts) + " attempt(s))"
                    ) from exc
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise RuntimeError("LLM request failed: " + str(exc.reason)) from exc
                if attempt + 1 >= attempts:
                    raise RuntimeError(
                        "LLM request timed out after " + str(self.timeout_seconds) + " seconds "
                        "(" + str(attempts) + " attempt(s))"
                    ) from exc
        else:
            raise RuntimeError("LLM request failed without a response")
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("LLM response did not contain choices")
        content = choices[0].get("message", {}).get("content", "")
        result = content if isinstance(content, str) else str(content)
        if not result.strip():
            raise RuntimeError("LLM response content was empty")
        return result

    def structured(self, system: str, user: str) -> Dict[str, Any]:
        response = self.generate(system, user, temperature=0, json_mode=True)
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", response, flags=re.S | re.I)
        candidate = fenced.group(1) if fenced else response
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
        value = json.loads(candidate)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value
