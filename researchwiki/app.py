from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union, Optional

from .config import Config
from .extraction.paper import analyze_paper, extract_paper, enrich_paper
from .extraction.basic import extract_basic
from .graph import extract_graph
from .index.database import Database
from .llm.client import LLMClient
from .models import StructuredPaper
from .parsing.pdf import parse_file
from .qa.engine import answer, answer_with_metadata, answer_events
from .qa.retrieval import retrieve_evidence
from .retrieval.search import search
from .retrieval.vector import sync_vector_index
from .wiki.manager import WikiManager


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
    if not paper.abstract and not paper.methods and not paper.datasets and not paper.topics:
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
            try:
                pages = parse_file(source_path).pages
            except Exception:
                continue
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
) -> Dict[str, Any]:
    initialize(config)
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(str(source_path))
    file_hash = _hash_file(source_path)
    db = Database(config.db_path)
    try:
        mode = (quality_mode or ("standard" if llm_client is not None else config.quality_mode)).strip().lower()
        if mode not in {"basic", "standard", "high"}:
            raise ValueError("quality_mode must be basic, standard or high")
        duplicate = db.find_by_hash(file_hash)
        previous_indexed = bool(duplicate and duplicate["status"] == "INDEXED")
        extraction_path = config.root / "data" / "extractions" / (file_hash + ".json")
        previous_extraction = extraction_path.read_bytes() if previous_indexed and extraction_path.exists() else None
        previous_record = db.get_paper(file_hash) if previous_indexed else None
        if duplicate and duplicate["status"] == "INDEXED" and not resume:
            return {"status": "already_exists", "title": duplicate["title"], "paper_path": None, "entity_paths": []}
        _job_stage(db, job_id, "COPY_SOURCE")
        raw_path = _raw_copy(config, source_path, file_hash)
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
        _job_stage(db, job_id, "PARSE")
        document = parse_file(source_path)
        db.update_paper_status(file_hash, "EXTRACTING")
        _job_stage(db, job_id, "EXTRACT")
        base = extract_basic(document, source_path=source_path, workspace_root=config.root)
        if previous_extraction is not None and mode != "basic":
            previous = StructuredPaper.from_dict(json.loads(previous_extraction.decode("utf-8")))
            if previous.source_hash == file_hash:
                if base.title == "Untitled Paper":
                    base.title = previous.title
                base.authors = base.authors or previous.authors
                base.year = base.year or previous.year
                base.abstract = base.abstract or previous.abstract
                base.keywords = base.keywords or previous.keywords
        if mode == "basic":
            paper = base
        else:
            llm = llm_client or LLMClient(config.llm)
            analysis = ""
            if mode == "high":
                _job_stage(db, job_id, "ANALYZE")
                analysis = analyze_paper(document, llm=llm, purpose=_purpose(config))
                analysis_path = config.root / "data" / "analyses" / (file_hash + ".md")
                _atomic_write_text(analysis_path, analysis.rstrip() + "\n")
            paper = enrich_paper(
                document, baseline=base, llm=llm, analysis=analysis,
                purpose=_purpose(config), quality_mode=mode,
            )
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
        _atomic_write_text(structured_path, json.dumps(paper.to_dict(), ensure_ascii=False, indent=2) + "\n")
        _create_quality_reviews(db, file_hash, paper)
        _job_stage(db, job_id, "COMMIT_WIKI")
        # All ingest modes use the same compiler as "rw compile", eliminating
        # old/new Wiki template divergence and preserving protected user notes.
        from .compilation import CompilationService
        report = CompilationService(config, database=db).run()
        if report.get("status") != "succeeded":
            raise RuntimeError("Wiki compilation failed: " + str(report.get("error") or report.get("diagnostics")))
        manifest_pages = CompilationService(config, database=db).manifest().get("pages", [])
        my_pages = [item for item in manifest_pages if file_hash in item.get("sources", [])]
        paper_item = next((item for item in my_pages if item["type"] == "paper"
                           and item["id"] == "paper:" + file_hash), None)
        if not paper_item:
            raise RuntimeError("Wiki compiler did not emit a paper page")
        paper_path = config.root / paper_item["path"]
        entity_paths = [config.root / item["path"] for item in my_pages if item["type"] != "paper"]
        wiki = WikiManager(config.wiki_root)
        db.upsert_paper(file_hash, paper.title, paper.authors, paper.year, str(source_path), str(raw_path), "WIKI_CREATED")
        _job_stage(db, job_id, "REBUILD_INDEX")
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
            db.update_ingest_job(job_id, status="SUCCEEDED", stage="DONE", error_message="")
        _log(config, {"event": "ingest", "title": paper.title, "source": str(source_path), "paper": str(paper_path), "entities": [str(p) for p in entity_paths], "quality_mode": mode, "index": stats, "vectors": vector_stats})
        return {"status": "indexed", "title": paper.title, "paper_path": str(paper_path), "entity_paths": [str(p) for p in entity_paths], "quality_mode": mode, "index": stats, "vectors": vector_stats}
    except Exception as exc:
        # Deep enrichment must not destroy a previously indexed basic record
        # if the optional model, compiler or index fails.
        if "previous_indexed" in locals() and previous_indexed and previous_record:
            try:
                if previous_extraction is not None:
                    _atomic_write_text(extraction_path, previous_extraction.decode("utf-8"))
                db.upsert_paper(
                    file_hash, previous_record["title"], previous_record.get("authors") or [],
                    previous_record.get("year"), previous_record.get("source_path") or "",
                    previous_record.get("raw_path") or "", "INDEXED",
                )
            except Exception:
                pass
        if "file_hash" in locals() and not ("previous_indexed" in locals() and previous_indexed):
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
                    db.update_ingest_job(job_id, status="FAILED", stage="FAILED", error_message=str(exc))
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
                    },
                )
            except Exception:
                pass
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
        ]
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
            if not candidate.is_file():
                continue
            target = trash_root / "files" / candidate.name
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
        stats = db.rebuild(WikiManager(config.wiki_root))
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
        results = search(
            db,
            query,
            top_k or config.top_k,
            vector_config=config.vector_config,
            graph_expansion=bool(config.retrieval.get("graph_expansion", True)),
        )
        # The course's reproducible weighted retriever also supplies candidates
        # missed by FTS tokenization (notably Chinese questions about English papers).
        weighted = retrieve_evidence(db, query, top_k or config.top_k)
        seen = {int(item["page_id"]) for item in weighted}
        return (weighted + [item for item in results if int(item["page_id"]) not in seen])[:top_k or config.top_k]
    finally:
        db.close()


def do_ask(config: Config, question: str, top_k: Optional[int] = None) -> str:
    results = do_search(config, question, top_k=top_k)
    return answer(question, results, LLMClient(config.llm), context_config=config.retrieval)


def do_ask_with_metadata(config: Config, question: str, top_k: Optional[int] = None) -> Dict[str, Any]:
    results = do_search(config, question, top_k=top_k)
    return answer_with_metadata(question, results, LLMClient(config.llm), context_config=config.retrieval)


def do_answer_events(config: Config, question: str, top_k: Optional[int] = None):
    results = do_search(config, question, top_k=top_k)
    yield from answer_events(question, results, LLMClient(config.llm),
                             context_config={**config.retrieval, "top_k": top_k or config.top_k})


def parse_only(config: Config, source: Union[str, Path], llm_client: Optional[LLMClient] = None) -> StructuredPaper:
    document = parse_file(Path(source).expanduser())
    return extract_paper(document, llm_client or LLMClient(config.llm))


def _log(config: Config, data: Dict[str, Any]) -> None:
    log_path = config.root / "logs" / "researchwiki.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": utc_now(), **data}, ensure_ascii=False) + "\n")
