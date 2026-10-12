"""Per-field source quote checks; never treat a page citation as claim verification."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import ParsedDocument

ALLOWED_FIELDS = re.compile(
    r"^(?:title|abstract|abstract_zh|keywords|research_problem|methodology|"
    r"experiment_info|metrics(?:[.:]\d+)?|contributions(?:[.:]\d+)?|"
    r"findings(?:[.:]\d+)?|limitations(?:[.:]\d+)?|"
    r"methods[.:][^\n]{1,120}|datasets[.:][^\n]{1,120})$"
)


def locate_quote(document: ParsedDocument, quote: str) -> Dict[str, Any]:
    quote = quote.strip()
    if len(quote) < 12 or len(quote) > 1400:
        return {"status": "unverified", "reason": "quote_missing_or_invalid"}
    # Whitespace-normalized matching, preserving original offsets and quotes.
    parts = quote.split()
    if not parts:
        return {"status": "unverified", "reason": "quote_missing_or_invalid"}
    expr = re.compile(r"\s+".join(re.escape(part) for part in parts), re.I)
    for page in document.pages:
        found = expr.search(page.text)
        if found:
            a,b = found.span()
            return {
                "status": "quote_matched", "page": page.number,
                "char_start": a, "char_end": b,
                "quote": page.text[a:b],
                "locator": f"page:{page.number}#chars={a}-{b}",
            }
    return {"status": "unverified", "reason": "quote_not_found_in_source"}


def verify_field_evidence(document: ParsedDocument, payload: Any) -> List[Dict[str, Any]]:
    """Validate claimed support spans without asserting semantic entailment."""
    records = []
    if not isinstance(payload, list):
        return records
    for item in payload[:80]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not ALLOWED_FIELDS.fullmatch(field):
            continue
        span = locate_quote(document, quote)
        records.append({"field": field, **span})
    return records


def evidence_for_field(records: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    return [row for row in records if row.get("field") == field]
