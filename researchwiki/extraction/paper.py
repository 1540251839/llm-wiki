from __future__ import annotations

from typing import Any, Optional
from pathlib import Path
import re

from ..llm.client import LLMClient
from ..models import ParsedDocument, StructuredPaper, EntityMention
from .context import extraction_context
from .evidence import verify_field_evidence
from .prompts import (
    PAPER_ANALYSIS_SYSTEM,
    PAPER_EXTRACTION_SYSTEM,
    DEEP_EXTRACTION_SYSTEM,
    paper_analysis_prompt,
    paper_extraction_prompt,
    extraction_language_rules,
)


def _valid_llm_payload(payload: Any) -> Optional[StructuredPaper]:
    if not isinstance(payload, dict) or not payload.get("title"):
        return None
    return StructuredPaper.from_dict(payload)


def analyze_paper(document: ParsedDocument, llm: LLMClient, purpose: str = "", language: str = "zh-CN") -> str:
    if not llm.enabled:
        raise RuntimeError("LLM is required for high-quality paper analysis")
    try:
        return llm.generate(
            PAPER_ANALYSIS_SYSTEM + extraction_language_rules(language),
            paper_analysis_prompt(extraction_context(document), purpose),
            temperature=0.1,
        )
    except Exception as exc:
        raise RuntimeError("LLM paper analysis failed: " + str(exc)) from exc


def extract_paper(
    document: ParsedDocument,
    llm: Optional[LLMClient] = None,
    analysis: str = "",
    purpose: str = "",
    language: str = "zh-CN",
) -> StructuredPaper:
    if llm is None or not llm.enabled:
        raise RuntimeError("LLM is required for paper extraction; configure the selected provider's API key")
    try:
        payload = llm.structured(
            PAPER_EXTRACTION_SYSTEM + extraction_language_rules(language),
            paper_extraction_prompt(extraction_context(document), analysis=analysis, purpose=purpose),
        )
    except Exception as exc:
        raise RuntimeError("LLM paper extraction failed: " + str(exc)) from exc
    parsed = _valid_llm_payload(payload)
    if parsed is None:
        raise RuntimeError("LLM returned invalid paper extraction JSON: a non-empty title is required")
    return parsed


