"""A research-reader graph projection, separate from the audit/debug graph contract.

Four object types only. Relations are projected from typed Wiki links; a
mere dataset mention is NOT labeled as experimentally evaluated on.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Optional

from ..index.database import Database
from ..wiki.manager import normalize_name, parse_frontmatter
from ..wiki.entities import canonical_entity
from ..qa.retrieval import _validated_span
from types import SimpleNamespace

LABELS = {
    "STUDIES_TOPIC": "研究主题",
    "REFERENCES_METHOD": "关联方法",
    "MENTIONS_DATASET": "提及数据集",
    "TESTED_ON": "已核验测试用途",
    "TRAINED_ON": "已核验训练用途",
    "VALIDATED_ON": "已核验验证用途",
    "RELATED": "相关研究",
}


def _id(path: str) -> str:
    return "research:" + hashlib.sha256(path.encode()).hexdigest()[:20]


def _dataset_role(quote: str, name: str) -> str:
    """A dataset and its experimental predicate must occur in the same clause."""
    term = re.escape(name)
    for relation, verb in (("TRAINED_ON", r"train(?:ed|ing|s)?"),
                           ("TESTED_ON", r"(?:test(?:ed|ing|s)?|evaluat(?:e|ed|es|ion))"),
                           ("VALIDATED_ON", r"validat(?:e|ed|es|ion)")):
        patterns = (
            r"\b" + verb + r"\b[^.;\n]{0,55}?\b(?:on|using|with)\s+(?:the\s+)?" + term + r"(?!\w)",
            r"(?<!\w)" + term + r"\s+(?:is|was|were|are)\s+used\s+(?:for|in)\s+" + verb + r"\b",
            r"\b(?:uses?|used)\s+(?:the\s+)?" + term + r"\s+(?:for|in)\s+" + verb + r"\b",
        )
        if any(re.search(pattern, quote, re.I) for pattern in patterns):
            # Do not cross another explicit role's clause to bind this target.
            for match in (re.search(pattern, quote, re.I) for pattern in patterns):
                if match and not re.search(r"\band\s+(?:train|test|evaluat|validat)", match.group(), re.I):
                    return relation
    for relation, verb in (("TRAINED_ON", "训练"), ("TESTED_ON", "测试|评测|评估"), ("VALIDATED_ON", "验证")):
        if re.search(r"(?:在|使用|采用)\s*" + term + r"[^，。；\n]{0,12}(?:" + verb + ")", quote):
            return relation
        if re.search(term + r"\s*(?:数据集)?\s*(?:用于|用作)(?:" + verb + ")", quote):
            return relation
    return ""


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
    try:
        manifest = json.loads((workspace / "wiki" / "manifest.json").read_text(encoding="utf-8"))
        claims_by_source = {str(source_id): page.get("claims", []) for page in manifest.get("pages", [])
                            if page.get("type") == "paper" for source_id in page.get("sources", [])}
    except (OSError, ValueError, TypeError):
        claims_by_source = {}
    validated_documents: Dict[str, Any] = {}
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
        support = {}
        if target["type"] == "dataset":
            source_hash = str(metadata[sid].get("source_hash") or "")
            for claim in claims_by_source.get(source_hash, []):
                if not isinstance(claim, dict) or (claim.get("verification") or {}).get("status") != "supported":
                    continue
                entities = [entry for entry in claim.get("entities", []) if isinstance(entry, dict)
                            and entry.get("type") == "dataset" and entry.get("name")
                            and canonical_entity("dataset", entry["name"])["canonical_id"] == canonical_entity("dataset", target["title"])["canonical_id"]]
                if not entities:
                    continue
                span = _validated_span(SimpleNamespace(root=workspace), database,
                        {**(claim.get("evidence") or {}), "source_id": source_hash}, validated_documents)
                if not span:
                    continue
                role = _dataset_role(span["evidence_text"], str(target["title"]))
                if role:
                    relation = role
                    support = {"claim_id": claim["claim_id"], "source_id": source_hash,
                               "verification_status": "supported", "locator": span["locator"],
                               "page_number": span["page_number"], "char_start": span["char_start"],
                               "char_end": span["char_end"], "evidence": span["evidence_text"]}
                    break
        edge_id = (node_ids[sid], node_ids[tid], relation)
        if edge_id in seen:
            continue
        seen.add(edge_id)
        edges.append({
            "id": "edge:" + hashlib.sha256("|".join(edge_id).encode()).hexdigest()[:18],
            "source": node_ids[sid], "target": node_ids[tid],
            "semantic": relation, "label": LABELS[relation],
            **support,
        })
        degree[node_ids[sid]] += 1
        degree[node_ids[tid]] += 1
    by_node = {item["id"]: item for item in nodes}
    all_edges = edges
    all_nodes = nodes
    global_view = center == "__all__"
    if not global_view and (not center or center not in by_node):
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
    if global_view:
        nodes = all_nodes[:max(1, min(500, limit))]
        selected = {node["id"] for node in nodes}
        edges = [edge for edge in all_edges if edge["source"] in selected and edge["target"] in selected]
    return {
        "nodes": nodes, "edges": edges,
        "stats": {"nodes": len(all_nodes), "edges": len(all_edges)},
        "center": center,
        "all_nodes": len(all_nodes), "all_edges": len(all_edges),
        "visible_nodes": len(nodes),
        "node_types": dict(Counter(node["type"] for node in nodes)),
        "relation_labels": LABELS,
        "mode": "research",
    }
