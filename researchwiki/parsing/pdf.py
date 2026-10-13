from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union
import hashlib
import re
import zipfile
from xml.etree import ElementTree

from ..models import DocumentBlock, Page, ParsedDocument
from ..extraction.quality import assess_document


PARSER_VERSION = "pymupdf-text-blocks-1"
TEXT_PARSER_VERSION = "text-paragraphs-1"


def section_heading(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first or len(first) > 140:
        return ""
    if re.match(r"^#{1,6}\s+\S", first):
        return first.lstrip("# ")
    if re.match(r"^(?:(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+)?(?:abstract|introduction|background|related work|method(?:ology|s)?|approach|experiments?|results?|evaluation|discussion|conclusions?|limitations?|references|appendix)(?:\s|$|:)", first, re.I):
        return first
    if re.match(r"^(?:\d+(?:\.\d+)*\.?\s+|[一二三四五六七八九十]+[、.．]\s*)[A-Z\u4e00-\u9fff][^.!?。！？]{1,100}$", first):
        return first
    if re.match(r"^(?:摘要|引言|绪论|相关工作|研究方法|方法|实验(?:结果|设置)?|讨论|结论|局限性|参考文献|附录)(?:\s|$|[：:])", first):
        return first
    return ""


def _block_type(text: str, heading: str) -> str:
    if heading and text.strip() == heading:
        return "heading"
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and sum("\t" in line or bool(re.search(r"\S {3,}\S", line)) for line in lines) >= 2:
        return "table"
    if re.match(r"^(?:Figure|Fig\.|Table|图|表)\s*\d", text.strip(), re.I):
        return "caption"
    if len(text) < 500 and sum(value in "=∑∫√≤≥±∈" for value in text) >= 2:
        return "formula"
    return "text"


def _block(page: Page, start: int, end: int, section: str,
           parser_version: str, bbox: Optional[List[float]] = None) -> DocumentBlock:
    text = page.text[start:end]
    heading = section_heading(text)
    signature = (parser_version + "\0" + str(page.number) + ":" + str(start) + ":" + str(end) + "\0" + text).encode("utf-8")
    return DocumentBlock(
        block_id="block_" + hashlib.sha256(signature).hexdigest()[:24],
        page=page.number, section=section, block_type=_block_type(text, heading),
        text=text, bbox=bbox, char_start=start, char_end=end, parser_version=parser_version,
    )


def build_document_blocks(pages: List[Page], parser_version: str = TEXT_PARSER_VERSION) -> List[DocumentBlock]:
    """Annotate existing page text without changing a single locator offset."""
    result: List[DocumentBlock] = []
    section = "front_matter"
    for page in pages:
        boundaries = [0] + [match.end() for match in re.finditer(r"\n[ \t]*\n+", page.text)] + [len(page.text)]
        for start, end in zip(boundaries, boundaries[1:]):
            text = page.text[start:end]
            if not text.strip():
                continue
            heading = section_heading(text)
            if heading:
                section = heading
            result.append(_block(page, start, end, section, parser_version))
    return result


def _parsed_text(text: str, source_kind: str) -> ParsedDocument:
    pages = [Page(number=1, text=text)]
    parsed = ParsedDocument(text=text, pages=pages, blocks=build_document_blocks(pages))
    # A scan remains a valid managed original. Only paid deep extraction is
    # blocked by this quality result; basic import must not discard the source.
    parsed.quality = assess_document(parsed, source_kind=source_kind)
    return parsed


def parse_file(path: Union[str, Path]) -> ParsedDocument:
    """Parse a PDF through PyMuPDF, with plain-text support for smoke tests and notes."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    if source.suffix.lower() in {".txt", ".md", ".markdown"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        return _parsed_text(text, "text")
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
        return _parsed_text(text, "docx")
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
        pages: List[Page] = []
        blocks: List[DocumentBlock] = []
        section = "front_matter"
        unaligned = 0
        for index, pdf_page in enumerate(document):
            # Keep the previous parser's unsorted text stream. Reordering it
            # would silently invalidate historical evidence character offsets.
            page = Page(number=index + 1, text=pdf_page.get_text("text", sort=False))
            pages.append(page)
            cursor = 0
            page_blocks: List[DocumentBlock] = []
            for raw in pdf_page.get_text("blocks", sort=False):
                if len(raw) < 7 or raw[6] != 0 or not str(raw[4]).strip():
                    continue
                value = str(raw[4])
                start = page.text.find(value, cursor)
                if start < 0:
                    unaligned += 1
                    continue
                end = start + len(value)
                heading = section_heading(value)
                if heading:
                    section = heading
                page_blocks.append(_block(page, start, end, section, PARSER_VERSION, [float(value) for value in raw[:4]]))
                cursor = end
            if not page_blocks and page.text.strip():
                page_blocks = build_document_blocks([page], PARSER_VERSION)
                for value in page_blocks:
                    if value.section == "front_matter":
                        value.section = section
                    else:
                        section = value.section
            blocks.extend(page_blocks)
    parsed = ParsedDocument(text="\n\n".join(page.text for page in pages), pages=pages, blocks=blocks)
    parsed.quality = assess_document(parsed, source_kind="pdf")
    parsed.quality["parser_version"] = PARSER_VERSION
    parsed.quality["signals"]["blocks"] = len(blocks)
    if unaligned:
        parsed.quality["issues"].append({"code": "bbox_alignment_unavailable", "severity": "warning",
            "message": "部分版面块无法匹配原有文字流，保留原页字符定位，未猜测其边界框。"})
    return parsed