def enrich_paper(
    document: ParsedDocument,
    baseline: StructuredPaper,
    llm: LLMClient,
    analysis: str = "",
    purpose: str = "",
    quality_mode: str = "standard",
    source_path: Optional[Path] = None,
    language: str = "zh-CN",
) -> StructuredPaper:
    """Optional LLM stage: augment metadata, never erase confirmed local fields.

    The deep model may return title/author/year for legacy compatibility, but
    verified local metadata takes precedence; the basic parser never invokes it.
    """
    if not llm.enabled:
        raise RuntimeError("Deep extraction requires a configured model API key")
    try:
        payload = llm.structured(
            DEEP_EXTRACTION_SYSTEM + extraction_language_rules(language),
            paper_extraction_prompt(document.text, analysis=analysis, purpose=purpose),
        )
    except Exception as exc:
        raise RuntimeError("Deep paper extraction failed: " + str(exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Deep extraction must return a JSON object")
    payload = dict(payload)
    payload.setdefault("title", baseline.title or "Untitled Paper")
    deep = _valid_llm_payload(payload)
    if deep is None:
        raise RuntimeError("Deep extraction returned an invalid paper object")

    # Avoid treating PDF journal headers and file names as authoritative titles.
    uncertain_title = baseline.title == "Untitled Paper" or baseline.title.casefold().startswith(
        ("contents lists available", "information sciences ", "artificial intelligence ")
    )
    # Plain-text fixtures often have an unmarked first line; deep extraction
    # can correct such weak title guesses. An explicit Title:/# heading remains authoritative.
    if source_path is not None and source_path.suffix.lower() in {".txt", ".md", ".markdown"}:
        explicit = re.search(r"(?im)^\s*(?:title|题目|论文标题)\s*[:：]", document.text[:2000])
        marked = source_path.suffix.lower() in {".md", ".markdown"} and re.search(r"(?m)^#\s+\S", document.text[:2000])
        uncertain_title = uncertain_title or not (explicit or marked)
    if uncertain_title and deep.title.strip() and deep.title != "Untitled Paper":
        baseline.title = deep.title
    baseline.authors = baseline.authors or deep.authors
    baseline.year = baseline.year or deep.year
    baseline.abstract = baseline.abstract or deep.abstract
    baseline.abstract_zh = deep.abstract_zh or baseline.abstract_zh
    # Prefer keywords from the author's paper, not model-generated substitutes.
    baseline.methods = deep.methods
    baseline.topics = deep.topics
    baseline.topic_labels_zh = deep.topic_labels_zh or baseline.topic_labels_zh
    baseline.research_problem = deep.research_problem
    baseline.methodology = deep.methodology
    baseline.experiment_info = deep.experiment_info
    baseline.metrics = deep.metrics
    baseline.contributions = deep.contributions
    baseline.findings = deep.findings
    baseline.limitations = deep.limitations

    datasets: dict[str, EntityMention] = {
        item.name.strip().casefold(): item for item in baseline.datasets if item.name.strip()
    }
    for item in deep.datasets:
        key = item.name.strip().casefold()
        if key:
            # Prefer an explicit model-extracted experimental role over a base mention.
            datasets[key] = item
    baseline.datasets = list(datasets.values())
    baseline.field_evidence = verify_field_evidence(document, payload.get("evidence"))
    baseline.extraction_mode = "high" if quality_mode == "high" else "deep"
    return baseline


FOCUSED_FIELDS = {
    "method": ("methods", "methodology", "contributions"),
    "experiment": ("datasets", "experiment_info", "metrics", "findings"),
    "overview": ("abstract_zh", "research_problem", "topics", "topic_labels_zh"),
}


def supplement_paper(
    document: ParsedDocument,
    previous: StructuredPaper,
    llm: LLMClient,
    focus: str,
    language: str = "zh-CN",
) -> StructuredPaper:
    """Update one research facet without regenerating other analyzed fields."""
    if focus not in FOCUSED_FIELDS:
        raise ValueError("focus must be method, experiment or overview")
    if not llm.enabled:
        raise RuntimeError("A model key is required for a focused supplement")
    import copy
    from .context import extraction_context
    section_text = extraction_context(document)
    # Keep front matter for context and only the section most relevant to this task.
    sections = {
        "method": ("FRONT SECTION", "METHOD SECTION"),
        "experiment": ("FRONT SECTION", "EXPERIMENT SECTION"),
        "overview": ("FRONT SECTION", "DISCUSSION SECTION"),
    }[focus]
    selected = [
        part for part in section_text.split("=== ")
        if any(part.startswith(name) for name in sections)
    ]
    context = ("=== ".join(selected) or section_text)[:42000]
    fields = ", ".join(FOCUSED_FIELDS[focus])
    system = (
        DEEP_EXTRACTION_SYSTEM + extraction_language_rules(language) +
        "\nFOCUSED UPDATE: Return only these top-level fields: " + fields +
        ", plus evidence. Do not infer other sections. Keep the existing"
        " paper title and canonical entity names. Include verbatim source quotes."
    )
    try:
        payload = llm.structured(system, paper_extraction_prompt(context))
    except Exception as exc:
        raise RuntimeError("Focused extraction failed: " + str(exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Focused extraction returned invalid data")
    patch = StructuredPaper.from_dict({"title": previous.title, **payload})
    result = copy.deepcopy(previous)
    for name in FOCUSED_FIELDS[focus]:
        value = getattr(patch, name)
        if value:
            if name == "datasets":
                merged = {entity.name.casefold(): entity for entity in result.datasets}
                for entity in value:
                    merged[entity.name.casefold()] = entity
                result.datasets = list(merged.values())
            else:
                setattr(result, name, value)
    new_evidence = verify_field_evidence(document, payload.get("evidence"))
    prefixes = {
        "method": ("methods", "methodology", "contributions"),
        "experiment": ("datasets", "experiment_info", "metrics", "findings"),
        "overview": ("abstract_zh", "research_problem", "topics"),
    }[focus]
    result.field_evidence = [
        row for row in result.field_evidence
        if not str(row.get("field") or "").startswith(prefixes)
    ] + new_evidence
    return result
