"""Conservative offline metadata extraction; never synthesize absent facts."""
from __future__ import annotations

import re
from ..models import ParsedDocument, StructuredPaper, EntityMention

KNOWN_DATASETS = {
    "LeCaRD": r"\bLeCaRD\b",
    "ELAM": r"\bELAM\b",
    "PHEME": r"\bPHEME\b",
    "Weibo": r"\bWeibo(?:\s+dataset)?\b",
    "MELD": r"\bMELD\b",
    "IEMOCAP": r"\bIEMOCAP\b",
    "MOSEI": r"\bCMU[- ]MOSEI\b",
    "MOSI": r"\bCMU[- ]MOSI\b",
    "Hateful Memes": r"\bHateful Memes\b",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_basic(document: ParsedDocument) -> StructuredPaper:
    """Only explicit text/known names. Uncertain fields stay blank for review."""
    full = document.text or ""
    head = full[:14000]
    lines = [_normalize(line) for line in head.splitlines()]
    lines = [line for line in lines if line]
    # Be conservative: title is only accepted if a plausible first-page line exists.
    rejected = re.compile(r"^(contents lists available|journal homepage|available online|arxiv:|doi:|www\.|https?://|abstract\b|keywords?\b|\d+$)", re.I)
    title = next((line for line in lines[:20] if 16 <= len(line) <= 240 and not rejected.search(line)), "")
    if not title:
        title = "Untitled Paper"
    author_match = re.search(r"(?im)^\s*(?:authors?|作者)\s*[:：]\s*(.+)$", head[:3000])
    authors = []
    if author_match:
        authors = [_normalize(name) for name in re.split(r"[,;，；、]", author_match.group(1)) if _normalize(name)]
    year_match = re.search(r"\b(?:19|20)\d{2}\b", head[:5000])
    year = int(year_match.group()) if year_match else None
    # English scientific abstracts with an explicit section boundary only.
    abstract = None
    match = re.search(
        r"(?im)^\s*(?:abstract|摘\s*要)\s*[:：—-]?\s*(.*?)"
        r"(?=^\s*(?:keywords?|index terms|key words|关键词|1\s*[.、 ]\s*introduction|introduction|引言|一[、.]\s*引言)\b|\Z)",
        head, re.S | re.M,
    )
    if match:
        value = _normalize(match.group(1))
        if 60 <= len(value) <= 6000:
            abstract = value
    keyword_match = re.search(r"(?im)^\s*(?:keywords?|index terms|key words|关键词)\s*[:：—-]\s*(.+)$", head)
    keywords = []
    if keyword_match:
        value = keyword_match.group(1).strip()
        for name in re.split(r"[,;，；、]", value):
            name = _normalize(name)
            if 1 < len(name) < 90 and len(keywords) < 15 and name not in keywords:
                keywords.append(name)
    # Restrict detection to mentions, not assertions that data was used in experiments.
    datasets = []
    for name, pattern in KNOWN_DATASETS.items():
        if re.search(pattern, full, re.I):
            datasets.append(EntityMention(name=name, role="文中提及；实验使用情况待核实"))
    return StructuredPaper(title=title, authors=authors, year=year, abstract=abstract, keywords=keywords, datasets=datasets)
