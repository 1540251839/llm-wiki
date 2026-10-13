"""Small, fully offline original-text chunks for searchable PDF/office sources.

No LLM calls. Every chunk is an exact slice of a parsed page; locations can
be checked against the immutable original document's extracted text.
"""
from __future__ import annotations

from typing import Any, Iterator, Dict


def source_chunks(pages: Any, max_chars: int = 1600) -> Iterator[Dict[str, Any]]:
    """Yield source-local slices without merging pages or inventing positions."""
    if not 500 <= max_chars <= 10000:
        raise ValueError("max_chars must be between 500 and 10000")
    for page in pages:
        full = str(page.text or "")
        pos = 0
        chunk_index = 0
        while pos < len(full):
            while pos < len(full) and full[pos].isspace():
                pos += 1
            if pos >= len(full):
                break
            cut = min(pos + max_chars, len(full))
            if cut < len(full):
                # Prefer paragraph/sentence boundaries but never discard text.
                minimum = pos + max_chars // 2
                breaks = [
                    full.rfind("\n\n", minimum, cut),
                    full.rfind("\n", minimum, cut),
                    full.rfind(". ", minimum, cut),
                    full.rfind("。", minimum, cut),
                    full.rfind(" ", minimum, cut),
                ]
                separator = next((value for value in breaks if value >= minimum), -1)
                if separator >= minimum:
                    cut = separator + (2 if full.startswith(". ", separator) or full.startswith("\n\n", separator) else 1)
            end = cut
            while end > pos and full[end - 1].isspace():
                end -= 1
            if end > pos:
                piece = full[pos:end]
                yield {
                    "page_number": int(page.number),
                    "chunk_index": chunk_index,
                    "char_start": pos, "char_end": end, "content": piece,
                }
                chunk_index += 1
            pos = cut
