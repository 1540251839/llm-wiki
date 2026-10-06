from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..extraction.prompts import QA_SYSTEM
from ..llm.client import LLMClient
from .context import assemble_context


def _source_items(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    values = []
    for position, item in enumerate(results[:5], 1):
        value: Dict[str, Any] = {
            "citation_index": int(item.get("citation_index") or position),
            "title": str(item.get("title", "")),
            "type": str(item.get("type", "")),
            "path": str(item.get("path", "")),
        }
        value["provenance"] = [
            {
                "source_id": str(source.get("source_id") or source.get("sha256") or ""),
                "name": str(source.get("original_name") or source.get("source_path") or ""),
                "raw_path": str(source.get("raw_path") or ""),
                "locator": str(source.get("locator") or ""),
                "page_number": int(source.get("page_number") or 0),
                "section": str(source.get("section") or ""),
                "evidence_text": str(source.get("evidence_text") or ""),
                "char_start": int(source.get("char_start") or 0),
                "char_end": int(source.get("char_end") or 0),
            }
            for source in item.get("sources") or []
        ]
        values.append(value)
    return values


def _section_evidence(question: str, content: str) -> List[str]:
    wanted: List[str] = []
    lowered = question.casefold()
    mappings = {
        "method": ("方法", "method", "approach"),
        "dataset": ("数据集", "dataset", "benchmark"),
        "contribution": ("贡献", "contribution"),
        "finding": ("结论", "结果", "finding", "result"),
        "limitation": ("局限", "限制", "limitation"),
        "abstract": ("摘要", "abstract", "概述"),
    }
    for key, terms in mappings.items():
        if any(term in lowered for term in terms):
            wanted.append(key)
    heading_keys = {
        "method": ("方法", "method"),
        "dataset": ("数据集", "dataset"),
        "contribution": ("贡献", "contribution"),
        "finding": ("结论", "结果", "finding", "result"),
        "limitation": ("局限", "限制", "limitation"),
        "abstract": ("摘要", "abstract"),
    }
    sections = re.findall(r"^##\s+(.+?)\s*$\n(.*?)(?=^##\s+|\Z)", content, flags=re.M | re.S)
    snippets: List[str] = []
    selected = wanted or ["abstract"]
    for key in selected:
        for heading, body in sections:
            if not any(term in heading.casefold() for term in heading_keys[key]):
                continue
            cleaned_lines = []
            for line in body.splitlines():
                clean = re.sub(r"^\s*(?:[-*>#]+)\s*", "", line).replace("`", "").strip()
                clean = re.sub(r"\[\[([^\]]+)\]\]", r"\1", clean)
                if clean and not clean.startswith("<!--"):
                    cleaned_lines.append(clean)
            if cleaned_lines:
                snippets.append(heading.strip() + "：" + "；".join(cleaned_lines)[:320])
                break
    return snippets[:4]


def answer_with_metadata(
    question: str,
    results: List[Dict[str, Any]],
    llm: Optional[LLMClient] = None,
    context_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not results:
        return {
            "answer": "没有在当前 Wiki 中检索到相关内容。请先导入论文，或换一个更具体的查询。",
            "mode": "no_evidence",
            "provider": None,
            "model": None,
            "fallback_reason": None,
            "sources": [],
            "context": {"pages_retrieved": 0, "pages_included": 0, "used_chars": 0},
        }
    context, included_results, context_stats = assemble_context(question, results, context_config)
    if not included_results:
        return {
            "answer": "检索结果没有可用正文，无法形成有证据的答案。",
            "mode": "no_evidence",
            "provider": None,
            "model": None,
            "fallback_reason": None,
            "sources": [],
            "context": context_stats,
        }
    fallback_reason: Optional[str] = None
    if llm and llm.enabled:
        try:
            generated = llm.generate(
                QA_SYSTEM,
                "Question:\n" + question + "\n\nResearchWiki context:\n" + context,
                temperature=0.2,
            )
            return {
                "answer": generated,
                "mode": "llm",
                "provider": llm.provider,
                "model": llm.model,
                "fallback_reason": None,
                "sources": _source_items(included_results),
                "context": context_stats,
            }
        except Exception as exc:
            fallback_reason = str(exc)
    query_terms = [term.lower() for term in re.findall(r"[\w-]{3,}", question) if term.lower() not in {"what", "which", "how", "why", "the", "this", "that", "and", "from"}]
    evidence = []
    for item in included_results:
        plain = re.sub(r"^---.*?---", "", str(item.get("content", "")), flags=re.S)
        sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", plain)
        for sentence in sentences:
            clean = re.sub(r"[#*`>-]", "", sentence).strip()
            if clean and (not query_terms or any(term in clean.lower() for term in query_terms)):
                evidence.append((str(item.get("title", "")), clean[:360]))
                if len(evidence) >= 6:
                    break
        if len(evidence) >= 6:
            break
    if not evidence:
        for item in included_results[:3]:
            plain = re.sub(r"^---.*?---", "", str(item.get("content", "")), flags=re.S)
            snippets = _section_evidence(question, plain)
            for snippet in snippets:
                evidence.append((str(item.get("title", "")), snippet))
                if len(evidence) >= 6:
                    break
    lines = ["基于当前 Wiki 的检索结果，相关内容主要来自："]
    if evidence:
        lines.append("")
        lines.append("证据摘录：")
        for title, snippet in evidence:
            matching = next((item for item in included_results if str(item.get("title")) == title), {})
            lines.append("- " + snippet + " [" + str(matching.get("citation_index") or 1) + "]" )
    for position, item in enumerate(included_results[:5], 1):
        lines.append("- [" + str(item.get("citation_index") or position) + "] [[" + str(item.get("title", "")) + "]]（" + str(item.get("type", "")) + "）")
    lines.append("")
    lines.append("离线模式未调用 LLM；以上为基于关键词的证据摘录，不等同于跨论文综合结论。")
    lines.append("")
    lines.append("Sources")
    for position, item in enumerate(included_results[:5], 1):
        lines.append("- [" + str(item.get("citation_index") or position) + "] [[" + str(item.get("title", "")) + "]] — " + str(item.get("path", "")))
    return {
        "answer": "\n".join(lines),
        "mode": "local_evidence",
        "provider": llm.provider if llm else None,
        "model": llm.model if llm else None,
        "fallback_reason": fallback_reason,
        "sources": _source_items(included_results),
        "context": context_stats,
    }


def answer(
    question: str,
    results: List[Dict[str, Any]],
    llm: Optional[LLMClient] = None,
    context_config: Optional[Dict[str, Any]] = None,
) -> str:
    return str(answer_with_metadata(question, results, llm, context_config=context_config)["answer"])
