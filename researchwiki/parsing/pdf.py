from __future__ import annotations

from pathlib import Path
from typing import Union
import zipfile
from xml.etree import ElementTree

from ..models import Page, ParsedDocument


def parse_file(path: Union[str, Path]) -> ParsedDocument:
    """Parse a PDF through PyMuPDF, with plain-text support for smoke tests and notes."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    if source.suffix.lower() in {".txt", ".md", ".markdown"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        return ParsedDocument(text=text, pages=[Page(number=1, text=text)])
    if source.suffix.lower() == ".docx":
        # DOCX has no reliable physical page layout. Page 1 denotes the logical
        # text stream; API consumers expose paragraph/character locators instead.
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 20 * 1024 * 1024:
                raise ValueError("DOCX document body exceeds the 20 MiB parsing limit")
            body = ElementTree.fromstring(archive.read(info))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        text = "\n\n".join(
            "".join(paragraph.itertext()).strip()
            for paragraph in body.findall(".//w:p", ns)
        ).strip()
        if not text:
            raise ValueError("DOCX contains no readable text")
        return ParsedDocument(text=text, pages=[Page(number=1, text=text)])
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PDF parsing requires PyMuPDF. Install dependencies with: pip install -e .") from exc
    with pymupdf.open(str(source)) as document:
        if document.needs_pass:
            raise ValueError("PDF is encrypted; upload an unlocked copy")
        pages = [Page(number=index + 1, text=page.get_text("text")) for index, page in enumerate(document)]
    if not any(page.text.strip() for page in pages):
        raise ValueError("PDF has no readable text layer; OCR is required before importing")
    return ParsedDocument(text="\n\n".join(page.text for page in pages), pages=pages)
