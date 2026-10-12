"""Explicit, cached cross-paper synthesis for Topic pages.

The model never runs during ordinary ingestion/compilation. Claims retain
their contributing paper IDs; a compiler labels them as unverified synthesis.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config
from ..index.database import Database
from ..llm.client import LLMClient
from ..models import StructuredPaper
from .prompts import answer_language_rules
from ..wiki.manager import normalize_name

SYNTHESIS_SYSTEM = """Create a careful, source-bounded comparison of multiple research papers.
You receive structured extractions, NOT verified full paper contents.
Never add outside facts, new datasets, invented experiments or numeric results.
Return JSON:
{
  "claims": [
    {"text": "a brief comparative statement", "source_ids": ["exact source ID", "exact source ID"]}
  ],
  "open_questions": [
    {"text": "an evidence-based research gap or disagreement", "source_ids": ["exact source ID"]}
  ]
}
Each source_ids array MUST be non-empty and contain only IDs supplied in the input.
Avoid conflating different experimental settings or metrics.
If there is insufficient evidence, return empty arrays.
Do not imply that summaries have been independently verified against original PDFs."""


def topic_key(title: str) -> str:
    return hashlib.sha256(normalize_name(title).encode("utf-8")).hexdigest()[:16]


def source_digest(papers: Iterable[Tuple[str, StructuredPaper]]) -> str:
    pairs = sorted((key, paper.to_dict()) for key, paper in papers)
    value = json.dumps(pairs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_topic_synthesis(
    root: Path,
    title: str,
    papers: Iterable[Tuple[str, StructuredPaper]],
) -> Optional[Dict[str, Any]]:
    path = root / "data" / "syntheses" / (topic_key(title) + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or data.get("topic") != normalize_name(title)
            or data.get("source_digest") != source_digest(papers)):
        return None
    return data


def _clean_points(value: Any, valid_source_ids: set[str]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    accepted = []
    for entry in value[:20]:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("text") or "").strip()
        cited = entry.get("source_ids")
        if not claim or len(claim) > 1500 or not isinstance(cited, list):
            continue
        ids = list(dict.fromkeys(str(v) for v in cited if str(v) in valid_source_ids))
        # Drop uncited or partially invalid source attribution, not just malformed JSON.
        if not ids or len(ids) != len(cited):
            continue
        accepted.append({"text": claim, "source_ids": ids})
    return accepted


def synthesize_topic(config: Config, title: str, llm: Optional[LLMClient] = None) -> Dict[str, Any]:
    client = llm or LLMClient(config.llm)
    if not client.enabled:
        raise RuntimeError("跨论文深度汇总需要先配置模型 API Key")
    database = Database(config.db_path)
    try:
        records = database.list_papers(page=1, page_size=100000)["items"]
    finally:
        database.close()
    papers: List[Tuple[str, StructuredPaper]] = []
    for entry in records:
        if entry["status"] != "INDEXED":
            continue
        source_id = str(entry["id"])
        cache = config.root / "data" / "extractions" / (source_id + ".json")
        try:
            item = StructuredPaper.from_dict(json.loads(cache.read_text(encoding="utf-8")))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if item.source_hash == source_id and any(normalize_name(t) == normalize_name(title) for t in item.topics):
            papers.append((source_id, item))
    papers.sort(key=lambda item: item[0])
    if len(papers) < 2:
        raise ValueError("当前主题少于两篇论文，无法进行跨论文综合")
    if len(papers) > 12:
        raise ValueError("该主题包含超过 12 篇论文，请先细分主题再进行可靠比较")
    if sum(p.extraction_mode != "basic" for _, p in papers) < 2:
        raise ValueError("请先对至少两篇相关论文进行深度抽取，再生成跨论文综合")

    digest = source_digest(papers)
    context = [{
        "source_id": source_id,
        "title": paper.title,
        "research_problem": paper.research_problem[:1800],
        "methodology": paper.methodology[:1800],
        "methods": [method.name for method in paper.methods][:12],
        "datasets": [dataset.name for dataset in paper.datasets][:12],
        "experiment_info": paper.experiment_info[:1500],
        "findings": paper.findings[:8],
        "limitations": paper.limitations[:6],
    } for source_id, paper in papers]
    response = client.structured(
        SYNTHESIS_SYSTEM + answer_language_rules(config.wiki_language),
        "Research topic: " + title + "\n\n" +
        json.dumps(context, ensure_ascii=False),
    )
    if not isinstance(response, dict):
        raise ValueError("跨论文总结响应必须是 JSON 对象")
    valid = {source_id for source_id, _ in papers}
    claims = _clean_points(response.get("claims"), valid)
    questions = _clean_points(response.get("open_questions"), valid)
    if not claims and not questions:
        raise ValueError("模型未产生含有效来源编号的跨论文总结，不保存不可信内容")

    result = {
        "topic": normalize_name(title), "source_ids": sorted(valid),
        "source_digest": digest, "claims": claims, "open_questions": questions,
        "review_status": "needs_review",
    }
    folder = config.root / "data" / "syntheses"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(folder), delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(result, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, folder / (topic_key(title) + ".json"))
    return result
