from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import copy
import re
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..llm.client import LLMClient
from ..models import EntityMention, Page, ParsedDocument, StructuredPaper
from .checkpoints import ChunkCheckpoints, digest, safe_response_meta
from .quality import ensure_document_quality, ensure_paper_quality, suspicious_title
from .evidence import verify_field_evidence
from .prompts import (PAPER_ANALYSIS_SYSTEM, PAPER_EXTRACTION_SYSTEM, DEEP_EXTRACTION_SYSTEM,
                      paper_analysis_prompt, paper_extraction_prompt, extraction_language_rules)


MAX_CHUNK_CHARS = 12000
EXTRACTION_VERSION = "unified-section-chunks-4"


@dataclass
class ExtractionChunk:
    chunk_id: str
    text: str
    segments: List[Dict[str, Any]] = field(default_factory=list)

    def metadata(self) -> Dict[str, Any]:
        return {"chunk_id": self.chunk_id, "characters": len(self.text),
                "pages": sorted({value["page"] for value in self.segments}),
                "sections": list(dict.fromkeys(value["section"] for value in self.segments)),
                "block_ids": list(dict.fromkeys(value["block_id"] for value in self.segments if value["block_id"])),
                "segments": self.segments}


def document_chunks(document: ParsedDocument, max_chars: int = MAX_CHUNK_CHARS) -> List[ExtractionChunk]:
    """Pack all page characters, including separators, with parser-owned labels."""
    if max_chars < 512:
        raise ValueError("max_chars must be at least 512")
    pages = document.pages or [Page(1, document.text)]
    if not document.blocks:
        # Local import avoids the parsing/extraction initialization cycle.
        from ..parsing.pdf import build_document_blocks
        document.blocks = build_document_blocks(pages)
    by_page: Dict[int, List[Any]] = {}
    for block in document.blocks:
        by_page.setdefault(block.page, []).append(block)
    fingerprint = hashlib.sha256("\n\n".join(page.text for page in pages).encode("utf-8")).hexdigest()
    chunks: List[ExtractionChunk] = []
    segments: List[Dict[str, Any]] = []
    runs: List[Dict[str, Any]] = []
    page_text = {page.number: page.text for page in pages}
    size = 0
    current_section = "front_matter"

    def flush() -> None:
        nonlocal segments, runs, size
        if not segments:
            return
        signature = EXTRACTION_VERSION + "\0" + fingerprint + "\0" + json.dumps(segments, sort_keys=True, ensure_ascii=False)
        chunk_id = "chunk_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
        text = _render_segments(page_text, segments)
        if len(text) != size or len(text) > max_chars:
            raise RuntimeError("Extraction label accounting exceeds chunk budget")
        chunks.append(ExtractionChunk(chunk_id, text, list(segments)))
        segments, runs, size = [], [], 0

    for page in pages:
        blocks = sorted((value for value in by_page.get(page.number, [])
                         if 0 <= value.char_start < value.char_end <= len(page.text)
                         and page.text[value.char_start:value.char_end] == value.text),
                        key=lambda value: value.char_start)
        starts = sorted({0, len(page.text)} | {value.char_start for value in blocks})
        block_at = {value.char_start: value for value in blocks}
        current_block = None
        for start, end in zip(starts, starts[1:]):
            if start in block_at:
                current_block = block_at[start]
                section = str(current_block.section or current_section)
                # Preserve the heading label but pack short neighbouring
                # sections together. A heading is not another network call.
                current_section = section
            cursor = start
            while cursor < end:
                block_id = str(current_block.block_id) if current_block is not None else ""
                span = {"page": page.number, "char_start": cursor, "char_end": end,
                        "section": current_section, "block_id": block_id}
                previous = runs[-1] if runs else None
                joins_run = bool(previous and previous["page"] == page.number
                                 and previous["section"] == current_section and previous["char_end"] == cursor)
                # Adjacent same-page/section blocks need only one locator in
                # the prompt. Exact individual block spans remain in metadata.
                if joins_run:
                    reserve = len(str(end)) - len(str(previous["char_end"]))
                else:
                    reserve = len(_chunk_label(span, previous["section"] if previous else None))
                available = max_chars - size - reserve
                if available <= 0:
                    if not segments:
                        raise ValueError("Parser section label exceeds chunk character budget")
                    flush()
                    continue
                piece_end = min(end, cursor + available)
                if piece_end < end:
                    split = page.text.rfind("\n", cursor + max(1, available // 2), piece_end)
                    if split >= 0:
                        piece_end = split + 1
                piece = page.text[cursor:piece_end]
                span["char_end"] = piece_end
                segments.append(span)
                if joins_run:
                    size += len(piece) + len(str(piece_end)) - len(str(previous["char_end"]))
                    previous["char_end"] = piece_end
                else:
                    size += len(piece) + len(_chunk_label(span, previous["section"] if previous else None))
                    runs.append(dict(span))
                cursor = piece_end
                if cursor < end:
                    flush()
    flush()
    covered = sum(value["char_end"] - value["char_start"] for chunk in chunks for value in chunk.segments)
    if covered != sum(len(page.text) for page in pages):
        raise RuntimeError("Extraction chunk coverage does not match document text")
    return chunks


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _strings(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        value = str(value).strip()
        key = _key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _entities(values: List[EntityMention]) -> List[EntityMention]:
    result: List[EntityMention] = []
    indices: Dict[str, int] = {}
    for value in values:
        key = _key(value.name)
        if not key:
            continue
        if key in indices:
            prior = result[indices[key]]
            prior.description = prior.description or value.description
            prior.role = prior.role or value.role
            for name in ("principle", "statistics", "name_zh"):
                setattr(prior, name, getattr(prior, name) or getattr(value, name))
            if prior.usage_status == "mentioned" and value.usage_status != "mentioned":
                prior.usage_status = value.usage_status
        else:
            indices[key] = len(result)
            result.append(copy.deepcopy(value))
    return result


def _claims(payload: Dict[str, Any], chunk: ExtractionChunk,
            document: Optional[ParsedDocument] = None) -> List[Dict[str, Any]]:
    claims = []
    for value in payload.get("claims") or []:
        if not isinstance(value, dict) or not str(value.get("statement") or "").strip():
            continue
        evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
        entities = [{"type": str(entity["type"]), "name": str(entity["name"]).strip()}
                    for entity in value.get("entities") or []
                    if isinstance(entity, dict) and entity.get("type") in {"method", "dataset", "topic", "paper"}
                    and str(entity.get("name") or "").strip()]
        pages = chunk.metadata()["pages"]
        quote = str(evidence.get("quote") or "")
        if document is not None and quote.strip():
            matched = [page.number for page in document.pages
                       if any(span["page"] == page.number and quote in page.text[span["char_start"]:span["char_end"]]
                              for span in chunk.segments)]
            if matched:
                pages = sorted(set(matched))
        claims.append({"statement": str(value["statement"]).strip(),
                       "claim_type": str(value.get("claim_type") or "finding"),
                       "subject": str(value.get("subject") or ""), "entities": entities,
                       "attributes": dict(value["attributes"]) if isinstance(value.get("attributes"), dict) else {},
                       # Never keep a model-supplied locator or verification.
                       "evidence": {"quote": str(evidence.get("quote") or "")},
                       "extraction": {"chunk_id": chunk.chunk_id,
                                      "block_ids": chunk.metadata()["block_ids"],
                                      "pages": pages}})
    return claims


def _merge(local: List[StructuredPaper], chunks: List[ExtractionChunk], llm: Any,
           document: ParsedDocument, max_chunk_chars: int = MAX_CHUNK_CHARS) -> StructuredPaper:
    candidates = [value.title for value in local if not suspicious_title(value.title)]
    title = candidates[0] if candidates else (local[0].title if local else "Untitled Paper")
    years = list(dict.fromkeys(value.year for value in local if value.year is not None))
    paper = StructuredPaper(title=title, authors=_strings([author for value in local for author in value.authors]),
                            year=years[0] if years else None)
    for name in ("abstract", "abstract_zh", "experiment_info", "research_problem", "methodology"):
        values = _strings([str(getattr(value, name) or "") for value in local])
        setattr(paper, name, "\n\n".join(values) or (None if name == "abstract" else ""))
    for name in ("keywords", "metrics", "contributions", "findings", "limitations", "topics"):
        setattr(paper, name, _strings([item for value in local for item in getattr(value, name)]))
    paper.methods = _entities([item for value in local for item in value.methods])
    paper.datasets = _entities([item for value in local for item in value.datasets])
    paper.topic_labels_zh = {name: label for value in local for name, label in value.topic_labels_zh.items()}
    evidence_keys = set()
    for value in local:
        for original_row in value.field_evidence:
            row = dict(original_row)
            indexed = re.fullmatch(r"(metrics|contributions|findings|limitations)[.:](\d+)", str(row.get("field") or ""))
            if indexed:
                name, index = indexed.group(1), int(indexed.group(2))
                values = getattr(value, name)
                if index >= len(values):
                    # A nonexistent field index cannot support another field.
                    continue
                merged_values = getattr(paper, name)
                target = next((n for n, item in enumerate(merged_values) if _key(item) == _key(values[index])), None)
                if target is None:
                    continue
                row["field"] = name + "." + str(target)
            key = digest(row)
            if key not in evidence_keys:
                paper.field_evidence.append(row)
                evidence_keys.add(key)
    seen = set()
    for value in local:
        for claim in value.claims:
            key = (_key(claim["statement"]), _key(claim["evidence"]["quote"]))
            if key not in seen:
                seen.add(key)
                paper.claims.append(claim)
    total = sum(len(page.text) for page in document.pages) if document.pages else len(document.text)
    paper.extraction_meta = {"version": EXTRACTION_VERSION, "provider": str(getattr(llm, "provider", "unknown")),
                             "model": str(getattr(llm, "model", "unknown")), "chunk_count": len(chunks),
                             "max_chunk_chars": max_chunk_chars, "source_characters": total,
                             "covered_characters": sum(span["char_end"] - span["char_start"] for chunk in chunks for span in chunk.segments),
                             "coverage": sum(span["char_end"] - span["char_start"] for chunk in chunks for span in chunk.segments) / max(1, total),
                             "chunks": [chunk.metadata() for chunk in chunks],
                             "title_candidates": _strings([value.title for value in local]), "year_candidates": years,
                             "document_quality": document.quality}
    return paper


def _bounded_option(options: Dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(options.get(name, default))))
    except (TypeError, ValueError):
        return default


def _chunk_label(segment: Dict[str, Any], previous_section: Optional[str] = None) -> str:
    section = str(segment["section"])
    heading = "\n[SECTION " + section + "]" if previous_section is None or section != previous_section else ""
    return heading + "\n[PAGE {page}; CHARS {char_start}:{char_end}]\n".format(**segment)


def _render_segments(pages: Dict[int, str], segments: List[Dict[str, Any]]) -> str:
    """Compact labels for contiguous runs; never rewrite a source character."""
    runs: List[Dict[str, Any]] = []
    for span in segments:
        if (runs and runs[-1]["page"] == span["page"] and runs[-1]["section"] == span["section"]
                and runs[-1]["char_end"] == span["char_start"]):
            runs[-1]["char_end"] = span["char_end"]
        else:
            runs.append(dict(span))
    result = []
    previous_section = None
    for run in runs:
        result.append(_chunk_label(run, previous_section))
        result.append(pages[run["page"]][run["char_start"]:run["char_end"]])
        previous_section = str(run["section"])
    return "".join(result)


def _split_chunk(document: ParsedDocument, chunk: ExtractionChunk, minimum: int) -> List[ExtractionChunk]:
    """Bisect original characters, not annotated labels or reconstructed prose."""
    length = sum(value["char_end"] - value["char_start"] for value in chunk.segments)
    if length < 2 * minimum:
        return []
    target = length // 2
    halves: List[List[Dict[str, Any]]] = [[], []]
    covered = 0
    for value in chunk.segments:
        span = value["char_end"] - value["char_start"]
        if covered + span <= target:
            halves[0].append(dict(value))
        elif covered >= target:
            halves[1].append(dict(value))
        else:
            middle = value["char_start"] + target - covered
            halves[0].append({**value, "char_end": middle})
            halves[1].append({**value, "char_start": middle})
        covered += span
    pages = {page.number: page.text for page in document.pages or [Page(1, document.text)]}
    result = []
    for spans in halves:
        identifier = "chunk_" + digest({"version": EXTRACTION_VERSION, "parent": chunk.chunk_id, "segments": spans})[:24]
        text = _render_segments(pages, spans)
        result.append(ExtractionChunk(identifier, text, spans))
    return result


def _valid_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or not any(key in payload for key in (
            "title", "abstract", "abstract_zh", "experiment_info", "research_problem", "methodology", "methods", "datasets", "findings", "claims", "topics", "metrics")):
        return False
    # A syntactically valid JSON string in place of arrays is not a usable
    # extraction. Validate before both checkpointing and merging.
    for name in ("authors", "keywords", "methods", "datasets", "metrics", "contributions", "findings", "limitations", "topics", "claims", "evidence", "field_evidence"):
        if name in payload and payload[name] is not None and not isinstance(payload[name], list):
            return False
    for name in ("title", "abstract", "abstract_zh", "experiment_info", "research_problem", "methodology"):
        if name in payload and payload[name] is not None and not isinstance(payload[name], str):
            return False
    try:
        digest(payload)
        StructuredPaper.from_dict(payload)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


class _ChunkRunner:
    def __init__(self, document: ParsedDocument, llm: Any, stage: str, purpose: str,
                 checkpoint_dir: Optional[Path], options: Dict[str, Any],
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]], total: int,
                 language: str = "zh-CN", system_override: Optional[str] = None):
        self.document, self.llm, self.stage, self.purpose = document, llm, stage, purpose
        self.checkpoints = ChunkCheckpoints(checkpoint_dir)
        self.options, self.callback = options, progress_callback
        self.language, self.system_override = language, system_override
        self.total, self.completed, self.cached = total, 0, 0
        self.minimum = _bounded_option(options, "min_chunk_chars", 2000, 512, MAX_CHUNK_CHARS)
        self.max_depth = _bounded_option(options, "max_split_depth", 2, 0, 4)
        self.started = time.monotonic()
        pages = document.pages or [Page(1, document.text)]
        self.source_fingerprint = digest([{"number": page.number, "text": page.text} for page in pages])
        self.parser_fingerprint = digest({"quality_version": document.quality.get("parser_version"),
            "blocks": [{"block_id": value.block_id, "parser_version": value.parser_version,
                        "page": value.page, "char_start": value.char_start, "char_end": value.char_end,
                        "section": value.section, "type": value.block_type, "bbox": value.bbox}
                       for value in document.blocks]})
        self.client_identity = {name: getattr(llm, name, None) for name in (
            "provider", "model", "max_output_tokens", "json_max_retries", "max_retries", "timeout_seconds")}
        self.client_identity["client_type"] = type(llm).__module__ + "." + type(llm).__qualname__
        self.client_identity["base_url_hash"] = digest(str(getattr(llm, "base_url", "")))
        self.client_identity["requested_temperature"] = 0.1 if stage == "analysis" else 0
        if hasattr(llm, "_effective_temperature"):
            self.client_identity["effective_temperature"] = llm._effective_temperature(self.client_identity["requested_temperature"])

    def emit(self, chunk: ExtractionChunk, status: str, elapsed_ms: int = 0, **extra: Any) -> None:
        if self.callback is not None:
            current = self.completed if status in {"completed", "cached"} else self.completed + 1
            self.callback({"stage": self.stage, "status": status, "current": min(current, self.total),
                           "total": self.total, "completed": self.completed, "cached": self.cached,
                           "chunk_id": chunk.chunk_id, "elapsed_ms": elapsed_ms,
                           "stage_elapsed_ms": int((time.monotonic() - self.started) * 1000), **extra})

    def identity(self, chunk: ExtractionChunk, system: str, user: str, analysis: str) -> Dict[str, Any]:
        return {"stage": self.stage, "source_fingerprint": self.source_fingerprint,
                "parser_fingerprint": self.parser_fingerprint, "extraction_version": EXTRACTION_VERSION,
                "chunk_id": chunk.chunk_id, "chunk_hash": digest(chunk.text), "segments_hash": digest(chunk.segments),
                "system_prompt_hash": digest(system), "user_prompt_hash": digest(user),
                "purpose_hash": digest(self.purpose), "analysis_hash": digest(analysis),
                "language": self.language, "focus": self.options.get("focus", ""),
                "operation": self.options.get("operation", "extract"),
                "client": self.client_identity,
                "chunk_options": {name: self.options.get(name, default) for name, default in (
                    ("max_chunk_chars", MAX_CHUNK_CHARS), ("min_chunk_chars", 2000), ("max_split_depth", 2))}}

    def run(self, chunk: ExtractionChunk, analysis: str = "", depth: int = 0) -> List[Tuple[ExtractionChunk, Any]]:
        if self.stage == "analysis":
            system, user = PAPER_ANALYSIS_SYSTEM + extraction_language_rules(self.language), paper_analysis_prompt(chunk.text, self.purpose)
        else:
            system = (self.system_override or PAPER_EXTRACTION_SYSTEM) + extraction_language_rules(self.language)
            user = paper_extraction_prompt(chunk.text, analysis=analysis, purpose=self.purpose)
        identity = self.identity(chunk, system, user, analysis)
        children = _split_chunk(self.document, chunk, self.minimum) if depth < self.max_depth else []

        def valid_entry(entry: Dict[str, Any]) -> bool:
            if entry.get("type") == "split":
                return bool(children) and entry.get("children") == [value.chunk_id for value in children]
            if entry.get("type") != "result":
                return False
            result = entry.get("payload")
            return bool(isinstance(result, str) and result.strip()) if self.stage == "analysis" else _valid_payload(result)

        entry, cache_state = self.checkpoints.read(self.stage, identity, valid_entry) if not self.options.get("force_refresh") else (None, "miss")
        if cache_state == "invalid":
            self.emit(chunk, "cache_invalid", cache_reason="corrupt_or_incompatible")
        if entry is not None:
            if entry["type"] == "split":
                self.total += 1
                self.emit(chunk, "resuming_split", reason=entry.get("reason", "truncated"))
                return [value for child in children for value in self.run(child, analysis, depth + 1)]
            self.completed += 1
            self.cached += 1
            self.emit(chunk, "cached", response_meta=safe_response_meta(entry.get("response_meta")))
            return [(chunk, entry["payload"])]

        self.emit(chunk, "started")
        started = time.monotonic()
        try:
            if self.stage == "analysis":
                result = self.llm.generate(system, user, temperature=0.1)
                if not isinstance(result, str) or not result.strip():
                    raise RuntimeError("LLM paper analysis returned empty content")
                result = result.strip()
            else:
                result = self.llm.structured(system, user)
                if not _valid_payload(result):
                    raise RuntimeError("LLM returned invalid paper extraction JSON")
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            response_meta = safe_response_meta(getattr(self.llm, "last_response_meta", {}))
            # Transport errors/timeouts never multiply into split requests.
            # The client has already spent its bounded JSON retry budget.
            reason = getattr(exc, "reason", "")
            if reason in {"truncated", "invalid_json"} and children:
                self.checkpoints.write(self.stage, identity,
                    {"type": "split", "children": [value.chunk_id for value in children], "reason": reason})
                self.total += 1
                self.emit(chunk, "split", elapsed, reason=reason, response_meta=response_meta)
                return [value for child in children for value in self.run(child, analysis, depth + 1)]
            self.emit(chunk, "failed", elapsed, reason=reason or "request_failed", response_meta=response_meta)
            raise RuntimeError("LLM paper " + self.stage + " failed in " + chunk.chunk_id + ": " + str(exc)) from exc
        elapsed = int((time.monotonic() - started) * 1000)
        response_meta = safe_response_meta(getattr(self.llm, "last_response_meta", {}))
        self.checkpoints.write(self.stage, identity, {"type": "result", "payload": result, "response_meta": response_meta})
        self.completed += 1
        self.emit(chunk, "completed", elapsed, response_meta=response_meta)
        return [(chunk, result)]


