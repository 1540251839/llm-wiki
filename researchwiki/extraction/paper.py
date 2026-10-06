from __future__ import annotations

from typing import Any, Optional

from ..llm.client import LLMClient
from ..models import ParsedDocument, StructuredPaper
from .prompts import (
    PAPER_ANALYSIS_SYSTEM,
    PAPER_EXTRACTION_SYSTEM,
    paper_analysis_prompt,
    paper_extraction_prompt,
)


def _valid_llm_payload(payload: Any) -> Optional[StructuredPaper]:
    if not isinstance(payload, dict) or not payload.get("title"):
        return None
    return StructuredPaper.from_dict(payload)


def analyze_paper(document: ParsedDocument, llm: LLMClient, purpose: str = "") -> str:
    if not llm.enabled:
        raise RuntimeError("LLM is required for high-quality paper analysis")
    try:
        return llm.generate(
            PAPER_ANALYSIS_SYSTEM,
            paper_analysis_prompt(document.text, purpose),
            temperature=0.1,
        )
    except Exception as exc:
        raise RuntimeError("LLM paper analysis failed: " + str(exc)) from exc


def extract_paper(
    document: ParsedDocument,
    llm: Optional[LLMClient] = None,
    analysis: str = "",
    purpose: str = "",
) -> StructuredPaper:
    if llm is None or not llm.enabled:
        raise RuntimeError("LLM is required for paper extraction; configure the selected provider's API key")
    try:
        payload = llm.structured(
            PAPER_EXTRACTION_SYSTEM,
            paper_extraction_prompt(document.text, analysis=analysis, purpose=purpose),
        )
    except Exception as exc:
        raise RuntimeError("LLM paper extraction failed: " + str(exc)) from exc
    parsed = _valid_llm_payload(payload)
    if parsed is None:
        raise RuntimeError("LLM returned invalid paper extraction JSON: a non-empty title is required")
    return parsed
