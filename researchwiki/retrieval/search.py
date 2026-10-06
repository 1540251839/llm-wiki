from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..index.database import Database
from .vector import vector_search


def search(
    database: Database,
    query: str,
    top_k: int = 8,
    vector_config: Optional[Dict[str, Any]] = None,
    graph_expansion: bool = True,
) -> List[Dict[str, Any]]:
    """Hybrid retrieval using lexical, optional vector, and graph signals."""
    lexical = database.search(query, top_k=max(top_k * 2, 12))
    vectors = vector_search(database, query, vector_config or {}, max(top_k * 2, 12))
    fused: Dict[int, Dict[str, Any]] = {}

    def add(item: Dict[str, Any], contribution: float, signal: str) -> None:
        page_id = int(item.get("page_id") or item.get("id") or 0)
        if not page_id:
            return
        value = fused.setdefault(page_id, {**item, "page_id": page_id, "hybrid_score": 0.0, "retrieval_signals": []})
        value["hybrid_score"] += contribution
        if signal not in value["retrieval_signals"]:
            value["retrieval_signals"].append(signal)

    for rank, item in enumerate(lexical, 1):
        add(item, 1.0 / (60 + rank), "lexical")
    for rank, item in enumerate(vectors, 1):
        add(item, 0.85 / (60 + rank), "vector")

    if graph_expansion:
        seeds = sorted(fused.values(), key=lambda value: -float(value["hybrid_score"]))[: min(5, top_k)]
        for seed_rank, seed in enumerate(seeds, 1):
            for related_rank, related in enumerate(database.related_pages(int(seed["page_id"]), limit=4), 1):
                detail = database.get_wiki_page(int(related["id"])) or {}
                add(
                    {
                        "title": related["title"],
                        "type": related["type"],
                        "path": related["path"],
                        "content": detail.get("content", ""),
                        "page_id": related["id"],
                    },
                    0.22 / (60 + seed_rank + related_rank),
                    "graph",
                )

    ranked = sorted(
        fused.values(),
        key=lambda value: (-float(value["hybrid_score"]), str(value.get("title") or "").casefold()),
    )
    for value in ranked:
        value["score"] = value["hybrid_score"]
        value["sources"] = database.get_page_sources(int(value["page_id"]))
    return ranked[:top_k]
