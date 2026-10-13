"""Deterministic quality gates, separate from factual support verification.

Passing these gates means the content is readable and not obviously broken. It
does not establish that an extracted claim is true: the verification layer does
that against parser-owned source spans.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

from ..models import ParsedDocument, StructuredPaper


class QualityGateError(RuntimeError):
    def __init__(self, message: str, quality: Dict[str, Any], stage: str = "extraction"):
        super().__init__(message)
        self.quality = quality
        self.issues = quality.get("issues", [])
        self.stage = stage


def _issue(code: str, message: str, severity: str = "error") -> Dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def suspicious_title(title: str) -> bool:
    value = " ".join(str(title or "").strip().casefold().split())
    if not value or value in {"untitled paper", "untitled", "unknown", "none", "null", "n/a"}:
        return True
    if value.startswith(("contents lists available", "available online at", "journal homepage", "www.sciencedirect", "http://", "https://")):
        return True
    return value in {"sciencedirect", "elsevier", "springer", "abstract", "references", "contents", "copyright", "research article", "original article", "full length article"}


def assess_paper(paper: StructuredPaper) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    if suspicious_title(paper.title):
        issues.append(_issue("suspicious_title", "抽取标题为空、占位符或期刊页眉，请核对原文标题后重新抽取。"))
    # Entity-only local/legacy extractions can be useful and are backwards
    # compatible. A topic alone is not a paper knowledge extraction.
    substantive = {
        "abstract": bool(str(paper.abstract or "").strip()),
        "research_problem": bool(paper.research_problem.strip()),
        "methodology": bool(paper.methodology.strip()),
        "methods": any(value.name.strip() for value in paper.methods),
        "datasets": any(value.name.strip() for value in paper.datasets),
        "contributions": any(value.strip() for value in paper.contributions),
        "findings": any(value.strip() for value in paper.findings),
        "claims": any(str(value.get("statement") or "").strip() for value in paper.claims),
    }
    if not any(substantive.values()):
        issues.append(_issue("sparse_extraction", "抽取没有摘要、研究问题、方法、数据集或事实，请重新抽取；主题标签不能代替论文内容。"))
    elif not (substantive["abstract"] or substantive["findings"] or substantive["claims"]):
        issues.append(_issue("partial_extraction", "摘要、发现与原子事实尚不完整；现有实体信息可保留，但不能据此声明事实已验证。", "warning"))
    if paper.year is not None and not 1600 <= paper.year <= 2200:
        issues.append(_issue("invalid_year", "抽取年份超出合理范围，请核对原文。"))
    allowed = not any(value["severity"] == "error" for value in issues)
    return {"version": "paper-quality-1", "allowed": allowed,
            "status": "passed" if allowed else "failed", "issues": issues,
            "signals": substantive}


def ensure_paper_quality(paper: StructuredPaper) -> Dict[str, Any]:
    quality = assess_paper(paper)
    paper.quality = quality
    if not quality["allowed"]:
        messages = [value["message"] for value in quality["issues"] if value["severity"] == "error"]
        raise QualityGateError("Paper quality gate failed: " + "; ".join(messages), quality)
    return quality


def assess_document(document: ParsedDocument, source_kind: str = "text") -> Dict[str, Any]:
    pages = document.pages
    text = document.text
    nonspace = [value for value in text if not value.isspace()]
    bad_count = sum(value == "\ufffd" or unicodedata.category(value) in {"Co", "Cn", "Cc"} for value in nonspace)
    ratio = bad_count / max(1, len(nonspace))
    empty_pages = [page.number for page in pages if not page.text.strip()]
    issues: List[Dict[str, str]] = []
    if not nonspace:
        issues.append(_issue("ocr_required", "文档没有可读文字层；请先 OCR，再导入可检索的文本 PDF。"))
    if ratio > 0.02:
        issues.append(_issue("corrupt_text", "文档含大量替换字符或无效字形；请使用 OCR 或增强解析后重试。"))
    elif ratio > 0.005:
        issues.append(_issue("text_artifacts", "文档存在少量无效字符，证据审核时请核对原文。", "warning"))
    if source_kind == "pdf" and pages and len(empty_pages) / len(pages) > 0.35:
        issues.append(_issue("partial_text_layer", "超过 35% 的 PDF 页面没有文字层，可能是部分扫描件；需 OCR 后再抽取。"))
    elif empty_pages:
        issues.append(_issue("empty_pages", "部分页面没有可读文字，请确认不是扫描正文。", "warning"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Broken one-glyph-per-line text can locate a quote mechanically while
    # destroying its meaning; stop before paying for a model extraction.
    fragmented = sum(len(line) <= 1 for line in lines) / max(1, len(lines))
    if len(lines) >= 30 and fragmented > 0.6:
        issues.append(_issue("fragmented_text", "正文大部分由单字断行组成，阅读顺序或文字层异常；请增强解析后重试。"))
    allowed = not any(value["severity"] == "error" for value in issues)
    return {"version": "document-quality-1", "allowed": allowed,
            "status": "passed" if allowed else "failed", "source_kind": source_kind,
            "issues": issues, "signals": {"pages": len(pages), "characters": len(text),
                "invalid_character_ratio": round(ratio, 6), "empty_pages": empty_pages,
                "one_character_line_ratio": round(fragmented, 6)}}


def ensure_document_quality(document: ParsedDocument, source_kind: str = "text") -> Dict[str, Any]:
    quality = assess_document(document, source_kind)
    previous = document.quality
    if previous.get("allowed") is False:
        quality = previous
    elif previous:
        quality["signals"] = {**previous.get("signals", {}), **quality["signals"]}
        known = {value["code"] for value in quality["issues"]}
        quality["issues"].extend(value for value in previous.get("issues", []) if value.get("code") not in known)
        if previous.get("parser_version"):
            quality["parser_version"] = previous["parser_version"]
    document.quality = quality
    if not quality["allowed"]:
        message = "; ".join(value["message"] for value in quality["issues"] if value["severity"] == "error")
        raise QualityGateError("Document quality gate failed: " + message, quality, stage="parsing")
    return quality
