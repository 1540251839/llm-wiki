from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Sequence

from ..index.database import Database


class EmbeddingClient:
    """Minimal OpenAI-compatible embeddings client used only when enabled."""

    def __init__(self, config: Dict[str, Any]):
        self.enabled = bool(config.get("enabled", False))
        self.provider = str(config.get("provider") or "openai").lower()
        self.model = str(config.get("model") or "text-embedding-3-small")
        self.base_url = str(config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        try:
            self.timeout_seconds = float(config.get("timeout_seconds", 60))
        except (TypeError, ValueError):
            self.timeout_seconds = 60.0
        self.api_key = str(config.get("api_key") or "").strip()
        if not self.api_key:
            provider_names = {
                "openai": ("OPENAI_API_KEY",),
                "deepseek": ("DEEPSEEK_API_KEY",),
                "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
                "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
            }
            for name in ("EMBEDDING_API_KEY", *provider_names.get(self.provider, ())):
                value = os.environ.get(name)
                if value:
                    self.api_key = value
                    break

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.available:
            raise RuntimeError("vector retrieval is enabled but no embedding API key is configured")
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/embeddings",
            data=body,
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("embedding request failed: " + str(exc.code) + " " + detail[:400]) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("embedding request failed: " + str(exc)) from exc
        rows = sorted(payload.get("data") or [], key=lambda value: int(value.get("index", 0)))
        vectors = [value.get("embedding") for value in rows]
        if len(vectors) != len(texts) or not all(isinstance(value, list) and value for value in vectors):
            raise RuntimeError("embedding response did not contain one vector per input")
        return [[float(number) for number in value] for value in vectors]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def sync_vector_index(database: Database, config: Dict[str, Any]) -> Dict[str, Any]:
    client = EmbeddingClient(config)
    if not client.enabled:
        return {"enabled": False, "indexed": 0, "skipped": 0}
    if not client.available:
        return {"enabled": True, "indexed": 0, "skipped": 0, "error": "embedding API key is not configured"}
    indexed = 0
    skipped = 0
    try:
        for page in database.list_wiki_pages(limit=10000):
            page_id = int(page["id"])
            detail = database.get_wiki_page(page_id) or {}
            content = str(detail.get("content") or "")[:24000]
            digest = _content_hash(content)
            existing = database.get_embedding_record(page_id)
            if existing and str(existing.get("model")) == client.model and str(existing.get("content_hash")) == digest:
                skipped += 1
                continue
            vector = client.embed([str(page.get("title") or "") + "\n\n" + content])[0]
            database.upsert_embedding(page_id, client.model, vector, digest)
            indexed += 1
    except Exception as exc:
        return {"enabled": True, "indexed": indexed, "skipped": skipped, "error": str(exc)}
    return {"enabled": True, "indexed": indexed, "skipped": skipped, "model": client.model}


def vector_search(database: Database, query: str, config: Dict[str, Any], top_k: int) -> List[Dict[str, Any]]:
    client = EmbeddingClient(config)
    if not client.available:
        return []
    try:
        query_vector = client.embed([query])[0]
    except Exception:
        return []
    scored: List[Dict[str, Any]] = []
    for row in database.list_embeddings(client.model):
        try:
            vector = [float(value) for value in json.loads(str(row.get("vector") or "[]"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        similarity = _cosine(query_vector, vector)
        if similarity <= 0:
            continue
        scored.append({
            "title": row.get("title"),
            "type": row.get("type"),
            "path": row.get("path"),
            "content": row.get("content"),
            "page_id": row.get("page_id"),
            "score": -similarity,
            "vector_score": similarity,
            "retrieval": "vector",
        })
    scored.sort(key=lambda value: (-float(value["vector_score"]), str(value.get("title") or "").casefold()))
    return scored[:top_k]
