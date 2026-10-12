from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..index.database import Database


def _edge_id(source: str, target: str, semantic: str) -> str:
    value = (source + "\0" + target + "\0" + semantic).encode("utf-8")
    return "edge:" + hashlib.sha256(value).hexdigest()[:20]


def _wiki_root(database: Database, pages: List[Dict[str, Any]], configured: Optional[Union[str, Path]]) -> Path:
    if configured is not None:
        return Path(configured).resolve()
    default = database.path.resolve().parent.parent / "wiki"
    paths = [Path(str(page["path"])).resolve() for page in pages]
    if not paths or all(path.is_relative_to(default) for path in paths):
        return default
    # Support old callers with a nondefault Wiki directory. The standard page
    # type directories identify the root without depending on database row IDs.
    candidates: Counter = Counter()
    for path in paths:
        for ancestor in path.parents:
            if ancestor.name in {"papers", "methods", "datasets", "topics", "synthesis"}:
                candidates[ancestor.parent] += 1
                break
    if candidates:
        return candidates.most_common(1)[0][0]
    return Path(os.path.commonpath([str(path.parent) for path in paths]))


def _relative(path: str, root: Path) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return candidate.name


def extract_graph(database: Database, wiki_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Project the compiled Wiki into a deterministic, validated graph contract."""
    pages = database.list_wiki_pages(limit=10000)
    root = _wiki_root(database, pages, wiki_root)
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    page_node_ids: Dict[int, str] = {}
    source_nodes: Dict[str, str] = {}
    type_nodes: Dict[str, str] = {}

    for page in pages:
        page_id = int(page["id"])
        relative_path = _relative(str(page.get("path") or ""), root)
        node_id = "page:" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
        page_node_ids[page_id] = node_id
        sources = database.get_page_sources(page_id)
        primary = sources[0] if sources else {}
        nodes.append({
            "id": node_id,
            "name": str(page["title"]),
            "type": "TOPIC" if str(page.get("type") or "").casefold() == "topic" else "PAGE",
            "page_id": page_id,
            "path": str(page.get("path") or ""),
            "source_doc": str(primary.get("source_id") or ""),
            "page_number": int(primary.get("page_number") or 0),
            "char_start": int(primary.get("char_start") or 0),
            "char_end": int(primary.get("char_end") or 0),
            "confidence": "metadata_exact",
        })

        page_type = str(page.get("type") or "unknown").upper()
        type_id = type_nodes.setdefault(page_type, "page_type:" + page_type.casefold())
        if not any(value["id"] == type_id for value in nodes):
            nodes.append({"id": type_id, "name": page_type, "type": "PAGE_TYPE", "source_doc": "wiki schema"})
        edges.append({
            "id": _edge_id(node_id, type_id, "HAS_TYPE"),
            "source": node_id,
            "target": type_id,
            "relation": "CO_OCCURS_IN",
            "semantic": "HAS_TYPE",
            "doc_id": relative_path,
            "evidence": "Wiki frontmatter type=" + str(page.get("type") or ""),
        })

        for source in sources:
            source_id = str(source.get("source_id") or "")
            if not source_id:
                continue
            source_node = source_nodes.setdefault(source_id, "source:" + source_id)
            if not any(value["id"] == source_node for value in nodes):
                nodes.append({
                    "id": source_node,
                    "name": str(source.get("original_name") or source_id[:12]),
                    "type": "SOURCE",
                    "source_id": source_id,
                    "path": str(source.get("raw_path") or source.get("source_path") or ""),
                    "source_doc": source_id,
                    # A SOURCE is a whole document; per-page spans belong to its
                    # HAS_SOURCE edges, not to whichever page was visited first.
                    "page_number": 0,
                    "char_start": 0,
                    "char_end": 0,
                    "confidence": "source_hash_exact",
                })
            edges.append({
                "id": _edge_id(node_id, source_node, "HAS_SOURCE"),
                "source": node_id,
                "target": source_node,
                "relation": "CO_OCCURS_IN",
                "semantic": "HAS_SOURCE",
                "doc_id": source_id,
                "page_number": int(source.get("page_number") or 0),
                "char_start": int(source.get("char_start") or 0),
                "char_end": int(source.get("char_end") or 0),
                "locator": str(source.get("locator") or ""),
                "confidence": "source_excerpt_fallback" if "fallback" in str(source.get("section") or "").casefold() else "source_locator" if source.get("locator") else "source_metadata",
                "evidence": str(source.get("evidence_text") or "")[:700],
            })

    raw_links = database.conn.execute("SELECT source_id,target_id,relation FROM links ORDER BY source_id,target_id").fetchall()
    for link in raw_links:
        source = page_node_ids.get(int(link["source_id"]))
        target = page_node_ids.get(int(link["target_id"]))
        if not source or not target or source == target:
            continue
        edges.append({
            "id": _edge_id(source, target, "LINKS_TO"),
            "source": source,
            "target": target,
            "relation": "CO_OCCURS_IN",
            "semantic": "LINKS_TO",
            "doc_id": source,
            "evidence": "Compiled Wiki link",
        })

    nodes.sort(key=lambda value: (str(value["type"]), str(value["name"]).casefold(), str(value["id"])))
    unique_edges = {str(edge["id"]): edge for edge in edges}
    edges = sorted(unique_edges.values(), key=lambda value: (str(value["semantic"]), str(value["source"]), str(value["target"])))
    node_ids = {node["id"] for node in nodes}
    if len(node_ids) != len(nodes):
        raise ValueError("graph node identity collision: Wiki paths must be unique")
    if any(edge["source"] == edge["target"] or edge["source"] not in node_ids or edge["target"] not in node_ids for edge in edges):
        raise ValueError("graph contains a self-loop or missing endpoint")
    canonical_nodes = []
    for node in nodes:
        stable = {key: value for key, value in node.items() if key != "page_id"}
        if stable.get("path"):
            stable["path"] = _relative(str(stable["path"]), root if node["type"] in {"PAGE", "TOPIC"} else root.parent)
        canonical_nodes.append(stable)
    canonical = json.dumps({"schema_version": 2, "nodes": canonical_nodes, "edges": edges}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    result = database.replace_graph(nodes, edges, digest)
    return {**result, "nodes": nodes, "edges": edges}