def _run_options(options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    value = dict(options or {})
    value["max_chunk_chars"] = _bounded_option(value, "max_chunk_chars", MAX_CHUNK_CHARS, 512, MAX_CHUNK_CHARS)
    value["min_chunk_chars"] = _bounded_option(value, "min_chunk_chars", 2000, 512, MAX_CHUNK_CHARS)
    value["max_split_depth"] = _bounded_option(value, "max_split_depth", 2, 0, 4)
    return value


def analyze_paper(document: ParsedDocument, llm: LLMClient, purpose: str = "", language: str = "zh-CN",
                  checkpoint_dir: Optional[Path] = None, options: Optional[Dict[str, Any]] = None,
                  progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    if not llm.enabled:
        raise RuntimeError("LLM is required for high-quality paper analysis")
    ensure_document_quality(document, str(document.quality.get("source_kind") or "text"))
    opts = _run_options(options)
    chunks = document_chunks(document, opts["max_chunk_chars"])
    runner = _ChunkRunner(document, llm, "analysis", purpose, checkpoint_dir, opts, progress_callback, len(chunks), language)
    notes = []
    for chunk in chunks:
        values = runner.run(chunk)
        # Keep the root marker even after a split so the second stage cannot
        # accidentally consume another root chunk's analysis notes.
        text = "\n\n".join(("[PART " + child.chunk_id + "]\n" if len(values) > 1 else "") + note
                           for child, note in values)
        notes.append("<!-- " + chunk.chunk_id + " -->\n" + text)
    return "\n\n".join(notes)


def _analysis_for_chunk(analysis: str, chunk: ExtractionChunk) -> str:
    marker = "<!-- " + chunk.chunk_id + " -->"
    if marker in analysis:
        remaining = analysis.split(marker, 1)[1]
        return remaining.split("<!-- chunk_", 1)[0].strip()
    # Externally supplied notes stay bounded and are context only. The source
    # itself is never truncated here or in the prompt formatter.
    return analysis[:4000]


def extract_paper(document: ParsedDocument, llm: Optional[LLMClient] = None,
                  analysis: str = "", purpose: str = "", language: str = "zh-CN", checkpoint_dir: Optional[Path] = None,
                  options: Optional[Dict[str, Any]] = None,
                  progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> StructuredPaper:
    return _extract_chunks(document, llm, analysis, purpose, language, checkpoint_dir, options, progress_callback)


def _extract_chunks(document: ParsedDocument, llm: Any, analysis: str, purpose: str, language: str,
                    checkpoint_dir: Optional[Path], options: Optional[Dict[str, Any]],
                    progress_callback: Optional[Callable[[Dict[str, Any]], None]],
                    system: Optional[str] = None, baseline_title: str = "", focus: str = "",
                    validate_quality: bool = True) -> StructuredPaper:
    if llm is None or not llm.enabled:
        raise RuntimeError("LLM is required for paper extraction; configure the selected provider's API key")
    ensure_document_quality(document, str(document.quality.get("source_kind") or "text"))
    opts = _run_options(options)
    planned = document_chunks(document, opts["max_chunk_chars"])
    scoped = False
    if focus:
        planned, scoped = _focused_chunks(document, planned, focus)
        opts.update({"focus": focus, "operation": "supplement"})
    elif system == DEEP_EXTRACTION_SYSTEM:
        opts["operation"] = "enrich"
    runner = _ChunkRunner(document, llm, "extraction", purpose, checkpoint_dir, opts, progress_callback,
                          len(planned), language, system)
    chunks: List[ExtractionChunk] = []
    local: List[StructuredPaper] = []
    for chunk in planned:
        for child, payload in runner.run(chunk, _analysis_for_chunk(analysis, chunk)):
            partial = StructuredPaper.from_dict(payload)
            if suspicious_title(partial.title) and baseline_title:
                partial.title = baseline_title
            partial.claims = _claims(payload, child, document)
            partial.field_evidence = verify_field_evidence(document, payload.get("evidence"))
            local.append(partial)
            chunks.append(child)
    merged = _merge(local, chunks, llm, document, opts["max_chunk_chars"])
    merged.extraction_meta.update({"planned_chunk_count": len(planned), "completed_chunk_count": runner.completed,
                                  "cached_chunk_count": runner.cached, "elapsed_ms": int((time.monotonic() - runner.started) * 1000)})
    merged.extraction_meta.update({"language": language, "focus": focus,
                                  "scope": "reliable_sections" if scoped else "full_document"})
    if validate_quality:
        ensure_paper_quality(merged)
    return merged


FOCUSED_FIELDS = {
    "method": ("methods", "methodology", "contributions"),
    "experiment": ("datasets", "experiment_info", "metrics", "findings"),
    "overview": ("abstract_zh", "research_problem", "topics", "topic_labels_zh"),
}


def _focused_chunks(document: ParsedDocument, planned: List[ExtractionChunk], focus: str) -> Tuple[List[ExtractionChunk], bool]:
    patterns = {
        "method": r"method|approach|proposed|方法|模型|框架",
        "experiment": r"experiment|result|evaluation|实验|结果|评估",
        "overview": r"abstract|introduction|discussion|conclusion|摘要|引言|绪论|讨论|结论",
    }
    expression = re.compile(patterns[focus], re.I)
    # Only parser-detected section boundaries justify narrowing the scope.
    if not any(expression.search(str(span["section"])) for chunk in planned for span in chunk.segments):
        return planned, False
    pages = {page.number: page.text for page in document.pages or [Page(1, document.text)]}
    selected = []
    for chunk in planned:
        spans = [dict(span) for span in chunk.segments
                 if span["section"] == "front_matter" or expression.search(str(span["section"]))]
        if spans:
            key = digest({"version": EXTRACTION_VERSION, "parent": chunk.chunk_id, "focus": focus, "segments": spans})
            selected.append(ExtractionChunk("chunk_" + key[:24], _render_segments(pages, spans), spans))
    return selected, True


def enrich_paper(document: ParsedDocument, baseline: StructuredPaper, llm: LLMClient,
                 analysis: str = "", purpose: str = "", quality_mode: str = "standard",
                 source_path: Optional[Path] = None, language: str = "zh-CN",
                 checkpoint_dir: Optional[Path] = None, options: Optional[Dict[str, Any]] = None,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> StructuredPaper:
    """Deepen an offline record through the same recoverable full-text runner."""
    if llm is None or not llm.enabled:
        raise RuntimeError("Deep extraction requires a configured model API key")
    try:
        deep = _extract_chunks(document, llm, analysis, purpose, language, checkpoint_dir, options,
                               progress_callback, DEEP_EXTRACTION_SYSTEM, baseline.title, validate_quality=False)
    except Exception as exc:
        raise RuntimeError("Deep paper extraction failed: " + str(exc)) from exc
    result = copy.deepcopy(baseline)
    uncertain = suspicious_title(result.title)
    if source_path is not None and source_path.suffix.lower() in {".txt", ".md", ".markdown"}:
        explicit = re.search(r"(?im)^\s*(?:title|题目|论文标题)\s*[:：]", document.text[:2000])
        marked = source_path.suffix.lower() in {".md", ".markdown"} and re.search(r"(?m)^#\s+\S", document.text[:2000])
        uncertain = uncertain or not (explicit or marked)
    if uncertain and not suspicious_title(deep.title):
        result.title = deep.title
    result.authors = result.authors or deep.authors
    result.year = result.year or deep.year
    result.abstract = result.abstract or deep.abstract
    # Author keywords are immutable basic metadata, not model suggestions.
    for name in ("abstract_zh", "research_problem", "methodology", "experiment_info", "methods", "metrics",
                 "contributions", "findings", "limitations", "topics", "topic_labels_zh"):
        value = getattr(deep, name)
        if value:
            setattr(result, name, value)
    result.datasets = _entities(deep.datasets + result.datasets)
    result.claims, result.field_evidence = deep.claims, deep.field_evidence
    result.extraction_meta = deep.extraction_meta
    result.extraction_mode = "high" if quality_mode == "high" else "deep"
    ensure_paper_quality(result)
    return result


def supplement_paper(document: ParsedDocument, previous: StructuredPaper, llm: LLMClient, focus: str,
                     language: str = "zh-CN", checkpoint_dir: Optional[Path] = None,
                     options: Optional[Dict[str, Any]] = None,
                     progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> StructuredPaper:
    """Update only an explicit facet, retaining all other facts and bibliography."""
    if focus not in FOCUSED_FIELDS:
        raise ValueError("focus must be method, experiment or overview")
    fields = FOCUSED_FIELDS[focus]
    system = DEEP_EXTRACTION_SYSTEM + "\nFOCUSED UPDATE: Return only these top-level fields: " + ", ".join(fields) + ", plus evidence and claims. Do not infer other sections."
    patch = _extract_chunks(document, llm, "", "", language, checkpoint_dir, options, progress_callback,
                            system, previous.title, focus, validate_quality=False)
    result = copy.deepcopy(previous)
    for name in fields:
        value = getattr(patch, name)
        if value:
            setattr(result, name, _entities(value + result.datasets) if name == "datasets" else value)
    result.field_evidence = [row for row in result.field_evidence
                             if not str(row.get("field") or "").startswith(fields)] + [
                                 row for row in patch.field_evidence if str(row.get("field") or "").startswith(fields)]
    allowed = {"method": {"method", "contribution", "method_usage"},
               "experiment": {"experiment_result", "finding", "dataset_usage"},
               "overview": {"research_problem", "abstract"}}[focus]
    result.claims = [row for row in result.claims if row.get("claim_type") not in allowed] + [
        row for row in patch.claims if row.get("claim_type") in allowed]
    result.extraction_meta = {**previous.extraction_meta, "last_supplement": patch.extraction_meta}
    ensure_paper_quality(result)
    return result
