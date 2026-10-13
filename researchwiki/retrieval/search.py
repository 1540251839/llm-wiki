from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..index.database import Database
from ..qa.retrieval import retrieve_evidence
from .vector import vector_search


def reciprocal_rank_fusion(
    rankings: Sequence[Tuple[str, Sequence[Dict[str, Any]], float]],
    rank_constant: int = 60,
) -> List[Dict[str, Any]]:
    """Combine independently ranked signals without comparing their raw scores.

    A page contributes once per signal, even when multiple chunks are returned.
    The result retains signal ranks and scores so the UI can explain its order.
    """
    fused: Dict[int, Dict[str, Any]] = {}
    for signal, candidates, weight in rankings:
        seen = set()
        rank = 0
        for candidate in candidates:
            page_id = int(candidate.get("page_id") or candidate.get("id") or 0)
            if not page_id or page_id in seen:
                continue
            seen.add(page_id)
            rank += 1
            item = fused.setdefault(page_id, {
                **candidate, "page_id": page_id, "hybrid_score": 0.0,
                "retrieval_signals": [], "signal_ranks": {}, "signal_scores": {},
            })
            # Prefer available text without overwriting the fused score/order.
            if not item.get("content") and candidate.get("content"):
                item["content"] = candidate["content"]
            item["hybrid_score"] += float(weight) / (max(1, rank_constant) + rank)
            item["retrieval_signals"].append(signal)
            item["signal_ranks"][signal] = rank
            item["signal_scores"][signal] = candidate.get("vector_score", candidate.get("score"))
    values = sorted(fused.values(), key=lambda item: (
        -float(item["hybrid_score"]), str(item.get("title") or "").casefold(),
        str(item.get("path") or ""), int(item["page_id"]),
    ))
    for item in values:
        item["score"] = item["hybrid_score"]
        item["retrieval"] = "hybrid_rrf"
    return values


def search(
    database: Database,
    query: str,
    top_k: int = 8,
    vector_config: Optional[Dict[str, Any]] = None,
    graph_expansion: bool = True,
    profile: str = "optimized",
) -> List[Dict[str, Any]]:
    """Recall broadly, then fuse weighted lexical, FTS, vector and graph ranks."""
    if not query.strip() or top_k <= 0:
        return []
    top_k = min(int(top_k), 100)
    recall_k = max(top_k * 4, 24)
    weighted = retrieve_evidence(database, query, top_k=recall_k, profile=profile)
    lexical = database.search(query, top_k=recall_k)
    vectors = vector_search(database, query, vector_config or {}, recall_k)
    rankings: List[Tuple[str, Sequence[Dict[str, Any]], float]] = [
        ("weighted_lexical", weighted, 1.0), ("fts", lexical, 1.0), ("vector", vectors, 1.0),
    ]
    if graph_expansion:
        seeds = reciprocal_rank_fusion(rankings)[:min(5, top_k)]
        related_candidates = []
        for seed in seeds:
            for related in database.related_pages(int(seed["page_id"]), limit=4):
                detail = database.get_wiki_page(int(related["id"])) or {}
                related_candidates.append({**related, "content": detail.get("content", ""), "page_id": related["id"]})
        rankings.append(("graph", related_candidates, 0.25))
    ranked = reciprocal_rank_fusion(rankings)[:top_k]
    for value in ranked:
        value["sources"] = database.get_page_sources(int(value["page_id"]))
    return ranked
