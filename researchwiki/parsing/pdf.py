from __future__ import annotations

from pathlib import Path
from typing import Union

from ..models import Page, ParsedDocument


def parse_file(path: Union[str, Path]) -> ParsedDocument:
    """Parse a PDF through PyMuPDF, with plain-text support for smoke tests and notes."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    if source.suffix.lower() in {".txt", ".md", ".markdown"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        return ParsedDocument(text=text, pages=[Page(number=1, text=text)])
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PDF parsing requires PyMuPDF. Install dependencies with: pip install -e .") from exc
    document = pymupdf.open(str(source))
    pages = [Page(number=index + 1, text=page.get_text("text")) for index, page in enumerate(document)]
    return ParsedDocument(text="\n\n".join(page.text for page in pages), pages=pages)
