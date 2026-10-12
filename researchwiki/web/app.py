from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from .. import __version__
from ..app import delete_source, do_ask_with_metadata, do_answer_events, do_search, ingest, initialize, rebuild
from ..compilation import CompilationService, compilation_status
from ..config import Config, load_config
from ..evaluation import load_questions, run_evaluation
from ..graph import extract_graph
from ..graph.research import project_research_graph
from ..index.database import Database
from ..llm.client import LLMClient
from ..parsing.pdf import parse_file
from ..secrets import read_deepseek_key, save_deepseek_key, delete_deepseek_key
from ..wiki.manager import parse_frontmatter


LOGGER = logging.getLogger("researchwiki.web")
STATIC_ROOT = Path(__file__).with_name("static")
ALLOWED_SUFFIXES = {".pdf", ".txt", ".md", ".markdown", ".docx"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PROCESSING_STATUSES = {"UPLOADED", "EXTRACTING", "EXTRACTED", "WIKI_CREATED"}
_INGEST_LOCK = threading.Lock()
_UPLOAD_LOCK = threading.Lock()
_SETTINGS_LOCK = threading.Lock()


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    top_k: Optional[int] = Field(default=None, ge=1, le=20)


class DeepSeekCredentialRequest(BaseModel):
    api_key: SecretStr = Field(min_length=8, max_length=512)


def _check_local_settings_write(request: Request) -> None:
    """Defense in depth for credential updates on a no-auth localhost server."""
    client = request.client.host if request.client else ""
    host = request.headers.get("host", "")
    target = urlparse("http://" + host).hostname or ""
    # "testclient" / "testserver" exist only in in-process FastAPI tests.
    is_test_client = client == "testclient" and target == "testserver"
    if not is_test_client and (client not in {"127.0.0.1", "::1"} or
                               target not in {"localhost", "127.0.0.1", "::1"}):
        raise HTTPException(status_code=403, detail="Model credentials can only be changed locally")
    if request.headers.get("x-rw-config-action") != "1":
        raise HTTPException(status_code=403, detail="Explicit settings action header required")
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin settings changes are forbidden")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc.lower() != host.lower():
        raise HTTPException(status_code=403, detail="Cross-origin settings changes are forbidden")


def _relative_name(root: Path, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return path.name


def _quality(config: Config, paper: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    status = str(paper.get("status") or "")
    if status == "FAILED":
        reasons.append(str(paper.get("error_message") or "处理失败"))
        return {"level": "error", "reasons": reasons}
    raw_path = str(paper.get("raw_path") or "")
    if raw_path and not Path(raw_path).exists():
        reasons.append("原始文件缺失")
    if status == "INDEXED":
        extraction_path = config.root / "data" / "extractions" / (str(paper.get("id")) + ".json")
        if not extraction_path.exists():
            reasons.append("结构化抽取文件缺失")
        else:
            try:
                value = json.loads(extraction_path.read_text(encoding="utf-8"))
                title = str(value.get("title") or "").casefold()
                generic = ("contents lists available" in title or title.startswith("information sciences "))
                sparse = not value.get("abstract") and not value.get("methods") and not value.get("topics")
                if generic or sparse:
                    reasons.append("抽取内容可能不完整，请核对标题与正文")
            except (OSError, ValueError, TypeError):
                reasons.append("结构化抽取文件无法读取")
    if reasons:
        return {"level": "warning", "reasons": reasons}
    if status in PROCESSING_STATUSES:
        return {"level": "processing", "reasons": []}
    return {"level": "ok", "reasons": []}


def _document(
    config: Config,
    paper: Dict[str, Any],
    include_extraction: bool = False,
    job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source_path = str(paper.get("source_path") or "")
    raw_path = str(paper.get("raw_path") or "")
    result: Dict[str, Any] = {
        "id": str(paper.get("id") or ""),
        "title": str(paper.get("title") or ""),
        "authors": paper.get("authors") or [],
        "year": paper.get("year"),
        "status": str(paper.get("status") or ""),
        "source_name": Path(source_path).name if source_path else "",
        "raw_name": Path(raw_path).name if raw_path else "",
        "source_ref": _relative_name(config.root, source_path),
        "raw_ref": _relative_name(config.root, raw_path),
        "file_type": Path(raw_path or source_path).suffix.lower().lstrip("."),
        "file_url": "/api/documents/" + str(paper.get("id") or "") + "/file?inline=true",
        "created_at": paper.get("created_at"),
        "updated_at": paper.get("updated_at"),
        "error_message": paper.get("error_message") or None,
        "retryable": str(paper.get("status")) == "FAILED" and bool(raw_path or source_path),
        "quality": _quality(config, paper),
        "job": job,
    }
    if include_extraction:
        extraction_path = config.root / "data" / "extractions" / (result["id"] + ".json")
        if extraction_path.exists():
            try:
                result["extraction"] = json.loads(extraction_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                result["extraction"] = None
        else:
            result["extraction"] = None
    return result


def _drain_ingest_queue(root: Path) -> None:
    with _INGEST_LOCK:
        config = load_config(root)
        while True:
            database = Database(config.db_path)
            try:
                job = database.claim_next_ingest_job()
            finally:
                database.close()
            if not job:
                return
            source = Path(str(job.get("source_path") or ""))
            if not source.is_file():
                database = Database(config.db_path)
                try:
                    message = "Queued source file is missing; upload it again."
                    database.update_ingest_job(int(job["id"]), status="FAILED", stage="FAILED", error_message=message)
                    database.update_paper_status(str(job["source_id"]), "FAILED", message)
                finally:
                    database.close()
                continue
            try:
                ingest(
                    config,
                    source,
                    resume=True,
                    quality_mode=str(job.get("quality_mode") or "basic"),
                    job_id=int(job["id"]),
                )
            except Exception as exc:  # ingest persists FAILED before returning here
                LOGGER.warning("Background ingest job %s failed for %s: %s", job["id"], source.name, exc)


def _safe_upload_name(filename: str) -> str:
    base = Path(filename or "document").name
    safe = re.sub(r"[^\w.()\-\u3400-\u9fff]+", "-", base, flags=re.UNICODE).strip("-.")
    return safe[:160] or "document"


def _public_search_result(config: Config, item: Dict[str, Any]) -> Dict[str, Any]:
    content = re.sub(r"^---.*?---", "", str(item.get("content") or ""), flags=re.S).strip()
    return {
        "id": int(item.get("page_id") or 0),
        "title": str(item.get("title") or ""),
        "type": str(item.get("type") or ""),
        "path": _relative_name(config.root, str(item.get("path") or "")),
        "excerpt": content[:600],
        "score": item.get("score"),
        "retrieval_signals": item.get("retrieval_signals") or [],
        "sources": [_public_evidence(config, value) for value in item.get("sources") or []],
    }


def _public_evidence(config: Config, evidence: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(evidence)
    for key in ("raw_path", "source_path"):
        if key in value:
            value[key] = _relative_name(config.root, str(value[key] or ""))
    if "original_name" in value:
        value["original_name"] = Path(str(value["original_name"] or "")).name
    source_id = str(value.get("source_id") or "")
    if source_id:
        page = max(1, int(value.get("page_number") or 1))
        value["file_url"] = f"/api/documents/{source_id}/file?inline=true#page={page}"
        value["evidence_url"] = f"/api/documents/{source_id}/evidence?page={page}&start={int(value.get('char_start') or 0)}&end={int(value.get('char_end') or 0)}"
    return value


def _public_answer(config: Config, result: Dict[str, Any], question: str) -> Dict[str, Any]:
    result = dict(result)
    result["question"] = question
    result["degraded"] = result.get("mode") == "local_evidence" and bool(result.get("fallback_reason"))
    result["answer"] = str(result.get("answer") or "").replace(str(config.root.resolve()) + "/", "")
    result["sources"] = [{**source, "path": _relative_name(config.root, str(source.get("path") or "")),
                          "provenance": [_public_evidence(config, value) for value in source.get("provenance") or []]}
                         for source in result.get("sources") or []]
    return result


def create_app(root: Union[str, Path] = ".") -> FastAPI:
    config = load_config(root)
    initialize(config)
    startup_db = Database(config.db_path)
    try:
        interrupted = startup_db.fail_interrupted()
        interrupted_jobs = startup_db.fail_interrupted_jobs()
    finally:
        startup_db.close()
    if interrupted:
        LOGGER.warning("Marked %s interrupted ingestion task(s) as FAILED", interrupted)
    if interrupted_jobs:
        LOGGER.warning("Marked %s interrupted persistent queue job(s) as FAILED", interrupted_jobs)
    ingest_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="researchwiki-ingest")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ingest_executor.submit(_drain_ingest_queue, config.root)
        yield
        ingest_executor.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="ResearchWiki", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.ingest_executor = ingest_executor

    if STATIC_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC_ROOT)), name="assets")

    def health_payload() -> Dict[str, Any]:
        llm = LLMClient(config.llm)
        return {
            "status": "ok",
            "service": "researchwiki",
            "version": __version__,
            "storage": {"type": "sqlite", "ready": config.db_path.exists()},
            "retrieval": {
                "type": "hybrid",
                "lexical": "sqlite-fts5",
                "vector_enabled": bool(config.vector_config.get("enabled", False)),
                "graph_expansion": bool(config.retrieval.get("graph_expansion", True)),
                "max_context_chars": int(config.retrieval.get("max_context_chars", 48000)),
            },
            "llm": {
                "provider": llm.provider,
                "model": llm.model,
                "configured": llm.enabled,
                "note": "configured does not mean the remote model was contacted",
            },
        }

    @app.get("/health")
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return health_payload()

    @app.get("/api/overview")
    def overview() -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            stats = db.overview()
            papers = db.list_papers(page=1, page_size=1000)["items"]
            recent = db.list_papers(page=1, page_size=6)["items"]
            warnings = sum(1 for paper in papers if _quality(config, paper)["level"] in {"warning", "error"})
            paper_titles = {str(paper.get("title") or "").casefold() for paper in papers}
            orphan_pages = sum(
                1
                for page in db.list_wiki_pages(page_type="paper", limit=2000)
                if str(page.get("title") or "").casefold() not in paper_titles
            )
            recent_documents = [
                _document(config, paper, job=db.latest_ingest_job(str(paper.get("id") or "")))
                for paper in recent
            ]
        finally:
            db.close()
        return {
            **stats,
            "recent_documents": recent_documents,
            "quality": {"documents_with_warnings": warnings, "orphan_paper_pages": orphan_pages},
            "system": health_payload(),
            "dataflow": [
                {"key": "documents", "label": "资料中心", "detail": "原文哈希 · 持久化导入"},
                {"key": "compilation", "label": "知识编译", "detail": "抽取缓存 · 跨论文聚合"},
                {"key": "wiki", "label": "Wiki 知识页", "detail": "引用 · 内链 · 人工笔记"},
                {"key": "graph", "label": "图谱与检索", "detail": "稳定节点边 · 证据对象"},
                {"key": "ask", "label": "可溯源问答", "detail": "流式回答 · 原文定位"},
                {"key": "evaluation", "label": "固定题库评估", "detail": "冻结快照 · 对照回放"},
            ],
        }

    @app.get("/api/documents")
    def list_documents(
        q: str = Query(default="", max_length=200),
        status: str = Query(default="", max_length=32),
        file_type: str = Query(default="", pattern="^(|pdf|md|markdown|txt|docx)$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            result = db.list_papers(query=q, status=status, page=page, page_size=page_size, file_type=file_type)
            result["items"] = [
                _document(config, paper, job=db.latest_ingest_job(str(paper.get("id") or "")))
                for paper in result["items"]
            ]
        finally:
            db.close()
        return result

    @app.get("/api/documents/{paper_id}")
    def get_document(paper_id: str) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            paper = db.get_paper(paper_id)
            if paper and str(paper.get("status")) == "DELETED":
                paper = None
            pages = db.list_pages_for_source(paper_id) if paper else []
            job = db.latest_ingest_job(paper_id) if paper else None
            reviews = [value for value in db.list_reviews(status="OPEN", limit=500) if value.get("source_id") == paper_id]
        finally:
            db.close()
        if not paper:
            raise HTTPException(status_code=404, detail="Document not found")
        result = _document(config, paper, include_extraction=True, job=job)
        result["wiki_pages"] = [
            {**page, "path": _relative_name(config.root, str(page.get("path") or ""))}
            for page in pages
        ]
        result["reviews"] = reviews
        return result

    @app.post("/api/documents", status_code=202)
    async def upload_document(
        file: UploadFile = File(...),
        quality_mode: str = Form(default="basic"),
    ) -> Dict[str, Any]:
        quality_mode = quality_mode.strip().lower()
        if quality_mode not in {"basic", "standard", "high"}:
            raise HTTPException(status_code=400, detail="quality_mode must be basic, standard or high")
        filename = _safe_upload_name(file.filename or "document")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(status_code=415, detail="Supported formats: PDF, DOCX, TXT, MD")
        payload = await file.read(MAX_UPLOAD_BYTES + 1)
        await file.close()
        if not payload:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Upload exceeds the 50 MiB limit")
        digest = hashlib.sha256(payload).hexdigest()
        inbox = config.root / "data" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        source = inbox / (digest[:12] + "-" + filename)
        if not source.exists():
            source.write_bytes(payload)
        with _UPLOAD_LOCK:
            db = Database(config.db_path)
            try:
                existing = db.find_by_hash(digest)
                if existing and str(existing["status"]) != "DELETED":
                    current = db.get_paper(digest) or dict(existing)
                    job = db.latest_ingest_job(digest)
                    document = _document(config, current, job=job)
                    status = str(existing["status"])
                    if status == "FAILED":
                        action = "retry_required"
                    elif status == "INDEXED":
                        action = "already_indexed"
                    else:
                        action = "already_processing"
                    return {"document": document, "job": job, "deduplicated": True, "action": action}
                db.upsert_paper(digest, Path(filename).stem, [], None, str(source), "", "UPLOADED")
                db.register_source(
                    digest,
                    str(source),
                    "",
                    file.content_type or "application/octet-stream",
                    len(payload),
                )
                job = db.enqueue_ingest_job(
                    digest,
                    str(source),
                    quality_mode=quality_mode,
                    max_attempts=config.max_ingest_attempts,
                )
                paper = db.get_paper(digest)
            finally:
                db.close()
        app.state.ingest_executor.submit(_drain_ingest_queue, config.root)
        return {
            "document": _document(config, paper or {"id": digest, "title": filename, "status": "UPLOADED"}, job=job),
            "job": job,
            "deduplicated": False,
            "action": "queued",
        }

    @app.post("/api/documents/{paper_id}/retry", status_code=202)
    def retry_document(
        paper_id: str,
        quality_mode: str = Query(default="", max_length=16),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            paper = db.get_paper(paper_id)
            if paper and str(paper.get("status")) == "DELETED":
                paper = None
            if not paper:
                raise HTTPException(status_code=404, detail="Document not found")
            if str(paper.get("status")) != "FAILED":
                raise HTTPException(status_code=409, detail="Only failed documents can be retried")
            candidates = [
                Path(value)
                for value in (str(paper.get("raw_path") or ""), str(paper.get("source_path") or ""))
                if value
            ]
            source = next((path for path in candidates if path.is_file()), None)
            if source is None:
                raise HTTPException(status_code=409, detail="The source file is missing; upload it again")
            previous_job = db.latest_ingest_job(paper_id)
            requested_mode = quality_mode.strip().lower() or str((previous_job or {}).get("quality_mode") or config.quality_mode)
            if requested_mode not in {"basic", "standard", "high"}:
                raise HTTPException(status_code=400, detail="quality_mode must be basic, standard or high")
            db.update_paper_status(paper_id, "UPLOADED")
            job = db.retry_ingest_job(int(previous_job["id"]), requested_mode) if previous_job else None
            if job is None:
                job = db.enqueue_ingest_job(
                    paper_id,
                    str(source),
                    quality_mode=requested_mode,
                    max_attempts=config.max_ingest_attempts,
                )
            updated = db.get_paper(paper_id)
        finally:
            db.close()
        app.state.ingest_executor.submit(_drain_ingest_queue, config.root)
        return {"document": _document(config, updated or paper, job=job), "job": job, "deduplicated": False, "action": "queued"}

    @app.post("/api/documents/{paper_id}/enrich", status_code=202)
    def enrich_document(
        paper_id: str,
        quality_mode: str = Query(default="standard", pattern="^(standard|high)$"),
    ) -> Dict[str, Any]:
        """Explicitly reprocess an existing document with optional LLM analysis."""
        if not LLMClient(config.llm).enabled:
            raise HTTPException(status_code=409, detail="Deep extraction requires a configured model API key")
        with _UPLOAD_LOCK:
            db = Database(config.db_path)
            try:
                paper = db.get_paper(paper_id)
                if not paper or str(paper.get("status")) != "INDEXED":
                    raise HTTPException(status_code=409, detail="Only indexed documents can be enriched")
                candidates = [Path(v) for v in (str(paper.get("raw_path") or ""), str(paper.get("source_path") or "")) if v]
                source = next((p for p in candidates if p.is_file()), None)
                if source is None:
                    raise HTTPException(status_code=409, detail="Original source file is missing")
                job = db.enqueue_ingest_job(paper_id, str(source), quality_mode, config.max_ingest_attempts)
            finally:
                db.close()
        app.state.ingest_executor.submit(_drain_ingest_queue, config.root)
        return {"action": "queued", "job": job, "source_id": paper_id}

    @app.post("/api/documents/{paper_id}/supplement")
    def supplement_document(
        request: Request,
        paper_id: str,
        focus: str = Query(pattern="^(method|experiment|overview)$"),
    ) -> Dict[str, Any]:
        """Explicit on-demand update of a single research facet."""
        _check_local_settings_write(request)
        if not LLMClient(config.llm).enabled:
            raise HTTPException(status_code=409, detail="Focused extraction requires a model API key")
        if not _INGEST_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="An extraction or compilation is already running")
        try:
            db = Database(config.db_path)
            try:
                paper = db.get_paper(paper_id)
                if not paper or paper.get("status") != "INDEXED":
                    raise HTTPException(status_code=409, detail="Only indexed sources can be supplemented")
                raw = Path(str(paper.get("raw_path") or ""))
                if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest() != paper_id:
                    raise HTTPException(status_code=409, detail="Original source unavailable or changed")
            finally:
                db.close()
            from ..app import ingest
            result = ingest(config, raw, resume=True, quality_mode="standard", focus=focus)
            return {"ok": True, "focus": focus, "document": result}
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail="Focused extraction failed; previous content preserved") from exc
        finally:
            _INGEST_LOCK.release()

    @app.delete("/api/documents/{paper_id}")
    def remove_document(
        paper_id: str,
        confirm: bool = Query(default=False),
        delete_raw: bool = Query(default=True),
    ) -> Dict[str, Any]:
        if not confirm:
            raise HTTPException(status_code=400, detail="Set confirm=true to move this source and generated pages to the workspace trash")
        try:
            with _INGEST_LOCK:
                result = delete_source(config, paper_id, delete_raw=delete_raw)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result["recoverable_from"] = _relative_name(config.root, str(result["recoverable_from"]))
        return result

    @app.get("/api/jobs")
    def list_jobs(
        status: str = Query(default="", max_length=20),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            items = db.list_ingest_jobs(status=status, limit=limit)
        finally:
            db.close()
        for item in items:
            item["source_path"] = _relative_name(config.root, str(item.get("source_path") or ""))
        return {"items": items, "total": len(items)}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: int) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            job = db.retry_ingest_job(job_id)
            if not job:
                raise HTTPException(status_code=409, detail="Only failed, cancelled, or paused jobs can be retried")
            db.update_paper_status(str(job["source_id"]), "UPLOADED")
        finally:
            db.close()
        app.state.ingest_executor.submit(_drain_ingest_queue, config.root)
        return {"job": job, "action": "queued"}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            if not db.cancel_ingest_job(job_id):
                raise HTTPException(status_code=409, detail="Only pending or paused jobs can be cancelled")
            job = db.get_ingest_job(job_id)
            if job:
                db.update_paper_status(str(job["source_id"]), "FAILED", "CANCELLED: queued import was cancelled; retry to resume.")
        finally:
            db.close()
        return {"job": job, "action": "cancelled"}

    @app.get("/api/reviews")
    def list_reviews(
        status: str = Query(default="OPEN", max_length=20),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            items = db.list_reviews(status=status, limit=limit)
        finally:
            db.close()
        for item in items:
            item["page_path"] = _relative_name(config.root, str(item.get("page_path") or ""))
        return {"items": items, "total": len(items)}

    @app.post("/api/reviews/{review_id}/resolve")
    def resolve_review(review_id: int) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            resolved = db.resolve_review(review_id)
        finally:
            db.close()
        if not resolved:
            raise HTTPException(status_code=404, detail="Open review not found")
        return {"id": review_id, "status": "RESOLVED"}

    @app.get("/api/documents/{paper_id}/file")
    def download_document(paper_id: str, inline: bool = False) -> FileResponse:
        db = Database(config.db_path)
        try:
            paper = db.get_paper(paper_id)
        finally:
            db.close()
        if not paper or paper.get("status") == "DELETED" or not paper.get("raw_path"):
            raise HTTPException(status_code=404, detail="Source file not found")
        path = Path(str(paper["raw_path"])).resolve()
        raw_root = (config.root / "raw" / "papers").resolve()
        if not path.exists() or not path.is_relative_to(raw_root):
            raise HTTPException(status_code=404, detail="Source file not found")
        return FileResponse(path, filename=path.name, content_disposition_type="inline" if inline else "attachment")

    @app.get("/api/documents/{paper_id}/evidence")
    def get_document_evidence(paper_id: str, page: int = Query(default=1, ge=1),
                             start: int = Query(default=0, ge=0), end: int = Query(default=0, ge=0)) -> Dict[str, Any]:
        response = download_document(paper_id, inline=True)
        path = Path(response.path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != paper_id:
            raise HTTPException(status_code=409, detail="Source hash changed; original evidence cannot be verified")
        try:
            document = parse_file(path)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        target = next((value for value in document.pages if value.number == page), None)
        if target is None or start > len(target.text) or (end and end < start):
            raise HTTPException(status_code=422, detail="Evidence locator is outside the parsed source")
        stop = min(end or start + 4000, len(target.text), start + 10000)
        return {"source_id": paper_id, "page_number": page, "char_start": start, "char_end": stop,
                "text": target.text[start:stop], "source_hash_verified": True,
                "pagination": "physical" if path.suffix.lower() == ".pdf" else "logical",
                "file_url": f"/api/documents/{paper_id}/file?inline=true#page={page}"}

    @app.get("/api/compilation")
    def get_compilation() -> Dict[str, Any]:
        return {**compilation_status(config), "busy": _INGEST_LOCK.locked()}

    @app.get("/api/compilation/history")
    def get_compilation_history() -> Dict[str, Any]:
        items = CompilationService(config).history()
        return {"items": items, "total": len(items)}

    @app.post("/api/compilation/build")
    def build_compilation() -> Dict[str, Any]:
        if not _INGEST_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="导入或编译正在运行，请完成后再试。")
        try:
            report = CompilationService(config).run()
            if report.get("status") == "succeeded":
                report["index"] = rebuild(config)
            return report
        finally:
            _INGEST_LOCK.release()

    @app.post("/api/wiki/topics/{page_id}/synthesize")
    def synthesize_wiki_topic(page_id: int) -> Dict[str, Any]:
        """Optional LLM-only synthesis; never runs during the normal compiler."""
        from ..extraction.synthesis import synthesize_topic
        if not LLMClient(config.llm).enabled:
            raise HTTPException(status_code=409, detail="跨论文深度汇总需要配置模型 API Key")
        if not _INGEST_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有资料导入或知识编译任务正在运行")
        try:
            db = Database(config.db_path)
            try:
                page = db.get_wiki_page(page_id)
            finally:
                db.close()
            if not page or page.get("type") != "topic":
                raise HTTPException(status_code=404, detail="未找到研究主题 Wiki 页面")
            try:
                summary = synthesize_topic(config, str(page["title"]))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            report = CompilationService(config).run()
            if report["status"] != "succeeded":
                raise HTTPException(status_code=409, detail="汇总已缓存，但 Wiki 编译失败，请检查编译诊断")
            rebuild(config)
            return {"ok": True, "topic": page["title"],
                    "claim_count": len(summary["claims"]),
                    "question_count": len(summary["open_questions"]),
                    "review_status": summary["review_status"]}
        finally:
            _INGEST_LOCK.release()

    @app.get("/api/wiki/pages")
    def list_wiki_pages(
        q: str = Query(default="", max_length=200),
        type: str = Query(default="", max_length=32),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Dict[str, Any]:
        # Stable page identity remains its original name; Chinese aliases are display-only.
        manifest = CompilationService(config).manifest()
        labels = {
            str(item.get("path") or ""): str(item.get("display_title") or item.get("title") or "")
            for item in manifest.get("pages", [])
            if isinstance(item, dict)
        }
        def localized(page: Dict[str, Any]) -> Dict[str, Any]:
            relative = _relative_name(config.root, str(page.get("path") or ""))
            return {**page, "path": relative,
                    "display_title": labels.get(relative) or str(page.get("title") or "")}

        db = Database(config.db_path)
        try:
            if q.strip() and re.search(r"[\u3400-\u9fff]", q):
                # The SQL title index uses original names; include Chinese display aliases.
                all_pages = [localized(row) for row in db.list_wiki_pages(page_type=type, limit=100000)]
                found = [item for item in all_pages if q.casefold() in
                         (item["title"] + " " + item["display_title"]).casefold()]
                total = len(found)
                pages = found[:limit]
            else:
                pages = [localized(row) for row in db.list_wiki_pages(query=q, page_type=type, limit=limit)]
                total = db.count_wiki_pages(query=q, page_type=type)
        finally:
            db.close()
        return {"items": pages, "total": total}

    @app.get("/api/wiki/pages/{page_id}")
    def get_wiki_page(page_id: int) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            page = db.get_wiki_page(page_id)
            related = db.related_pages(page_id) if page else []
            backlinks = [dict(row) for row in db.conn.execute(
                "SELECT DISTINCT p.id,p.title,p.type,p.path FROM links l JOIN wiki_pages p ON p.id=l.source_id WHERE l.target_id=? ORDER BY p.title", (page_id,))] if page else []
            outgoing = [dict(row) for row in db.conn.execute(
                "SELECT DISTINCT p.id,p.title,p.type,p.path FROM links l JOIN wiki_pages p ON p.id=l.target_id WHERE l.source_id=? ORDER BY p.title", (page_id,))] if page else []
        finally:
            db.close()
        if not page:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        page["path"] = _relative_name(config.root, str(page.get("path") or ""))
        metadata = parse_frontmatter(str(page.get("content") or ""))
        page["display_title"] = str(metadata.get("display_title") or page["title"])
        declared_sources = metadata.get("sources") or []
        if not isinstance(declared_sources, list):
            declared_sources = []
        order = {str(value): index for index, value in enumerate(declared_sources, 1)}
        sources = sorted(page.get("sources") or [], key=lambda source: order.get(str(source.get("source_id")), len(order) + 1))
        page["sources"] = [{**_public_evidence(config, source),
                            "citation_index": order.get(str(source.get("source_id")), index)}
                           for index, source in enumerate(sources, 1)]
        page["backlinks"] = [{**item, "path": _relative_name(config.root, str(item["path"]))} for item in backlinks]
        page["outgoing"] = [{**item, "path": _relative_name(config.root, str(item["path"]))} for item in outgoing]
        page["related"] = [
            {**item, "path": _relative_name(config.root, str(item.get("path") or ""))}
            for item in related
        ]
        return page

    @app.get("/api/graph/research")
    def get_research_graph(
        center: str = Query(default="", max_length=100),
        depth: int = Query(default=1, ge=1, le=2),
        limit: int = Query(default=130, ge=1, le=400),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            return project_research_graph(db, config.root, center=center or None,
                                          depth=depth, limit=limit)
        finally:
            db.close()

    @app.get("/api/graph")
    def get_graph(limit: int = Query(default=500, ge=1, le=2000)) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            if db.graph_overview()["stats"]["nodes"] == 0 and db.count_wiki_pages() > 0:
                extract_graph(db, wiki_root=config.wiki_root)
            value = db.graph(limit=limit)
        finally:
            db.close()
        for node in value["nodes"]:
            node["path"] = _relative_name(config.root, str(node.get("path") or ""))
            node["source_doc"] = Path(str(node.get("source_doc") or "")).name
        return value

    @app.get("/api/graph/nodes")
    def get_graph_nodes(
        type: str = Query(default="", max_length=32),
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=2000, ge=1, le=10000),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            if db.graph_overview()["stats"]["nodes"] == 0 and db.count_wiki_pages() > 0:
                extract_graph(db, wiki_root=config.wiki_root)
            items = db.list_graph_nodes(node_type=type, query=q, limit=limit)
        finally:
            db.close()
        for node in items:
            node["path"] = _relative_name(config.root, str(node.get("path") or ""))
            node["source_doc"] = Path(str(node.get("source_doc") or "")).name
        return {"items": items, "total": len(items)}

    @app.get("/api/graph/edges")
    def get_graph_edges(
        semantic: str = Query(default="", max_length=32),
        limit: int = Query(default=10000, ge=1, le=20000),
    ) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            if db.graph_overview()["stats"]["nodes"] == 0 and db.count_wiki_pages() > 0:
                extract_graph(db, wiki_root=config.wiki_root)
            items = db.list_graph_edges(semantic=semantic, limit=limit)
        finally:
            db.close()
        return {"items": items, "total": len(items)}

    @app.post("/api/graph/extract")
    def reextract_graph() -> Dict[str, Any]:
        with _INGEST_LOCK:
            db = Database(config.db_path)
            try:
                value = extract_graph(db, wiki_root=config.wiki_root)
            finally:
                db.close()
        return {
            "ok": True,
            "mode": value["mode"],
            "stats": value["stats"],
            "generated_at": value["generated_at"],
            "digest": value["digest"],
        }

    @app.get("/api/search")
    def search_wiki(q: str = Query(min_length=1, max_length=500), top_k: int = Query(default=8, ge=1, le=20)) -> Dict[str, Any]:
        results = do_search(config, q, top_k=top_k)
        return {"items": [_public_search_result(config, item) for item in results], "total": len(results)}

    @app.post("/api/ask")
    def ask(request: AskRequest) -> Dict[str, Any]:
        result = do_ask_with_metadata(config, request.question, top_k=request.top_k)
        return _public_answer(config, result, request.question)

    @app.post("/api/ask/stream")
    def ask_stream(request: AskRequest) -> StreamingResponse:
        def event(name: str, payload: Dict[str, Any]) -> str:
            return "event: " + name + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        def generate() -> Iterator[str]:
            yield event("status", {"stage": "retrieval", "message": "正在检索 Wiki 证据"})
            try:
                for item in do_answer_events(config, request.question, top_k=request.top_k):
                    payload = item["data"]
                    if item["event"] in {"sources", "result"}:
                        payload = _public_answer(config, payload, request.question)
                    yield event(item["event"], payload)
            except Exception as exc:
                yield event("error", {"message": str(exc)})
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/evaluation/questions")
    def evaluation_questions() -> Dict[str, Any]:
        items = load_questions(config)
        return {"items": items, "total": len(items)}

    @app.get("/api/evaluation/runs")
    def evaluation_runs(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            items = db.list_evaluation_runs(limit=limit)
        finally:
            db.close()
        return {"items": items, "total": len(items)}

    @app.get("/api/evaluation/runs/{run_id}")
    def evaluation_run(run_id: int) -> Dict[str, Any]:
        db = Database(config.db_path)
        try:
            value = db.get_evaluation_run(run_id)
        finally:
            db.close()
        if not value:
            raise HTTPException(status_code=404, detail="Evaluation run not found")
        for case in value.get("cases") or []:
            case["answer"] = str(case.get("answer") or "").replace(str(config.root.resolve()) + "/", "")
            for source in case.get("sources") or []:
                source["path"] = _relative_name(config.root, str(source.get("path") or ""))
                for evidence in source.get("provenance") or []:
                    evidence["raw_path"] = _relative_name(config.root, str(evidence.get("raw_path") or ""))
        return value

    @app.post("/api/evaluation/run")
    def start_evaluation(profile: str = Query(default="optimized", pattern="^(baseline|optimized)$")) -> Dict[str, Any]:
        value = run_evaluation(config, profile=profile)
        return evaluation_run(int(value["id"]))

    @app.post("/api/evaluation/compare")
    def compare_evaluation() -> Dict[str, Any]:
        from ..evaluation.runner import compare_profiles
        value = compare_profiles(config)
        for profile in ("baseline", "optimized"):
            value[profile] = evaluation_run(int(value[profile]["id"]))
        value.setdefault("comparison", value.get("compare") or value.get("gate") or {})
        value.setdefault("gate", value["comparison"])
        return value

    @app.post("/api/evaluation/runs/{run_id}/replay")
    def replay_run(run_id: int) -> Dict[str, Any]:
        from ..evaluation.runner import replay_evaluation
        try:
            value = replay_evaluation(config, run_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return evaluation_run(int(value["id"]))

    @app.get("/api/settings")
    def settings() -> Dict[str, Any]:
        llm = LLMClient(config.llm)
        return {
            "llm": {
                "provider": llm.provider,
                "model": llm.model,
                "base_url": llm.base_url,
                "configured": llm.enabled,
                "timeout_seconds": llm.timeout_seconds,
            },
            "wiki": {"language": config.wiki_language, "original_text_preserved": True},
            "credentials": {"stored_locally": bool(read_deepseek_key(config.root)),
                            "configured": llm.enabled},
            "retrieval": {
                "top_k": config.top_k,
                "graph_expansion": bool(config.retrieval.get("graph_expansion", True)),
                "vector_enabled": bool(config.vector_config.get("enabled", False)),
                "max_context_chars": int(config.retrieval.get("max_context_chars", 48000)),
            },
        }

    @app.post("/api/settings/deepseek-key")
    def store_deepseek_key(request: Request, payload: DeepSeekCredentialRequest) -> Dict[str, Any]:
        _check_local_settings_write(request)
        secret = payload.api_key.get_secret_value().strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,512}", secret):
            raise HTTPException(status_code=422, detail="Invalid API key format")
        with _SETTINGS_LOCK:
            save_deepseek_key(config.root, secret)
            config.llm = {**config.llm, "provider": "deepseek", "api_key": secret}
        return {"ok": True, "configured": True, "stored_locally": True}

    @app.delete("/api/settings/deepseek-key")
    def clear_deepseek_key(request: Request) -> Dict[str, Any]:
        _check_local_settings_write(request)
        with _SETTINGS_LOCK:
            delete_deepseek_key(config.root)
            # Clearing a locally stored key does not revoke a provider credential,
            # nor does it clear a key explicitly supplied by the user's environment.
            config.llm = dict(load_config(config.root).llm)
        return {"ok": True, "configured": LLMClient(config.llm).enabled,
                "stored_locally": False}

    @app.post("/api/settings/test")
    def test_model_connection() -> Dict[str, Any]:
        llm = LLMClient(config.llm)
        if not llm.enabled:
            raise HTTPException(status_code=409, detail="The selected model provider has no configured API key")
        started = __import__("time").monotonic()
        try:
            response = llm.generate(
                "You are a connectivity probe. Return exactly OK.",
                "Reply exactly OK.",
                temperature=0,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ok": True,
            "provider": llm.provider,
            "model": llm.model,
            "latency_ms": round((__import__("time").monotonic() - started) * 1000),
            "response": response.strip()[:80],
        }

    @app.get("/")
    def index() -> FileResponse:
        index_path = STATIC_ROOT / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=503, detail="Web assets are not installed")
        return FileResponse(index_path)

    return app
