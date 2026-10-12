"""A research-reader graph projection, separate from the audit/debug graph contract.

Four object types only. Relations are projected from typed Wiki links; a
mere dataset mention is NOT labeled as experimentally evaluated on.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Optional

from ..index.database import Database
from ..wiki.manager import normalize_name, parse_frontmatter

LABELS = {
    "STUDIES_TOPIC": "研究主题",
    "REFERENCES_METHOD": "关联方法",
    "MENTIONS_DATASET": "提及数据集",
    "TESTED_ON": "测试数据集",
    "TRAINED_ON": "训练数据集",
    "VALIDATED_ON": "验证数据集",
    "RELATED": "相关研究",
}


def _id(path: str) -> str:
    return "research:" + hashlib.sha256(path.encode()).hexdigest()[:20]


def project_research_graph(
    database: Database, workspace: Path, center: Optional[str] = None,
    depth: int = 1, limit: int = 120,
) -> Dict[str, Any]:
    pages = database.list_wiki_pages(limit=10000)
    page_by_pk = {int(page["id"]): page for page in pages}
    node_ids = {}
    for page in pages:
        path = Path(page["path"])
        try:
            portable = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            portable = "/".join(path.parts[-3:])
        node_ids[int(page["id"])] = _id(portable)
    nodes = []
    metadata = {}
    for page in pages:
        pk = int(page["id"])
        detail = database.get_wiki_page(pk) or {}
        front = parse_frontmatter(detail.get("content") or "")
        metadata[pk] = front
        nodes.append({
            "id": node_ids[pk],
            "name": str(front.get("display_title") or page["title"]),
            "original_name": str(page["title"]),
            "type": str(page.get("type") or "").upper(),
            "page_id": pk,
        })
    degree = Counter()
    raw_edges = database.conn.execute(
        "SELECT source_id,target_id,relation FROM links ORDER BY source_id,target_id"
    ).fetchall()
    edges = []
    seen = set()
    extraction_by_source: Dict[str, Dict[str, Any]] = {}
    for link in raw_edges:
        sid, tid = int(link["source_id"]), int(link["target_id"])
        if sid not in node_ids or tid not in node_ids or sid == tid:
            continue
        source = page_by_pk[sid]
        target = page_by_pk[tid]
        if source["type"] != "paper" or target["type"] not in {"topic", "method", "dataset"}:
            continue
        relation = {"topic": "STUDIES_TOPIC", "method": "REFERENCES_METHOD",
                    "dataset": "MENTIONS_DATASET"}[target["type"]]
        if target["type"] == "dataset":
            source_hash = str(metadata[sid].get("source_hash") or "")
            if source_hash and source_hash not in extraction_by_source:
                file = workspace / "data" / "extractions" / (source_hash + ".json")
                try:
                    extraction_by_source[source_hash] = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    extraction_by_source[source_hash] = {}
            values = extraction_by_source.get(source_hash, {}).get("datasets") or []
            for value in values:
                if not isinstance(value, dict) or normalize_name(str(value.get("name") or "")) != normalize_name(target["title"]):
                    continue
                status = value.get("usage_status") or "mentioned"
                # Only exact quote matches may support an experimental role.
                locators = extraction_by_source.get(source_hash, {}).get("field_evidence") or []
                has_quote = any(
                    row.get("status") == "quote_matched" and
                    str(row.get("field") or "").startswith("datasets:")
                    for row in locators if isinstance(row, dict)
                )
                if has_quote:
                    relation = {"training": "TRAINED_ON", "testing": "TESTED_ON",
                                "validation": "VALIDATED_ON"}.get(status, relation)
                break
        edge_id = (node_ids[sid], node_ids[tid], relation)
        if edge_id in seen:
            continue
        seen.add(edge_id)
        edges.append({
            "id": "edge:" + hashlib.sha256("|".join(edge_id).encode()).hexdigest()[:18],
            "source": node_ids[sid], "target": node_ids[tid],
            "semantic": relation, "label": LABELS[relation],
        })
        degree[node_ids[sid]] += 1
        degree[node_ids[tid]] += 1
    by_node = {item["id"]: item for item in nodes}
    all_edges = edges
    all_nodes = nodes
    global_view = center == "__all__"\n    if not global_view and (not center or center not in by_node):
        paper_nodes = [node for node in nodes if node["type"] == "PAPER"]
        anchor = max(paper_nodes or nodes, key=lambda n: degree[n["id"]], default=None)
        center = anchor["id"] if anchor else ""
    if center and not global_view:
        allowed = {center}
        frontier = {center}
        depth = max(1, min(2, int(depth)))
        adjacency: Dict[str, set[str]] = defaultdict(set)
        for edge in all_edges:
            adjacency[edge["source"]].add(edge["target"])
            adjacency[edge["target"]].add(edge["source"])
        for _ in range(depth):
            candidates = set()
            for current in frontier:
                candidates.update(adjacency[current])
            frontier = candidates - allowed
            allowed.update(frontier)
        ordered = sorted((by_node[nid] for nid in allowed if nid in by_node),
                         key=lambda n: (n["id"] != center, n["type"], n["name"]))
        selected = {node["id"] for node in ordered[:max(1, min(500, limit))]}
        nodes = [node for node in ordered if node["id"] in selected]
        edges = [edge for edge in all_edges
                 if edge["source"] in selected and edge["target"] in selected]
    if global_view:\n        nodes = all_nodes[:max(1, min(500, limit))]\n        selected = {node["id"] for node in nodes}\n        edges = [edge for edge in all_edges if edge["source"] in selected and edge["target"] in selected]\n    return {\n        "nodes": nodes, "edges": edges,\n        "stats": {"nodes": len(all_nodes), "edges": len(all_edges)},
        "center": center,
        "all_nodes": len(all_nodes), "all_edges": len(all_edges),
        "visible_nodes": len(nodes),
        "node_types": dict(Counter(node["type"] for node in nodes)),
        "relation_labels": LABELS,
        "mode": "research",
    }
