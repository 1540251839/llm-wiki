"""Section-aware text selection, retaining context from the *middle* of papers.

Only source passages are included. This is selection, not summarization.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List

from ..models import ParsedDocument

HEADINGS = {
    "method": re.compile(r"^(?:(?:[2-9]|[IVX]+)[.、 ]+)?(?:method(?:ology|s)?|approach|proposed (?:framework|model)|模型|方法|框架)", re.I),
    "experiment": re.compile(r"^(?:(?:[2-9]|[IVX]+)[.、 ]+)?(?:experiments?|experimental|evaluations?|results?|实验|结果|评估|数据集)", re.I),
    "discussion": re.compile(r"^(?:(?:[2-9]|[IVX]+)[.、 ]+)?(?:discussion|limitations?|conclusions?|future work|讨论|局限|结论)", re.I),
    "related": re.compile(r"^(?:(?:[2-9]|[IVX]+)[.、 ]+)?(?:related work|references|bibliography|参考文献|相关工作)", re.I),
}
LIMITS = {"front": 12500, "method": 20000, "experiment": 23000, "discussion": 9000}
ORDER = ("front", "method", "experiment", "discussion")


def _heading(line: str) -> str | None:
    short = line.strip().strip("#* ")
    if not short or len(short) > 85:
        return None
    for section, expression in HEADINGS.items():
        if expression.match(short):
            return section
    return None


def extraction_context(document: ParsedDocument, budget: int = 65000) -> str:
    """Keep representative front/method/experiment/conclusion text across PDF pages.

    No inference is made about the role of a dataset mentioned in a section.
    Retains page labels so a model can return exact quotes for later verification.
    """
    # A paper that already fits the model budget should be read in full.
    # The previous implementation applied per-section caps even to short
    # papers, discarding sections that were not recognized as headings.
    if len(document.text or "") <= budget - 1024:
        if document.pages:
            return "\n\n".join(
                f"[PAGE {page.number}]\n{page.text}" for page in document.pages if page.text.strip()
            )
        return document.text
    passages: Dict[str, List[str]] = defaultdict(list)
    current = "front"
    pages = document.pages or []
    if not pages and document.text:
        from ..models import Page
        pages = [Page(1, document.text)]
    for page in pages:
        lines = page.text.splitlines(keepends=True)
        chunk: List[str] = []
        for line in lines:
            next_heading = _heading(line)
            if next_heading and next_heading != current:
                if chunk:
                    passages[current].append(f"[PAGE {page.number}]\n" + "".join(chunk))
                    chunk.clear()
                current = next_heading
            if current != "related":
                chunk.append(line)
        if chunk:
            passages[current].append(f"[PAGE {page.number}]\n" + "".join(chunk))
    output = []
    remaining = max(4000, budget)
    for kind in ORDER:
        available = "".join(passages[kind])
        limit = min(remaining, LIMITS[kind])
        if not available or limit < 150:
            continue
        # Each category keeps the section beginning. An experiment middle section
        # is never discarded just because the total document is long.
        chosen = available[:limit]
        output.append(f"=== {kind.upper()} SECTION / SOURCE TEXT ===\n" + chosen)
        remaining -= len(chosen)
    if not output:
        return document.text[:budget]
    return "\n\n".join(output)[:budget]
