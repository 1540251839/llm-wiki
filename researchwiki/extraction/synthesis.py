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
from .checkpoints import ChunkCheckpoints, digest as checkpoint_digest, safe_response_meta
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
    language: str = "zh-CN",
) -> Optional[Dict[str, Any]]:
    path = root / "data" / "syntheses" / (topic_key(title) + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or data.get("topic") != normalize_name(title)
            or data.get("source_digest") != source_digest(papers)
            or data.get("language", "zh-CN") != language):
        return None
    return data


def _clean_points(value: Any, valid_source_ids: set[str]) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    accepted = []
    for entry in value:
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


def _context_batches(context: List[Dict[str, Any]], budget: int) -> List[List[Dict[str, Any]]]:
    """Keep every field/character, subdividing oversized records explicitly."""
    fragments: List[Dict[str, Any]] = []
    for item in context:
        encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) + 2 <= budget:
            fragments.append(item)
            continue
        payload = json.dumps({key: value for key, value in item.items() if key not in {"source_id", "title"}}, ensure_ascii=False)
        offset = 0
        while offset < len(payload):
            # Account for escaped strings and metadata in the wire JSON itself.
            end = min(len(payload), offset + max(256, budget // 2))
            fragment = {"source_id": item["source_id"], "title": item["title"],
                        "fragment_start": offset, "structured_fragment": payload[offset:end]}
            while len(json.dumps(fragment, ensure_ascii=False)) + 2 > budget and end > offset:
                end = offset + (end - offset) // 2
                fragment["structured_fragment"] = payload[offset:end]
            if end <= offset:
                raise ValueError("Synthesis source labels exceed input budget")
            fragment["fragment_end"] = end
            # Extra locator digits can push a near-boundary item over its limit.
            while len(json.dumps(fragment, ensure_ascii=False)) + 2 > budget:
                end -= 1
                if end <= offset:
                    raise ValueError("Synthesis fragment metadata exceeds input budget")
                fragment.update({"fragment_end": end, "structured_fragment": payload[offset:end]})
            fragments.append(fragment)
            offset = end
    batches, batch = [], []
    for item in fragments:
        if batch and len(json.dumps(batch + [item], ensure_ascii=False)) > budget:
            batches.append(batch)
            batch = []
        batch.append(item)
    if batch:
        batches.append(batch)
    return batches


def synthesize_topic(config: Config, title: str, llm: Optional[LLMClient] = None,
                     force_refresh: bool = False) -> Dict[str, Any]:
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
        "abstract": paper.abstract,
        "abstract_zh": paper.abstract_zh,
        "research_problem": paper.research_problem,
        "methodology": paper.methodology,
        "methods": [method.__dict__ for method in paper.methods],
        "datasets": [dataset.__dict__ for dataset in paper.datasets],
        "experiment_info": paper.experiment_info,
        "findings": paper.findings,
        "limitations": paper.limitations,
        "contributions": paper.contributions,
        "metrics": paper.metrics,
        "claims": paper.claims,
    } for source_id, paper in papers]
    valid = {source_id for source_id, _ in papers}
    try:
        budget = max(512, min(12000, int(config.ingestion.get("max_chunk_chars", 12000))))
    except (TypeError, ValueError):
        budget = 12000
    batches = _context_batches(context, budget)
    claims, questions = [], []
    checkpoints = ChunkCheckpoints(config.root / "data" / "synthesis_checkpoints" / topic_key(title))
    cached = 0
    for index, batch in enumerate(batches):
        system = SYNTHESIS_SYSTEM + answer_language_rules(config.wiki_language) + (
            "\nThis is a bounded batch. structured_fragment is an explicit character range of a complete structured record; missing neighboring ranges are not evidence. Only compare sources in this batch.")
        user = "Research topic: " + title + "\n\n" + json.dumps(batch, ensure_ascii=False)
        identity = {"stage": "extraction", "operation": "synthesis", "version": "complete-batches-1",
                    "topic": normalize_name(title), "source_digest": digest, "batch": index,
                    "batch_digest": checkpoint_digest(batch), "language": config.wiki_language,
                    "system_digest": checkpoint_digest(system), "user_digest": checkpoint_digest(user),
                    "client_type": type(client).__module__ + "." + type(client).__qualname__,
                    "client": {name: getattr(client, name, None) for name in (
                        "provider", "model", "max_output_tokens", "json_max_retries", "max_retries", "timeout_seconds")},
                    "base_url_digest": checkpoint_digest(str(getattr(client, "base_url", "")))}
        def valid_entry(entry: Dict[str, Any]) -> bool:
            payload = entry.get("payload")
            return bool(entry.get("type") == "result" and isinstance(payload, dict)
                        and isinstance(payload.get("claims", []), list)
                        and isinstance(payload.get("open_questions", []), list))
        entry, _state = checkpoints.read("extraction", identity, valid_entry) if not force_refresh else (None, "miss")
        if entry is not None:
            response = entry["payload"]
            cached += 1
        else:
            response = client.structured(system, user)
            if not valid_entry({"type": "result", "payload": response}):
                raise ValueError("跨论文总结响应必须包含 JSON 数组")
            checkpoints.write("extraction", identity, {"type": "result", "payload": response,
                "response_meta": safe_response_meta(getattr(client, "last_response_meta", {}))})
        batch_ids = {item["source_id"] for item in batch}
        claims.extend(_clean_points(response.get("claims"), batch_ids))
        questions.extend(_clean_points(response.get("open_questions"), batch_ids))
    def unique(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return list({json.dumps(point, ensure_ascii=False, sort_keys=True): point for point in points}.values())
    claims, questions = unique(claims), unique(questions)
    if not claims and not questions:
        raise ValueError("模型未产生含有效来源编号的跨论文总结，不保存不可信内容")

    result = {
        "topic": normalize_name(title), "source_ids": sorted(valid),
        "source_digest": digest, "claims": claims, "open_questions": questions,
        "review_status": "needs_review",
        "language": config.wiki_language,
        "batch_count": len(batches), "input_source_ids": sorted(valid),
        "cached_batch_count": cached,
        "comparison_scope": "within_batches" if len(batches) > 1 else "all_sources",
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
