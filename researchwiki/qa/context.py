from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .retrieval import factual_text
from ..wiki.manager import parse_frontmatter


def _int_config(config: Dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


def assemble_context(question: str, results: List[Dict[str, Any]],
                     config: Optional[Dict[str, Any]] = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
    """Pack typed evidence objects and provenance into one global character budget."""
    value = config or {}
    maximum = _int_config(value, "max_context_chars", 48000, 8000, 1_000_000)
    reserve = _int_config(value, "response_reserve_chars", 6000, 1000, maximum // 2)
    per_object = _int_config(value, "max_page_chars", 9000, 1000, maximum)
    overhead = _int_config(value, "prompt_overhead_chars", 1200 + len(question), 0, 10_000_000)
    budget = max(0, maximum - reserve - overhead)
    pieces, included = [], []
    used, evidence_count = 0, 0
    for item in results:
        content = factual_text(str(item.get("content") or ""))
        if parse_frontmatter(content).get("verification_status") == "no_supported_claims":
            continue
        kind = str(item.get("evidence_type") or "wiki_excerpt")
        if kind == "supported_claim" and item.get("verification_status") != "supported":
            continue
        remaining = min(per_object, budget - used - 8)
        if remaining < 100:
            break
        number = len(included) + 1
        label = "SUPPORTED CLAIM" if kind == "supported_claim" else "RAW EXCERPT — original-text location only, NOT independently supported" if kind == "raw_excerpt" else "WIKI EXCERPT"
        header = "SOURCE [" + str(number) + "]: [[" + str(item.get("title") or "")[:180] + "]]\n"
        header += "EVIDENCE TYPE: " + label + "\n"
        source_lines, selected = [], []
        provenance_limit = min(2200, max(0, remaining // 2 - len(header) - 100))
        provenance_used = 0
        for source in item.get("sources") or []:
            line = "- " + str(source.get("original_name") or source.get("source_path") or "source")[:150]
            source_id = str(source.get("source_id") or "")
            line += " [sha256:" + source_id + "] @ " + str(source.get("locator") or "")[:180]
            line += " | verification_status=" + str(source.get("verification_status") or "source_locator")[:32]
            if source.get("claim_id"):
                line += " | claim_id=" + str(source["claim_id"])[:100]
            # Original slices already form the content; do not duplicate them.
            quote = str(source.get("evidence_text") or "")
            if quote and kind != "raw_excerpt":
                available = provenance_limit - provenance_used - len(line) - 25
                if available < 40:
                    continue
                line += "\n  RAW EVIDENCE: " + quote[:min(900, available)]
            if provenance_used + len(line) + 1 > provenance_limit:
                continue
            source_lines.append(line)
            selected.append(source)
            provenance_used += len(line) + 1
        if kind in {"supported_claim", "raw_excerpt"} and not selected:
            continue
        if source_lines:
            header += "PROVENANCE:\n" + "\n".join(source_lines) + "\n"
        excerpt = content[:max(0, remaining - len(header))]
        if not excerpt.strip():
            continue
        block = header + excerpt
        pieces.append(block)
        included.append({**item, "citation_index": number, "context_excerpt": excerpt, "context_sources": selected})
        evidence_count += len(selected)
        used += len(block) + 8
    return "\n\n---\n\n".join(pieces), included, {
        "max_context_chars": maximum, "response_reserve_chars": reserve,
        "prompt_overhead_chars": overhead,
        "page_budget_chars": budget, "used_chars": used, "pages_included": len(included),
        "pages_retrieved": len(results), "source_evidence_included": evidence_count,
        "supported_claims_included": sum(item.get("evidence_type") == "supported_claim" for item in included),
        "raw_excerpts_included": sum(item.get("evidence_type") == "raw_excerpt" for item in included),
    }
