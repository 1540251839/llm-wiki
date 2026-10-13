from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union, Optional

from .config import Config
from .extraction.paper import analyze_paper, extract_paper, enrich_paper, supplement_paper
from .extraction.basic import extract_basic
from .extraction.quality import QualityGateError
from .graph import extract_graph
from .index.database import Database
from .llm.client import LLMClient
from .models import ParsedDocument, StructuredPaper
from .parsing.pdf import parse_file
from .parsing.grobid import header_metadata
from .qa.engine import answer, answer_with_metadata, answer_events
from .qa.retrieval import retrieve_hybrid
from .retrieval.search import search
from .retrieval.vector import sync_vector_index
from .wiki.manager import WikiManager
from .verification import ClaimsService


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def initialize(config: Config) -> Dict[str, str]:
    directories = {
        "raw": config.root / "raw" / "papers",
        "wiki": config.wiki_root,
        "schema": config.root / "schema",
        "data": config.db_path.parent,
        "analyses": config.root / "data" / "analyses",
        "trash": config.root / "data" / "trash",
        "logs": config.root / "logs",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    for directory in ("papers", "methods", "datasets", "topics", "synthesis"):
        (config.wiki_root / directory).mkdir(parents=True, exist_ok=True)
    _write_schemas(config.root / "schema")
    purpose_path = config.root / "purpose.md"
    if not purpose_path.exists():
        purpose_path.write_text(
            "# ResearchWiki Purpose\n\n"
            "将研究资料编译为可追溯、可检索、可由原始来源复核的本地知识层。\n",
            encoding="utf-8",
        )
    env_path = config.root / ".env"
    if not env_path.exists():
        env_path.write_text(
            "# Server-side keys (this file is gitignored — never commit it)\n"
            "DEEPSEEK_API_KEY=\n"
            "# EMBEDDING_API_KEY=\n",
            encoding="utf-8",
        )
    Database(config.db_path).close()
    return {key: str(value) for key, value in directories.items()}


def _write_schemas(directory: Path) -> None:
    schemas = {
        "paper.md": "# Paper\n\nGenerated sections are managed by ResearchWiki. Keep personal notes between the user-notes markers.\n",
        "method.md": "# Method\n\nA method page aggregates usage across papers.\n",
        "dataset.md": "# Dataset\n\nA dataset page aggregates usage across papers.\n",
        "topic.md": "# Topic\n\nA topic page aggregates related papers and entities.\n",
    }
    for name, content in schemas.items():
        path = directory / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raw_copy(config: Config, source: Path, file_hash: str) -> Path:
    """Persist an immutable source by content identity, independent of LLM output."""
    suffix = source.suffix.lower() or ".bin"
    target = config.root / "raw" / "papers" / (file_hash + suffix)
    if source.resolve() != target.resolve():
        if not target.exists():
            shutil.copy2(source, target)
    return target


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _purpose(config: Config) -> str:
    path = config.root / "purpose.md"
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _create_quality_reviews(db: Database, source_id: str, paper: StructuredPaper) -> None:
    title = paper.title.casefold().strip()
    if "contents lists available" in title or title.startswith("information sciences ") or title == "untitled paper":
        db.add_review(
            "suspicious_title",
            "抽取标题疑似期刊页眉或占位标题，请对照原文核验。",
            source_id=source_id,
            severity="error",
            details={"title": paper.title},
        )
    if paper.extraction_mode != "basic" and not paper.abstract and not paper.methods and not paper.datasets and not paper.topics:
        db.add_review(
            "sparse_extraction",
            "结构化抽取过于稀疏，建议使用高质量模式重新处理。",
            source_id=source_id,
            details={"title": paper.title},
        )
    if paper.extraction_mode != "basic" and not paper.limitations:
        db.add_review(
            "missing_limitations",
            "未抽取到局限性；这可能是论文未明确说明，也可能需要人工复核。",
            source_id=source_id,
            severity="info",
            details={"title": paper.title},
        )


def _audit_existing_reviews(config: Config, db: Database) -> int:
    before = len(db.list_reviews(status="OPEN", limit=10000))
    papers = db.list_papers(page=1, page_size=10000)["items"]
    paper_titles = {str(value.get("title") or "").casefold() for value in papers}
    for record in papers:
        source_id = str(record.get("id") or "")
        extraction_path = config.root / "data" / "extractions" / (source_id + ".json")
        if not extraction_path.exists():
            db.add_review(
                "missing_extraction",
                "资料已登记，但结构化抽取文件缺失。",
                source_id=source_id,
                severity="error",
            )
            continue
        try:
            paper = StructuredPaper.from_dict(json.loads(extraction_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            db.add_review(
                "invalid_extraction",
                "结构化抽取文件无法读取或格式无效。",
                source_id=source_id,
                severity="error",
            )
            continue
        _create_quality_reviews(db, source_id, paper)
    for page in db.list_wiki_pages(page_type="paper", limit=10000):
        if str(page.get("title") or "").casefold() in paper_titles:
            continue
        db.add_review(
            "orphan_paper_page",
            "该论文页面没有对应的活动资料记录；请确认是历史残留还是人工页面。",
            page_id=int(page["id"]),
            page_path=str(page.get("path") or ""),
        )
    after = len(db.list_reviews(status="OPEN", limit=10000))
    return max(0, after - before)


def _job_stage(db: Database, job_id: Optional[int], stage: str) -> None:
    if job_id is not None:
        db.update_ingest_job(job_id, stage=stage)


def _refresh_evidence_locators(
    config: Config,
    db: Database,
    current_source_id: str = "",
    current_pages: Optional[List[Any]] = None,
) -> int:
    updated = 0
    parsed_sources = {}
    verified_claims = {}
    db.conn.execute("DELETE FROM page_evidence")
    for record in db.list_papers(page=1, page_size=10000)["items"]:
        source_id = str(record.get("id") or "")
        if not source_id:
            continue
        if source_id == current_source_id and current_pages is not None:
            pages = current_pages
        else:
            source_path = Path(str(record.get("raw_path") or record.get("source_path") or ""))
            if not source_path.is_file():
                continue
            if _hash_file(source_path) != source_id:
                continue
            try:
                pages = parse_file(source_path).pages
            except Exception:
                continue
        cache = config.root / "data" / "extractions" / (source_id + ".json")
        if cache.is_file():
            try:
                extracted = StructuredPaper.from_dict(json.loads(cache.read_text(encoding="utf-8")))
                verified = ClaimsService(config).prepare(source_id, extracted, ParsedDocument(text="", pages=list(pages)), persist=False)
                verified_claims[source_id] = {claim["claim_id"]: claim for claim in verified["claims"]
                                              if claim["verification"]["status"] == "supported"}
            except (OSError, ValueError, TypeError):
                verified_claims[source_id] = {}
        updated += db.enrich_source_evidence(source_id, pages)
        parsed_sources[source_id] = {page.number: page.text for page in pages}
    # A compiler manifest holds the precise spans shown in compiled Markdown.
    # Re-validate against the parsed source before using them in QA/graph so all
    # consumers see the same evidence rather than independently guessed snippets.
    manifest_path = config.wiki_root / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("pages", []):
                page_path = (config.root / str(item.get("path") or "")).resolve()
                if not page_path.is_relative_to(config.wiki_root.resolve()):
                    continue
                row = db.conn.execute("SELECT id FROM wiki_pages WHERE path=?", (str(page_path),)).fetchone()
                if not row:
                    continue
                for span in item.get("evidence", []):
                    source_id = str(span.get("source_id") or "")
                    text = parsed_sources.get(source_id, {}).get(int(span.get("page_number") or 0), "")
                    start, end = int(span.get("char_start") or 0), int(span.get("char_end") or 0)
                    if not (0 <= start < end <= len(text)) or text[start:end] != span.get("evidence_text"):
                        continue
                    if span.get("claim_id"):
                        verified = verified_claims.get(source_id, {}).get(span["claim_id"])
                        current = (verified or {}).get("evidence", {})
                        if not verified or (current.get("page_number"), current.get("char_start"), current.get("char_end"), current.get("quote")) != (span.get("page_number"), start, end, text[start:end]):
                            continue
                        db.conn.execute("""INSERT OR REPLACE INTO page_evidence(
                            page_id,claim_id,source_id,source_path,locator,page_number,paragraph,char_start,char_end,
                            evidence_text,confidence,verification_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (row["id"], span["claim_id"], source_id, span.get("source_path"), span.get("locator"),
                             span.get("page_number"), span.get("paragraph"), start, end, text[start:end],
                             "verified_quote", "supported"))
                    db.conn.execute("""UPDATE page_sources SET locator=?,page_number=?,char_start=?,char_end=?,
                        paragraph=?,confidence=?,evidence_text=?,section=? WHERE page_id=? AND source_id=?""",
                        (span.get("locator"), span.get("page_number"), start, end, span.get("paragraph"),
                         span.get("confidence"), text[start:end], "compiled evidence", row["id"], source_id))
            db.conn.commit()
        except (OSError, ValueError, TypeError):
            # Legacy Wiki indexes still work if an optional manifest is invalid.
            pass
    return updated


def ingest(
    config: Config,
    source: Union[str, Path],
    resume: bool = False,
    llm_client: Optional[LLMClient] = None,
    quality_mode: Optional[str] = None,
    job_id: Optional[int] = None,
    force_refresh: bool = False,
    focus: str = "",
    operation: str = "",
) -> Dict[str, Any]:
    initialize(config)
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    file_hash = _hash_file(source_path)
    db = Database(config.db_path)
    attempt_started = time.monotonic()
    stage_started = attempt_started
    active_stage = ""
    stage_timings: Dict[str, float] = {}
    model_totals: Dict[str, Dict[str, int]] = {}
    previous_record = None
    previous_cache: Dict[Path, Optional[bytes]] = {}
    publication: Optional[Dict[Path, bytes]] = None

    def timings_snapshot() -> Dict[str, float]:
        snapshot = dict(stage_timings)
        if active_stage:
            snapshot[active_stage] = round(time.monotonic() - stage_started, 3)
        snapshot["total_seconds"] = round(time.monotonic() - attempt_started, 3)
        return snapshot

    def mark_stage(stage: str) -> None:
        nonlocal active_stage, stage_started
        if active_stage:
            stage_timings[active_stage] = round(time.monotonic() - stage_started, 3)
        active_stage, stage_started = stage, time.monotonic()
        if job_id is not None:
            db.update_ingest_job(job_id, stage=stage, stage_timings=timings_snapshot())

    def progress_callback(event: Dict[str, Any]) -> None:
        # Persist only operational metadata, never prompts, responses or keys.
        safe = {key: event[key] for key in ("stage", "current", "total", "completed", "cached", "chunk_id", "status",
                                            "elapsed_ms", "split_depth", "reason", "namespace", "request_count") if key in event}
        metadata = event.get("response_meta") or event.get("last_response_meta") or event.get("metadata") or event.get("llm") or {}
        safe["model"] = {key: metadata[key] for key in ("finish_reason", "elapsed_ms", "elapsed_seconds", "total_elapsed_ms", "requests", "request_count",
                                                       "http_attempts", "request_attempts", "json_attempts", "json_retries", "response_chars") if key in metadata}
        usage = metadata.get("usage") or {}
        safe["model"]["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}
        accumulated_usage = metadata.get("usage_totals") or usage
        safe["model"]["usage_totals"] = {key: accumulated_usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in accumulated_usage}
        stage_totals = model_totals.setdefault(str(event.get("stage") or "extraction"),
                                               {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached": 0})
        if event.get("status") == "cached":
            stage_totals["cached"] += 1
        elif event.get("status") in {"completed", "failed", "split"}:
            stage_totals["requests"] += int(metadata.get("requests") or metadata.get("request_attempts") or 0)
            for key in ("prompt_tokens", "completion_tokens"):
                stage_totals[key] += int(accumulated_usage.get(key) or 0)
        safe["model_totals"] = {stage: dict(totals) for stage, totals in model_totals.items()}
        safe["attempt_elapsed_seconds"] = round(time.monotonic() - attempt_started, 3)
        if job_id is not None:
            db.update_ingest_job(job_id, progress=safe, stage_timings=timings_snapshot())
        _log(config, {"event": "ingest_chunk", "paper_id": file_hash, "job_id": job_id, **safe})

    try:
        mode = (quality_mode or ("standard" if llm_client is not None else config.quality_mode)).strip().lower()
        if mode not in {"basic", "standard", "high"}:
            raise ValueError("quality_mode must be basic, standard or high")
        if focus and (focus not in {"method", "experiment", "overview"} or mode == "basic"):
            raise ValueError("Focused extraction requires a deep mode and a valid focus")
        duplicate = db.find_by_hash(file_hash)
        if duplicate and duplicate["status"] == "INDEXED" and not resume:
            return {"status": "already_exists", "title": duplicate["title"], "paper_path": None, "entity_paths": []}
        if duplicate and duplicate["status"] == "INDEXED":
            previous_record = db.get_paper(file_hash)
        structured_path = config.root / "data" / "extractions" / (file_hash + ".json")
        for cached in (structured_path, config.root / "data" / "claims" / (file_hash + ".json"),
                       config.root / "data" / "analyses" / (file_hash + ".md")):
            previous_cache[cached] = cached.read_bytes() if cached.is_file() else None
        if focus and previous_cache[structured_path] is None:
            raise ValueError("Focused extraction requires an existing extraction")
        mark_stage("COPY_SOURCE")
        raw_path = _raw_copy(config, source_path, file_hash)
        if _hash_file(raw_path) != file_hash:
            raise RuntimeError("Immutable source hash mismatch; refusing extraction")
        db.register_source(
            file_hash,
            str(source_path),
            str(raw_path),
            mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
            source_path.stat().st_size,
        )
        db.upsert_paper(
            file_hash,
            source_path.stem,
            [],
            None,
            str(source_path),
            str(raw_path),
            "UPLOADED",
        )
        mark_stage("PARSE")
        document = parse_file(source_path)
        indexed_source_chunks = db.index_source_chunks(file_hash, document.pages)
        db.update_paper_status(file_hash, "EXTRACTING")
        base = extract_basic(document, source_path=source_path, workspace_root=config.root)
        header = header_metadata(source_path, str(config.parser.get("grobid_url") or ""))
        if header.get("title") and (base.title == "Untitled Paper" or source_path.suffix.lower() == ".pdf"):
            base.title = header["title"]
        base.authors = header.get("authors") or base.authors
        base.abstract = header.get("abstract") or base.abstract
        base.keywords = header.get("keywords") or base.keywords
        if mode == "basic":
            paper = base
            paper.claims = []
        else:
            llm = llm_client or LLMClient(config.llm)
            checkpoint_dir = config.root / "data" / "ingest_checkpoints" / file_hash
            extraction_options = {**config.ingestion, "force_refresh": force_refresh}
            previous = None
            if previous_cache[structured_path] is not None:
                previous = StructuredPaper.from_dict(json.loads(previous_cache[structured_path].decode("utf-8")))
                if previous.source_hash != file_hash:
                    raise ValueError("Previous extraction source hash does not match")
                base.title = previous.title if base.title == "Untitled Paper" else base.title
                base.authors = base.authors or previous.authors
                base.year = base.year or previous.year
                base.abstract = base.abstract or previous.abstract
                base.abstract_zh = previous.abstract_zh
                base.topic_labels_zh = dict(previous.topic_labels_zh)
                base.keywords = base.keywords or previous.keywords
            analysis = ""
            if focus:
                mark_stage("EXTRACT")
                paper = supplement_paper(document, previous, llm, focus, language=config.wiki_language,
                                         checkpoint_dir=checkpoint_dir, options=extraction_options,
                                         progress_callback=progress_callback)
            else:
                if mode == "high":
                    mark_stage("ANALYZE")
                    analysis = analyze_paper(document, llm=llm, purpose=_purpose(config),
                                             language=config.wiki_language, checkpoint_dir=checkpoint_dir,
                                             options=extraction_options, progress_callback=progress_callback)
                    analysis_path = config.root / "data" / "analyses" / (file_hash + ".md")
                    _atomic_write_text(analysis_path, analysis.rstrip() + "\\n")
                mark_stage("EXTRACT")
                paper = enrich_paper(document, baseline=base, llm=llm, analysis=analysis,
                                     purpose=_purpose(config), quality_mode=mode, source_path=source_path,
                                     language=config.wiki_language, checkpoint_dir=checkpoint_dir,
                                     options=extraction_options, progress_callback=progress_callback)
        title_duplicate = db.find_indexed_title_duplicate(paper.title, file_hash)
        if title_duplicate:
            raise RuntimeError(
                "A different document is already indexed with the extracted title '"
                + paper.title
                + "'. Review the extraction before retrying to avoid overwriting its Wiki page."
            )
        paper.source_path = str(source_path)
        paper.source_hash = file_hash
        db.upsert_paper(file_hash, paper.title, paper.authors, paper.year, str(source_path), str(raw_path), "EXTRACTED")
        structured_path = config.root / "data" / "extractions" / (file_hash + ".json")
        if force_refresh:
            paper.extraction_meta["generation_revision"] = uuid.uuid4().hex
        elif structured_path.is_file():
            try:
                previous = json.loads(structured_path.read_text(encoding="utf-8"))
                revision = (previous.get("extraction_meta") or {}).get("generation_revision")
                if revision:
                    paper.extraction_meta["generation_revision"] = revision
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        _atomic_write_text(structured_path, json.dumps(paper.to_dict(), ensure_ascii=False, indent=2) + "\n")
        _create_quality_reviews(db, file_hash, paper)
        mark_stage("VERIFY_EVIDENCE")
        ClaimsService(config).prepare(file_hash, paper, document)
        publication = {p: p.read_bytes() for p in config.wiki_root.rglob("*") if p.is_file()}
        mark_stage("COMMIT_WIKI")
        wiki = WikiManager(config.wiki_root)
        from .compilation import compile_wiki
        compiled = compile_wiki(config, source_id=file_hash, database=db)
        if compiled.get("status") != "succeeded":
            raise RuntimeError("知识编译失败：" + str(compiled.get("error") or compiled.get("diagnostics")))
        paper_path = config.root / compiled["paper_path"]
        manifest = json.loads((config.wiki_root / "manifest.json").read_text(encoding="utf-8"))
        entity_paths = [config.root / item["path"] for item in manifest["pages"]
                        if item["type"] != "paper" and file_hash in item.get("sources", [])]
        db.upsert_paper(file_hash, paper.title, paper.authors, paper.year, str(source_path), str(raw_path), "WIKI_CREATED")
        mark_stage("REBUILD_INDEX")
        stats = db.rebuild(wiki)
        stats["evidence_locators"] = _refresh_evidence_locators(
            config, db, current_source_id=file_hash, current_pages=document.pages
        )
        graph_stats = extract_graph(db, wiki_root=config.wiki_root)
        stats["graph_nodes"] = int(graph_stats["stats"]["nodes"])
        stats["graph_edges"] = int(graph_stats["stats"]["edges"])
        vector_stats = sync_vector_index(db, config.vector_config)
        db.upsert_paper(file_hash, paper.title, paper.authors, paper.year, str(source_path), str(raw_path), "INDEXED")
        if job_id is not None:
            db.update_ingest_job(job_id, status="SUCCEEDED", stage="DONE", error_message="", stage_timings=timings_snapshot())
        _log(config, {"event": "ingest", "title": paper.title, "source": str(source_path), "paper": str(paper_path), "entities": [str(p) for p in entity_paths], "quality_mode": mode, "index": stats, "vectors": vector_stats, "stage_timings": timings_snapshot(), "model_totals": model_totals, "original_text_chunks": indexed_source_chunks})
        return {"status": "indexed", "title": paper.title, "paper_path": str(paper_path), "entity_paths": [str(p) for p in entity_paths], "quality_mode": mode, "index": stats, "vectors": vector_stats, "stage_timings": timings_snapshot(), "model_totals": model_totals}
    except Exception as exc:
        # Successful chunks remain resumable; active publication is all-or-nothing.
        for cached, content in previous_cache.items():
            if content is None:
                cached.unlink(missing_ok=True)
            else:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(content)
        if publication is not None:
            for generated in config.wiki_root.rglob("*"):
                if generated.is_file() and generated not in publication:
                    generated.unlink()
            for generated, content in publication.items():
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(content)
            try:
                db.rebuild(WikiManager(config.wiki_root))
                _refresh_evidence_locators(config, db)
                extract_graph(db, wiki_root=config.wiki_root)
            except Exception:
                pass
        if previous_record:
            db.upsert_paper(file_hash, previous_record["title"], previous_record.get("authors") or [],
                            previous_record.get("year"), previous_record.get("source_path") or "",
                            previous_record.get("raw_path") or "", "INDEXED")
        if isinstance(exc, QualityGateError):
            db.add_review("quality_gate", str(exc), source_id=file_hash, severity="error",
                          details={"stage": exc.stage, "quality": exc.quality})
        if "file_hash" in locals() and not previous_record:
            try:
                failed_title = paper.title if "paper" in locals() else source_path.stem
                failed_authors = paper.authors if "paper" in locals() else []
                failed_year = paper.year if "paper" in locals() else None
                failed_raw_path = str(raw_path) if "raw_path" in locals() else ""
                db.upsert_paper(
                    file_hash,
                    failed_title,
                    failed_authors,
                    failed_year,
                    str(source_path),
                    failed_raw_path,
                    "FAILED",
                    str(exc)[:2000],
                )
            except Exception:
                pass
            if job_id is not None:
                try:
                    db.update_ingest_job(job_id, status="FAILED", stage="FAILED", error_message=str(exc), stage_timings=timings_snapshot())
                except Exception:
                    pass
            try:
                _log(
                    config,
                    {
                        "event": "ingest_failed",
                        "paper_id": file_hash,
                        "source": str(source_path),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                        "stage_timings": timings_snapshot(),
                        "model_totals": model_totals,
                    },
                )
            except Exception:
                pass
        if previous_record and job_id is not None:
            db.update_ingest_job(job_id, status="FAILED", stage="FAILED", error_message=str(exc),
                                 stage_timings=timings_snapshot())
            _log(config, {"event": "ingest_failed", "paper_id": file_hash, "job_id": job_id,
                          "error": str(exc), "stage_timings": timings_snapshot(), "model_totals": model_totals})
        raise
    finally:
        db.close()


def delete_source(config: Config, source_id: str, delete_raw: bool = True) -> Dict[str, Any]:
    """Recoverably remove a source and its generated-only Wiki contribution."""
    initialize(config)
    db = Database(config.db_path)
    try:
        paper = db.get_paper(source_id)
        source = db.get_source(source_id)
        if not paper or not source or source.get("deleted_at"):
            raise FileNotFoundError("Source not found")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trash_root = config.root / "data" / "trash" / (stamp + "-" + source_id[:12])
        wiki_result = WikiManager(config.wiki_root).remove_source(source_id, str(paper.get("title") or ""), trash_root / "wiki")
        moved_files: List[str] = []
        candidates = [
            config.root / "data" / "extractions" / (source_id + ".json"),
            config.root / "data" / "analyses" / (source_id + ".md"),
            config.root / "data" / "claims" / (source_id + ".json"),
        ]
        if re.fullmatch(r"[a-f0-9]{64}", source_id):
            candidates.append(config.root / "data" / "ingest_checkpoints" / source_id)
        if delete_raw:
            for value in (source.get("raw_path"), paper.get("source_path")):
                if not value:
                    continue
                candidate = Path(str(value)).resolve()
                try:
                    candidate.relative_to(config.root.resolve())
                except ValueError:
                    continue
                candidates.append(candidate)
        for candidate in dict.fromkeys(path.resolve() for path in candidates):
            if not (candidate.is_file() or candidate.is_dir()):
                continue
            try:
                relative = candidate.relative_to(config.root.resolve())
            except ValueError:
                continue
            # Claims and extraction share a basename; preserve their directories
            # so recoverable deletion cannot overwrite either JSON with the other.
            target = trash_root / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(target))
            moved_files.append(str(candidate))
        _atomic_write_text(
            trash_root / "manifest.json",
            json.dumps(
                {
                    "deleted_at": utc_now(),
                    "source": source,
                    "paper": paper,
                    "wiki": wiki_result,
                    "moved_files": moved_files,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        db.mark_source_deleted(source_id)
        db.delete_source_chunks(source_id)
        if (config.wiki_root / "manifest.json").exists():
            from .compilation import compile_wiki
            reconciled = compile_wiki(config, database=db)
            if reconciled.get("status") != "succeeded":
                # Never serve stale text from a deleted source when an unrelated
                # manual collision prevents recompilation of a shared page.
                affected = db.conn.execute("SELECT page_id FROM page_sources WHERE source_id=?", (source_id,)).fetchall()
                for row in affected:
                    db.conn.execute("DELETE FROM chunks WHERE page_id=?", (row["page_id"],))
                    db.conn.execute("DELETE FROM embeddings WHERE page_id=?", (row["page_id"],))
                    if db.fts_available:
                        db.conn.execute("DELETE FROM wiki_fts WHERE page_id=?", (row["page_id"],))
                db.conn.commit()
                raise RuntimeError("资料已移入回收区，但共享页重编译失败。受影响页已暂停检索，请处理编译诊断后重建。")
        stats = db.rebuild(WikiManager(config.wiki_root))
        _refresh_evidence_locators(config, db)
        extract_graph(db, wiki_root=config.wiki_root)
        sync_vector_index(db, config.vector_config)
        _log(config, {
            "event": "source_deleted",
            "source_id": source_id,
            "trash": str(trash_root),
            "wiki": wiki_result,
            "files": moved_files,
            "index": stats,
        })
        return {
            "source_id": source_id,
            "status": "deleted",
            "recoverable_from": str(trash_root),
            "wiki": wiki_result,
            "files": moved_files,
            "index": stats,
        }
    finally:
        db.close()


def rebuild(config: Config) -> Dict[str, int]:
    initialize(config)
    db = Database(config.db_path)
    try:
        paused = []
        if (config.wiki_root / "manifest.json").is_file():
            from .compilation import CompilationService
            service = CompilationService(config, database=db)
            manifest = service.manifest()
            preview = service.inventory()
            if preview.get("status") != "ready" or preview.get("input_digest") != manifest.get("input_digest"):
                paused = [config.root / page["path"] for page in manifest.get("pages", [])]
                db.add_review("stale_compilation", "原件、抽取或审核状态已变化；旧编译页已暂停检索，请先重建知识层。")
        stats = db.rebuild(WikiManager(config.wiki_root), excluded_paths=paused)
        stats["compiled_pages_paused"] = len(paused)
        backfilled = 0
        for row in db.conn.execute("SELECT id,raw_path,source_path FROM papers WHERE status != 'DELETED'").fetchall():
            if db.count_source_chunks(str(row["id"])):
                continue
            original = Path(str(row["raw_path"] or row["source_path"] or ""))
            if not original.is_file() or _hash_file(original) != str(row["id"]):
                continue
            try:
                db.index_source_chunks(str(row["id"]), parse_file(original).pages)
                backfilled += 1
            except (OSError, ValueError, RuntimeError):
                continue
        stats["original_sources_backfilled"] = backfilled
        stats["evidence_locators"] = _refresh_evidence_locators(config, db)
        graph_stats = extract_graph(db, wiki_root=config.wiki_root)
        stats["graph_nodes"] = int(graph_stats["stats"]["nodes"])
        stats["graph_edges"] = int(graph_stats["stats"]["edges"])
        stats["reviews_added"] = _audit_existing_reviews(config, db)
        sync_vector_index(db, config.vector_config)
        _log(config, {"event": "build", **stats})
        return stats
    finally:
        db.close()


def do_search(config: Config, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
    initialize(config)
    db = Database(config.db_path)
    try:
        return search(
            db,
            query,
            top_k or config.top_k,
            vector_config=config.vector_config,
            graph_expansion=bool(config.retrieval.get("graph_expansion", True)),
        )
    finally:
        db.close()


def _answer_evidence(config: Config, question: str, top_k: Optional[int], evidence_scope: str) -> List[Dict[str, Any]]:
    if evidence_scope not in {"hybrid", "wiki"}:
        raise ValueError("evidence_scope must be hybrid or wiki")
    initialize(config)
    db = Database(config.db_path)
    try:
        return retrieve_hybrid(config, db, question, top_k or config.top_k, evidence_scope=evidence_scope)
    finally:
        db.close()


def do_ask(config: Config, question: str, top_k: Optional[int] = None, evidence_scope: str = "hybrid") -> str:
    results = _answer_evidence(config, question, top_k, evidence_scope)
    return answer(question, results, LLMClient(config.llm), context_config={**config.retrieval, "wiki_language": config.wiki_language})


def do_ask_with_metadata(config: Config, question: str, top_k: Optional[int] = None, evidence_scope: str = "hybrid") -> Dict[str, Any]:
    results = _answer_evidence(config, question, top_k, evidence_scope)
    return answer_with_metadata(question, results, LLMClient(config.llm), context_config={**config.retrieval, "wiki_language": config.wiki_language})


def do_answer_events(config: Config, question: str, top_k: Optional[int] = None, evidence_scope: str = "hybrid"):
    results = _answer_evidence(config, question, top_k, evidence_scope)
    yield from answer_events(question, results, LLMClient(config.llm),
                             context_config={**config.retrieval, "top_k": top_k or config.top_k, "wiki_language": config.wiki_language})


def parse_only(
    config: Config,
    source: Union[str, Path],
    llm_client: Optional[LLMClient] = None,
    mode: str = "basic",
) -> StructuredPaper:
    path = Path(source).expanduser().resolve()
    document = parse_file(path)
    base = extract_basic(document, source_path=path, workspace_root=config.root)
    header = header_metadata(path, str(config.parser.get("grobid_url") or ""))
    base.title = header.get("title") or base.title
    base.authors = header.get("authors") or base.authors
    base.abstract = header.get("abstract") or base.abstract
    base.keywords = header.get("keywords") or base.keywords
    if mode == "basic":
        return base
    if mode not in {"standard", "high"}:
        raise ValueError("mode must be basic, standard or high")
    client = llm_client or LLMClient(config.llm)
    analysis = analyze_paper(document, client, purpose=_purpose(config), language=config.wiki_language) if mode == "high" else ""
    return enrich_paper(
        document, base, client, analysis=analysis,
        purpose=_purpose(config), quality_mode=mode, source_path=path,
        language=config.wiki_language,
    )


def _log(config: Config, data: Dict[str, Any]) -> None:
    log_path = config.root / "logs" / "researchwiki.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": utc_now(), **data}, ensure_ascii=False) + "\n")
