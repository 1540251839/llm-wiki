"""Conservative, fully offline extraction of *explicit* bibliographic facts.

This is deliberately a metadata extractor, not a scientific reasoning engine.
The method reports missing fields rather than filling them with generated prose.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Optional

from ..models import EntityMention, ParsedDocument, StructuredPaper


KNOWN_DATASETS: Dict[str, str] = {
    "LeCaRD": r"\bLeCaRD\b",
    "ELAM": r"\bELAM\b",
    "PHEME": r"\bPHEME\b",
    "Weibo": r"\bWeibo(?:\s+dataset)?\b",
    "MELD": r"\bMELD\b",
    "IEMOCAP": r"\bIEMOCAP\b",
    "CMU-MOSEI": r"\b(?:CMU[- ]MOSEI|MOSEI)\b",
    "CMU-MOSI": r"\b(?:CMU[- ]MOSI|MOSI)\b",
    "Hateful Memes": r"\bHateful\s+Memes\b",
    "MMHS150K": r"\bMMHS[- ]?150K\b",
}
_JOURNAL_HEADERS = (
    "contents lists available", "information sciences", "artificial intelligence",
    "journal homepage", "available online", "elsevier", "springer", "copyright",
    "a r t i c l e", "a b s t r a c t", "keywords", "key words",
    "proceedings of", "arxiv:", "doi:", "http:", "https:", "www.",
)
_STOP_SECTION = re.compile(
    r"(?im)^\s*(?:key\s*words|keywords?|index\s+terms|关键词|"
    r"(?:\d{1,2}\s*[.、)]?\s*)?(?:introduction|background|related work|引言|绪论|前言)|"
    r"a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|摘要|"
    r"(?:2|ii)\s*[.、 ]\s*\S+)\s*[:：—-]?\s*$"
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\r\n*·✩")


def _plausible_title(value: str) -> bool:
    name = _compact(value)
    if len(name) < 15 or len(name) > 230:
        return False
    lower = name.casefold()
    if any(lower.startswith(prefix) for prefix in _JOURNAL_HEADERS):
        return False
    if re.search(r"(?:@|www\.|https?://|doi[:/]|[^\s]@[^\s])", name, re.I):
        return False
    if re.fullmatch(r"[\d\s,.:;()/_-]+", name):
        return False
    # PDF headers and isolated journal names are not paper titles.
    if re.fullmatch(r"(?:vol(?:ume)?\.?\s*)?\d+[\s:(.)\d-]*", name, re.I):
        return False
    if name.count("_") or name.count("|") or name.count("©"):
        return False
    return True


def _pdf_title(source_path: Optional[Path]) -> str:
    if source_path is None or source_path.suffix.lower() != ".pdf":
        return ""
    try:
        try:
            import pymupdf  # type: ignore
        except ImportError:
            import fitz as pymupdf  # type: ignore
        with pymupdf.open(str(source_path)) as pdf:
            value = str((pdf.metadata or {}).get("title") or "").strip()
        if value.lower().startswith(("microsoft word", "untitled", "latex", "template")):
            return ""
        return _compact(value) if _plausible_title(value) else ""
    except (OSError, ValueError, RuntimeError, ImportError):
        return ""


def _title_from_lines(head: str, source_path: Optional[Path]) -> str:
    # Explicit title is more reliable than PDF creator metadata.
    explicit = re.search(r"(?im)^\s*(?:title|论文标题|题目)\s*[:：]\s*(.+)$", head[:3500])
    if explicit and _plausible_title(explicit.group(1)):
        return _compact(explicit.group(1))
    if source_path and source_path.suffix.lower() in {".md", ".markdown"}:
        heading = re.search(r"(?m)^\s*#\s+(.+)$", head[:4500])
        if heading and _plausible_title(heading.group(1)):
            return _compact(heading.group(1))
    metadata_title = _pdf_title(source_path)
    if metadata_title:
        return metadata_title
    lines = [_compact(line) for line in head[:6000].splitlines()]
    candidates = []
    for index, line in enumerate(lines[:48]):
        if not _plausible_title(line):
            continue
        # Reject frequent PDF author/affiliation or bibliographic furniture.
        if re.search(r"\b(?:received|accepted|corresponding author|affiliation|university|institute|issn|vol\.|issue)\b", line, re.I):
            continue
        if line.endswith(":") or re.match(r"^\d+\s", line):
            continue
        title = line
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if (re.search(r"\b(?:with|for|and|of|on|in|the|to|from|via|using)$", line, re.I)
                or re.search(r"[-–]$", line)) and following and len(following) < 95:
            title = _compact(line + " " + following)
        if _plausible_title(title):
            candidates.append(title)
        # An explicit abstract usually starts after the actual title.
        if len(candidates) >= 4:
            break
    return candidates[0] if candidates else "Untitled Paper"


def _authors(head: str) -> list[str]:
    match = re.search(r"(?im)^\s*(?:authors?|作者)\s*[:：]\s*(.{2,250})$", head[:2500])
    if not match:
        return []
    return [
        item for raw in re.split(r"[,;，；、]", match.group(1))
        if (item := _compact(raw)) and len(item) < 90
    ][:30]


def _year(head: str) -> Optional[int]:
    # Prefer an explicit publication year over arbitrary years in citations or abstracts.
    explicit = re.search(
        r"(?i)(?:published|publication date|available online|出版|发表|©|copyright)"
        r"[^\n]{0,35}\b((?:19|20)\d{2})\b",
        head[:3200],
    )
    if explicit:
        return int(explicit.group(1))
    journal = re.search(r"\(((?:19|20)\d{2})\)", head[:1600])
    if journal:
        return int(journal.group(1))
    early = re.search(r"\b((?:19|20)\d{2})\b", head[:700])
    return int(early.group(1)) if early else None


def _abstract(head: str) -> Optional[str]:
    # Extract only text bracketed by a visible abstract heading and section boundary.
    marker = re.search(r"(?im)^\s*(?:abstract|摘\s*要|a\s+b\s+s\s+t\s+r\s+a\s+c\s+t)\s*[:：—-]?\s*", head)
    if not marker:
        return None
    rest = head[marker.end():]
    limit = _STOP_SECTION.search(rest)
    text = _compact(rest[:limit.start()] if limit else rest[:4200])
    return text if 55 <= len(text) <= 4200 else None


def _keywords(head: str) -> list[str]:
    match = re.search(r"(?im)^\s*(?:key\s*words|keywords?|index\s+terms|关键词)\s*[:：—-]?\s*(.*)$", head)
    if not match:
        return []
    tail = match.group(1).strip()
    if not tail:
        candidates = []
        for line in head[match.end():].splitlines()[:8]:
            token = _compact(line)
            if not token or re.match(r"(?i)^(?:abstract|introduction|background|1\.|摘要|引言|a\s+b\s+s)", token):
                break
            if len(token) > 65:
                break
            candidates.append(token)
        tail = "; ".join(candidates)
    words = []
    for raw in re.split(r"[,;，；、]|\s+[·•]\s+", tail):
        token = _compact(raw)
        if 1 < len(token) < 90 and token not in words:
            words.append(token)
        if len(words) == 15:
            break
    return words


def _catalog(root: Optional[Path]) -> Dict[str, str]:
    """An optional local list of known datasets; never calls any service."""
    catalog = dict(KNOWN_DATASETS)
    if root is None:
        return catalog
    file = root / "data" / "dataset_catalog.json"
    if not file.exists():
        return catalog
    try:
        names = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(names, list):
            return catalog
        for name in names:
            if isinstance(name, str) and len(name.strip()) >= 3:
                canonical = name.strip()
                catalog[canonical] = r"(?<!\w)" + re.escape(canonical) + r"(?!\w)"
    except (OSError, ValueError, TypeError):
        pass
    return catalog


def extract_basic(
    document: ParsedDocument,
    source_path: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> StructuredPaper:
    """Return facts clearly visible in the document, with no model invocation."""
    head = (document.text or "")[:14000]
    full = document.text or ""
    datasets = []
    for name, pattern in _catalog(workspace_root).items():
        if re.search(pattern, full, re.I):
            # Mere mention is not proof the authors actually evaluated on this set.
            datasets.append(EntityMention(
                name=name, role="文中提及；是否实际用于实验需要复核"
            ))
    return StructuredPaper(
        title=_title_from_lines(head, source_path),
        authors=_authors(head),
        year=_year(head),
        abstract=_abstract(head),
        keywords=_keywords(head),
        datasets=datasets,
        extraction_mode="basic",
    )
