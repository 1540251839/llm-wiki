from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _int_config(config: Dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


def assemble_context(
    question: str,
    results: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, int]]:
    """Pack retrieved pages into one bounded prompt context.

    Budgets use characters because this project supports several compatible
    providers without depending on provider-specific tokenizers.
    """
    value = config or {}
    max_context = _int_config(value, "max_context_chars", 48000, 8000, 1_000_000)
    response_reserve = _int_config(value, "response_reserve_chars", 6000, 1000, max_context // 2)
    per_page = _int_config(value, "max_page_chars", 9000, 1000, max_context)
    fixed_overhead = min(6000, 1200 + len(question))
    page_budget = max(1000, max_context - response_reserve - fixed_overhead)
    pieces: List[str] = []
    included: List[Dict[str, Any]] = []
    used = 0
    for citation_index, item in enumerate(results, 1):
        source_lines = []
        for source in item.get("sources") or []:
            label = str(source.get("original_name") or source.get("source_path") or source.get("source_id") or "source")
            source_id = str(source.get("source_id") or source.get("sha256") or "")
            locator = str(source.get("locator") or "")
            source_lines.append("- " + label + (" [sha256:" + source_id[:12] + "]" if source_id else "") + (" @ " + locator if locator else ""))
            if source.get("evidence_text"):
                source_lines.append("  RAW EXCERPT: " + str(source["evidence_text"])[:1000])
        header = (
            "SOURCE [" + str(citation_index) + "]: [[" + str(item.get("title", "")) + "]] (" + str(item.get("type", "")) + ")\n"
            + ("PROVENANCE:\n" + "\n".join(source_lines) + "\n" if source_lines else "")
        )
        remaining = page_budget - used - len(header) - 8
        if remaining <= 0:
            break
        content = str(item.get("content", ""))
        take = min(per_page, remaining)
        excerpt = content[:take]
        if not excerpt.strip():
            continue
        block = header + excerpt
        pieces.append(block)
        included.append({**item, "citation_index": citation_index, "context_excerpt": excerpt})
        used += len(block) + 8
    return (
        "\n\n---\n\n".join(pieces),
        included,
        {
            "max_context_chars": max_context,
            "response_reserve_chars": response_reserve,
            "page_budget_chars": page_budget,
            "used_chars": used,
            "pages_included": len(included),
            "pages_retrieved": len(results),
        },
    )
