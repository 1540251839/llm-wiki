from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from ..wiki.manager import WikiManager, parse_frontmatter, wiki_links


_CJK_TOKEN = re.compile(r"[\u3400-\u9fff]+")
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_QUERY_EXPANSIONS = (
    (("标签噪声", "噪声标签"), ("label noise", "noisy label")),
    (("噪声", "噪音"), ("noise", "noisy")),
    (("法律检索", "案例检索", "案件检索"), ("legal case retrieval", "case retrieval")),
    (("不确定性",), ("uncertainty", "evidential")),
    (("数据集",), ("dataset", "benchmark")),
    (("方法",), ("method", "approach")),
)


def _fallback_search_terms(query: str) -> List[str]:
    """Create lexical FTS terms for queries containing continuous CJK text.

    SQLite's default FTS5 tokenizer does not segment Chinese text.  Keep this
    as retrieval-only logic: it does not infer paper content or create Wiki
    pages.  Overlapping 2–4 character fragments let a natural-language
    question match terms such as ``方法`` and ``数据集`` already present in a
    generated page.
    """
    terms: List[str] = []
    seen = set()
    for token in _LATIN_TOKEN.findall(query):
        lowered = token.lower()
        if lowered not in seen:
            terms.append(lowered)
            seen.add(lowered)
    for run in _CJK_TOKEN.findall(query):
        if len(run) < 2:
            continue
        for width in (4, 3, 2):
            if len(run) < width:
                continue
            for start in range(len(run) - width + 1):
                term = run[start:start + width]
                if term not in seen:
                    terms.append(term)
                    seen.add(term)
    for triggers, expansions in _QUERY_EXPANSIONS:
        if any(trigger in query for trigger in triggers):
            for term in expansions:
                if term not in seen:
                    terms.append(term)
                    seen.add(term)
    return terms


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _is_placeholder_page(content: str) -> bool:
    """Exclude pages produced by an incomplete/failed extraction from retrieval."""
    return (
        "作者：Unknown" in content
        and "未识别到摘要" in content
        and "None identified" in content
    )


class Database:
    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.fts_available = True
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT, year INTEGER,
          source_path TEXT, raw_path TEXT, status TEXT NOT NULL,
          error_message TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS wiki_pages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          type TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS links (
          source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
          relation TEXT NOT NULL, UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE IF NOT EXISTS aliases (
          entity_id INTEGER NOT NULL, alias TEXT NOT NULL,
          UNIQUE(entity_id, alias)
        );
        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, page_id INTEGER NOT NULL,
          content TEXT NOT NULL, source TEXT
        );
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
          original_name TEXT NOT NULL, original_path TEXT,
          raw_path TEXT, media_type TEXT, size_bytes INTEGER,
          created_at TEXT, updated_at TEXT, deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ingest_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
          source_path TEXT NOT NULL, status TEXT NOT NULL,
          stage TEXT NOT NULL DEFAULT 'QUEUED', quality_mode TEXT NOT NULL DEFAULT 'standard',
          attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
          error_message TEXT, created_at TEXT, updated_at TEXT,
          started_at TEXT, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS page_sources (
          page_id INTEGER NOT NULL, source_id TEXT NOT NULL,
          source_path TEXT, locator TEXT, page_number INTEGER,
          section TEXT, evidence_text TEXT, char_start INTEGER, char_end INTEGER,
          created_at TEXT,
          PRIMARY KEY(page_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT,
          page_id INTEGER, page_path TEXT, kind TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'warning', message TEXT NOT NULL,
          details TEXT, status TEXT NOT NULL DEFAULT 'OPEN',
          created_at TEXT, updated_at TEXT, resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS embeddings (
          page_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
          dimensions INTEGER NOT NULL, vector TEXT NOT NULL,
          content_hash TEXT NOT NULL, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS graph_nodes (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
          page_id INTEGER, source_id TEXT, path TEXT, source_doc TEXT,
          page_number INTEGER NOT NULL DEFAULT 0,
          char_start INTEGER NOT NULL DEFAULT 0,
          char_end INTEGER NOT NULL DEFAULT 0,
          confidence TEXT NOT NULL DEFAULT 'metadata_exact',
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS graph_edges (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
          relation TEXT NOT NULL, semantic TEXT NOT NULL,
          doc_id TEXT, page_number INTEGER NOT NULL DEFAULT 0,
          evidence TEXT, created_at TEXT,
          UNIQUE(source,target,semantic)
        );
        CREATE TABLE IF NOT EXISTS graph_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          generated_at TEXT NOT NULL, node_count INTEGER NOT NULL,
          edge_count INTEGER NOT NULL, digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluation_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
          status TEXT NOT NULL, score REAL NOT NULL DEFAULT 0,
          metrics TEXT NOT NULL, question_count INTEGER NOT NULL,
          started_at TEXT, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluation_cases (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
          question_id TEXT NOT NULL, question TEXT NOT NULL,
          expected_no_evidence INTEGER NOT NULL DEFAULT 0,
          mode TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL,
          metrics TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pages_type ON wiki_pages(type);
        CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
        CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingest_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_source ON ingest_jobs(source_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_source
          ON ingest_jobs(source_id) WHERE status IN ('PENDING','RUNNING','PAUSED');
        CREATE INDEX IF NOT EXISTS idx_page_sources_source ON page_sources(source_id, page_id);
        CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type,name);
        CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic ON graph_edges(semantic,source,target);
        CREATE INDEX IF NOT EXISTS idx_evaluation_runs_profile ON evaluation_runs(profile,id);
        """)
        paper_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(papers)")}
        if "error_message" not in paper_columns:
            self.conn.execute("ALTER TABLE papers ADD COLUMN error_message TEXT")
        page_source_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(page_sources)")}
        for name, kind in (
            ("page_number", "INTEGER"),
            ("section", "TEXT"),
            ("evidence_text", "TEXT"),
            ("char_start", "INTEGER"),
            ("char_end", "INTEGER"),
            ("paragraph", "INTEGER"),
            ("confidence", "TEXT"),
        ):
            if name not in page_source_columns:
                self.conn.execute("ALTER TABLE page_sources ADD COLUMN " + name + " " + kind)
        edge_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(graph_edges)")}
        for name, kind in (("locator", "TEXT"), ("char_start", "INTEGER"), ("char_end", "INTEGER"), ("confidence", "TEXT")):
            if name not in edge_columns:
                self.conn.execute("ALTER TABLE graph_edges ADD COLUMN " + name + " " + kind)
        self.conn.execute(
            """INSERT OR IGNORE INTO sources(
                   id,sha256,original_name,original_path,raw_path,media_type,size_bytes,created_at,updated_at
               )
               SELECT id,id,
                      CASE WHEN source_path IS NULL OR source_path='' THEN id ELSE source_path END,
                      source_path,raw_path,'',NULL,created_at,updated_at
               FROM papers"""
        )
        for row in self.conn.execute("SELECT id,original_name FROM sources"):
            original_name = str(row["original_name"] or "")
            basename = Path(original_name).name
            if basename and basename != original_name:
                self.conn.execute("UPDATE sources SET original_name=? WHERE id=?", (basename, row["id"]))
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(title, type, path, content, page_id UNINDEXED)")
        except sqlite3.OperationalError:
            self.fts_available = False
        self.conn.commit()

    def upsert_paper(self, paper_id: str, title: str, authors: List[str], year: Optional[int], source_path: str, raw_path: str, status: str, error_message: str = "") -> None:
        self.conn.execute("""INSERT INTO papers(id,title,authors,year,source_path,raw_path,status,error_message,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
          ON CONFLICT(id) DO UPDATE SET title=excluded.title,authors=excluded.authors,year=excluded.year,
          source_path=excluded.source_path,
          raw_path=CASE WHEN excluded.raw_path='' THEN papers.raw_path ELSE excluded.raw_path END,
          status=excluded.status,error_message=excluded.error_message,updated_at=datetime('now')""",
          (paper_id, title, json.dumps(authors, ensure_ascii=False), year, source_path, raw_path, status, error_message))
        self.conn.commit()

    def update_paper_status(self, paper_id: str, status: str, error_message: str = "") -> None:
        self.conn.execute(
            "UPDATE papers SET status=?, error_message=?, updated_at=datetime('now') WHERE id=?",
            (status, error_message, paper_id),
        )
        self.conn.commit()

    def register_source(
        self,
        source_id: str,
        original_path: str,
        raw_path: str,
        media_type: str = "",
        size_bytes: Optional[int] = None,
    ) -> None:
        original_name = Path(original_path).name or source_id
        self.conn.execute(
            """INSERT INTO sources(
                   id,sha256,original_name,original_path,raw_path,media_type,size_bytes,
                   created_at,updated_at,deleted_at
               ) VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'),NULL)
               ON CONFLICT(id) DO UPDATE SET
                 original_name=excluded.original_name,
                 original_path=excluded.original_path,
                 raw_path=CASE WHEN excluded.raw_path='' THEN sources.raw_path ELSE excluded.raw_path END,
                 media_type=CASE WHEN excluded.media_type='' THEN sources.media_type ELSE excluded.media_type END,
                 size_bytes=COALESCE(excluded.size_bytes,sources.size_bytes),
                 updated_at=datetime('now'),deleted_at=NULL""",
            (source_id, source_id, original_name, original_path, raw_path, media_type, size_bytes),
        )
        self.conn.commit()

    def get_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM sources WHERE id=? LIMIT 1", (source_id,)).fetchone()
        return dict(row) if row else None

    def mark_source_deleted(self, source_id: str) -> None:
        self.conn.execute(
            "UPDATE sources SET deleted_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
            (source_id,),
        )
        self.conn.execute(
            "UPDATE papers SET status='DELETED',updated_at=datetime('now') WHERE id=?",
            (source_id,),
        )
        self.conn.execute(
            "UPDATE reviews SET status='RESOLVED',resolved_at=datetime('now'),updated_at=datetime('now') WHERE source_id=? AND status='OPEN'",
            (source_id,),
        )
        self.conn.commit()

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def enqueue_ingest_job(
        self,
        source_id: str,
        source_path: str,
        quality_mode: str = "basic",
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        mode = quality_mode if quality_mode in {"basic", "standard", "high"} else "basic"
        active = self.conn.execute(
            """SELECT * FROM ingest_jobs
               WHERE source_id=? AND status IN ('PENDING','RUNNING','PAUSED')
               ORDER BY id DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
        if active:
            return self._job_dict(active)
        cursor = self.conn.execute(
            """INSERT INTO ingest_jobs(
                   source_id,source_path,status,stage,quality_mode,attempts,max_attempts,
                   error_message,created_at,updated_at
               ) VALUES(?,?,'PENDING','QUEUED',?,0,?,'',datetime('now'),datetime('now'))""",
            (source_id, source_path, mode, max(1, min(10, int(max_attempts)))),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._job_dict(row)

    def claim_next_ingest_job(self, job_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if job_id is None:
                row = self.conn.execute(
                    """SELECT * FROM ingest_jobs
                       WHERE status='PENDING' AND attempts < max_attempts
                       ORDER BY created_at,id LIMIT 1"""
                ).fetchone()
            else:
                row = self.conn.execute(
                    """SELECT * FROM ingest_jobs
                       WHERE id=? AND status='PENDING' AND attempts < max_attempts LIMIT 1""",
                    (job_id,),
                ).fetchone()
            if not row:
                self.conn.commit()
                return None
            self.conn.execute(
                """UPDATE ingest_jobs SET status='RUNNING',stage='STARTING',attempts=attempts+1,
                   started_at=datetime('now'),finished_at=NULL,error_message='',updated_at=datetime('now')
                   WHERE id=?""",
                (int(row["id"]),),
            )
            self.conn.commit()
            claimed = self.conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (int(row["id"]),)).fetchone()
            return self._job_dict(claimed) if claimed else None
        except Exception:
            self.conn.rollback()
            raise

    def update_ingest_job(
        self,
        job_id: int,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        fields = ["updated_at=datetime('now')"]
        params: List[Any] = []
        if status is not None:
            fields.append("status=?")
            params.append(status)
            if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                fields.append("finished_at=datetime('now')")
        if stage is not None:
            fields.append("stage=?")
            params.append(stage)
        if error_message is not None:
            fields.append("error_message=?")
            params.append(error_message[:2000])
        params.append(job_id)
        self.conn.execute("UPDATE ingest_jobs SET " + ",".join(fields) + " WHERE id=?", params)
        self.conn.commit()

    def get_ingest_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM ingest_jobs WHERE id=? LIMIT 1", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def latest_ingest_job(self, source_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM ingest_jobs WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return self._job_dict(row) if row else None

    def list_ingest_jobs(self, status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM ingest_jobs WHERE status=? ORDER BY id DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ingest_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._job_dict(row) for row in rows]

    def retry_ingest_job(self, job_id: int, quality_mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or str(row["status"]) not in {"FAILED", "CANCELLED", "PAUSED"}:
            return None
        attempts = int(row["attempts"] or 0)
        max_attempts = int(row["max_attempts"] or 3)
        if attempts >= max_attempts:
            self.conn.execute("UPDATE ingest_jobs SET attempts=0 WHERE id=?", (job_id,))
        mode = quality_mode if quality_mode in {"basic", "standard", "high"} else str(row["quality_mode"] or "basic")
        self.conn.execute(
            """UPDATE ingest_jobs SET status='PENDING',stage='QUEUED',error_message='',
               quality_mode=?,started_at=NULL,finished_at=NULL,updated_at=datetime('now') WHERE id=?""",
            (mode, job_id),
        )
        self.conn.commit()
        return self.get_ingest_job(job_id)

    def cancel_ingest_job(self, job_id: int) -> bool:
        cursor = self.conn.execute(
            """UPDATE ingest_jobs SET status='CANCELLED',stage='CANCELLED',
               finished_at=datetime('now'),updated_at=datetime('now')
               WHERE id=? AND status IN ('PENDING','PAUSED')""",
            (job_id,),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def fail_interrupted_jobs(self) -> int:
        cursor = self.conn.execute(
            """UPDATE ingest_jobs SET status='FAILED',stage='INTERRUPTED',
               error_message='PROCESS_INTERRUPTED: the previous Web process stopped before this job completed.',
               finished_at=datetime('now'),updated_at=datetime('now')
               WHERE status='RUNNING'"""
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def add_review(
        self,
        kind: str,
        message: str,
        source_id: Optional[str] = None,
        severity: str = "warning",
        page_id: Optional[int] = None,
        page_path: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        existing = self.conn.execute(
            """SELECT id FROM reviews WHERE kind=? AND message=?
               AND COALESCE(source_id,'')=COALESCE(?,'') AND COALESCE(page_path,'')=COALESCE(?,'')
               LIMIT 1""",
            (kind, message, source_id, page_path),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = self.conn.execute(
            """INSERT INTO reviews(
                   source_id,page_id,page_path,kind,severity,message,details,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'OPEN',datetime('now'),datetime('now'))""",
            (
                source_id,
                page_id,
                page_path,
                kind,
                severity,
                message,
                json.dumps(details or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_reviews(self, status: str = "OPEN", limit: int = 200) -> List[Dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM reviews WHERE status=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM reviews ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        values: List[Dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["details"] = json.loads(str(value.get("details") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                value["details"] = {}
            values.append(value)
        return values

    def resolve_review(self, review_id: int) -> bool:
        cursor = self.conn.execute(
            """UPDATE reviews SET status='RESOLVED',resolved_at=datetime('now'),updated_at=datetime('now')
               WHERE id=? AND status='OPEN'""",
            (review_id,),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def find_duplicate(self, file_hash: str, title: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute("SELECT * FROM papers WHERE id=? OR lower(title)=lower(?) LIMIT 1", (file_hash, title)).fetchone()
        return row

    def find_indexed_title_duplicate(self, title: str, exclude_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM papers
               WHERE id<>? AND status='INDEXED' AND lower(trim(title))=lower(trim(?))
               LIMIT 1""",
            (exclude_id, title),
        ).fetchone()

    def find_by_hash(self, file_hash: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM papers WHERE id=? LIMIT 1", (file_hash,)).fetchone()

    def get_paper(self, paper_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM papers WHERE id=? LIMIT 1", (paper_id,)).fetchone()
        return self._paper_dict(row) if row else None

    def fail_interrupted(self) -> int:
        statuses = ("UPLOADED", "EXTRACTING", "EXTRACTED", "WIKI_CREATED")
        cursor = self.conn.execute(
            """UPDATE papers
               SET status='FAILED',
                   error_message='PROCESS_INTERRUPTED: the previous Web process stopped before ingestion completed; retry this document.',
                   updated_at=datetime('now')
               WHERE status IN (?,?,?,?) AND NOT EXISTS (
                 SELECT 1 FROM ingest_jobs j WHERE j.source_id=papers.id AND j.status='PENDING'
               )""",
            statuses,
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def list_papers(
        self,
        query: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 20,
        file_type: str = "",
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        params: List[Any] = []
        if query.strip():
            like = "%" + query.strip() + "%"
            clauses.append("(title LIKE ? OR authors LIKE ? OR source_path LIKE ?)")
            params.extend([like, like, like])
        if status.strip():
            clauses.append("status = ?")
            params.append(status.strip().upper())
        else:
            clauses.append("status <> 'DELETED'")
        if file_type.strip():
            suffix = file_type.strip().lower().lstrip(".")
            suffixes = ("md", "markdown") if suffix in {"md", "markdown"} else (suffix,)
            clauses.append("(" + " OR ".join("lower(COALESCE(NULLIF(raw_path,''),source_path)) LIKE ?" for _ in suffixes) + ")")
            params.extend("%." + value for value in suffixes)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = int(self.conn.execute("SELECT count(*) FROM papers" + where, params).fetchone()[0])
        offset = max(0, page - 1) * page_size
        rows = self.conn.execute(
            "SELECT * FROM papers" + where + " ORDER BY updated_at DESC, created_at DESC, id LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return {"items": [self._paper_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def overview(self) -> Dict[str, Any]:
        statuses = {
            str(row["status"]): int(row["count"])
            for row in self.conn.execute("SELECT status, count(*) AS count FROM papers GROUP BY status")
        }
        page_types = {
            str(row["type"]): int(row["count"])
            for row in self.conn.execute("SELECT type, count(*) AS count FROM wiki_pages GROUP BY type")
        }
        return {
            "documents": sum(count for status, count in statuses.items() if status != "DELETED"),
            "statuses": statuses,
            "wiki_pages": int(self.conn.execute("SELECT count(*) FROM wiki_pages").fetchone()[0]),
            "links": int(self.conn.execute("SELECT count(*) FROM links").fetchone()[0]),
            "sources": int(self.conn.execute("SELECT count(*) FROM sources WHERE deleted_at IS NULL").fetchone()[0]),
            "open_reviews": int(self.conn.execute("SELECT count(*) FROM reviews WHERE status='OPEN'").fetchone()[0]),
            "queue": {
                str(row["status"]): int(row["count"])
                for row in self.conn.execute("SELECT status,count(*) AS count FROM ingest_jobs GROUP BY status")
            },
            "page_types": page_types,
        }

    def list_wiki_pages(self, query: str = "", page_type: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if query.strip():
            clauses.append("title LIKE ?")
            params.append("%" + query.strip() + "%")
        if page_type.strip():
            clauses.append("type = ?")
            params.append(page_type.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT id,title,type,path,created_at,updated_at FROM wiki_pages" + where + " ORDER BY type,title LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [dict(row) for row in rows]

    def count_wiki_pages(self, query: str = "", page_type: str = "") -> int:
        clauses: List[str] = []
        params: List[Any] = []
        if query.strip():
            clauses.append("title LIKE ?")
            params.append("%" + query.strip() + "%")
        if page_type.strip():
            clauses.append("type = ?")
            params.append(page_type.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return int(self.conn.execute("SELECT count(*) FROM wiki_pages" + where, params).fetchone()[0])

    def get_wiki_page(self, page_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """SELECT wp.id,wp.title,wp.type,wp.path,wp.created_at,wp.updated_at,c.content
               FROM wiki_pages wp LEFT JOIN chunks c ON c.page_id=wp.id WHERE wp.id=? LIMIT 1""",
            (page_id,),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["sources"] = self.get_page_sources(page_id)
        return value

    def get_page_sources(self, page_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT ps.source_id,ps.source_path,ps.locator,ps.page_number,
                      ps.section,ps.evidence_text,ps.char_start,ps.char_end,ps.paragraph,ps.confidence,
                      s.original_name,s.raw_path,s.sha256
               FROM page_sources ps LEFT JOIN sources s ON s.id=ps.source_id
               WHERE ps.page_id=? ORDER BY s.original_name,ps.source_id""",
            (page_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def enrich_source_evidence(self, source_id: str, pages: Iterable[Any]) -> int:
        """Attach a reproducible page/character locator to every derived Wiki page.

        The locator points into the immutable parsed source, not into generated Wiki
        prose. Exact page-title matches are preferred. When a derived title is not
        present verbatim (for example, an LLM-normalized topic), the first non-empty
        source page is retained as a clearly labelled fallback excerpt.
        """
        source_pages = [
            (int(getattr(page, "number", 0) or 0), str(getattr(page, "text", "") or ""))
            for page in pages
            if str(getattr(page, "text", "") or "").strip()
        ]
        if not source_pages:
            return 0
        rows = self.conn.execute(
            """SELECT wp.id,wp.title,wp.type,c.content
               FROM page_sources ps JOIN wiki_pages wp ON wp.id=ps.page_id
               LEFT JOIN chunks c ON c.page_id=wp.id
               WHERE ps.source_id=? ORDER BY wp.type,wp.title""",
            (source_id,),
        ).fetchall()
        updated = 0
        for row in rows:
            title = str(row["title"] or "").strip()
            match_page = source_pages[0]
            match_start = -1
            for page_number, text in source_pages:
                start = text.casefold().find(title.casefold()) if title else -1
                if start >= 0:
                    match_page = (page_number, text)
                    match_start = start
                    break
            page_number, text = match_page
            if match_start < 0:
                # Try a distinctive generated statement before falling back to page 1.
                content = re.sub(r"^---.*?---", "", str(row["content"] or ""), flags=re.S)
                candidates = [
                    value.strip()
                    for value in re.split(r"\n+", content)
                    if len(value.strip()) >= 24 and not value.lstrip().startswith(("#", "- [["))
                ]
                for candidate in candidates[:8]:
                    needle = candidate[:80]
                    for candidate_page, candidate_text in source_pages:
                        start = candidate_text.casefold().find(needle.casefold())
                        if start >= 0:
                            page_number, text, match_start = candidate_page, candidate_text, start
                            break
                    if match_start >= 0:
                        break
            exact = match_start >= 0
            if match_start < 0:
                match_start = 0
            paragraph_start = max(0, text.rfind("\n", 0, match_start) + 1)
            paragraph_end = text.find("\n\n", match_start)
            if paragraph_end < 0 or paragraph_end - paragraph_start > 900:
                paragraph_end = min(len(text), max(match_start + max(len(title), 1), paragraph_start + 650))
            char_end = min(len(text), paragraph_end, paragraph_start + 700)
            evidence = text[paragraph_start:char_end]
            locator = f"page:{page_number}#chars={paragraph_start}-{char_end}"
            section = title if exact else "source excerpt (fallback)"
            self.conn.execute(
                """UPDATE page_sources SET locator=?,page_number=?,section=?,evidence_text=?,
                     char_start=?,char_end=?,paragraph=?,confidence=? WHERE page_id=? AND source_id=?""",
                (locator, page_number, section, evidence, paragraph_start, char_end,
                 len(re.findall(r"\n\s*\n", text[:paragraph_start])) + 1,
                 "exact_text" if exact else "source_excerpt_fallback", int(row["id"]), source_id),
            )
            updated += 1
        self.conn.commit()
        return updated

    def list_pages_for_source(self, source_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT wp.id,wp.title,wp.type,wp.path,wp.created_at,wp.updated_at
               FROM page_sources ps JOIN wiki_pages wp ON wp.id=ps.page_id
               WHERE ps.source_id=? ORDER BY wp.type,wp.title""",
            (source_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_embedding(
        self,
        page_id: int,
        model: str,
        vector: Sequence[float],
        content_hash: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO embeddings(page_id,model,dimensions,vector,content_hash,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))
               ON CONFLICT(page_id) DO UPDATE SET model=excluded.model,
                 dimensions=excluded.dimensions,vector=excluded.vector,
                 content_hash=excluded.content_hash,updated_at=datetime('now')""",
            (page_id, model, len(vector), json.dumps(list(vector)), content_hash),
        )
        self.conn.commit()

    def get_embedding_record(self, page_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM embeddings WHERE page_id=?", (page_id,)).fetchone()
        return dict(row) if row else None

    def list_embeddings(self, model: str = "") -> List[Dict[str, Any]]:
        if model:
            rows = self.conn.execute(
                """SELECT e.*,wp.title,wp.type,wp.path,c.content
                   FROM embeddings e JOIN wiki_pages wp ON wp.id=e.page_id
                   LEFT JOIN chunks c ON c.page_id=e.page_id WHERE e.model=?""",
                (model,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT e.*,wp.title,wp.type,wp.path,c.content
                   FROM embeddings e JOIN wiki_pages wp ON wp.id=e.page_id
                   LEFT JOIN chunks c ON c.page_id=e.page_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def related_pages(self, page_id: int, limit: int = 12) -> List[Dict[str, Any]]:
        page = self.conn.execute("SELECT id,title,type,path FROM wiki_pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            return []
        pages = {int(row["id"]): dict(row) for row in self.conn.execute("SELECT id,title,type,path FROM wiki_pages")}
        adjacency: Dict[int, set] = {value: set() for value in pages}
        for row in self.conn.execute("SELECT source_id,target_id FROM links"):
            source_id, target_id = int(row["source_id"]), int(row["target_id"])
            adjacency.setdefault(source_id, set()).add(target_id)
            adjacency.setdefault(target_id, set()).add(source_id)
        own_sources = {
            str(row["source_id"])
            for row in self.conn.execute("SELECT source_id FROM page_sources WHERE page_id=?", (page_id,))
        }
        scores: Dict[int, float] = {}
        reasons: Dict[int, List[str]] = {}
        for candidate_id, candidate in pages.items():
            if candidate_id == page_id:
                continue
            score = 0.0
            candidate_reasons: List[str] = []
            if candidate_id in adjacency.get(page_id, set()):
                score += 3.0
                candidate_reasons.append("direct_link")
            candidate_sources = {
                str(row["source_id"])
                for row in self.conn.execute("SELECT source_id FROM page_sources WHERE page_id=?", (candidate_id,))
            }
            overlap = len(own_sources & candidate_sources)
            if overlap:
                score += 4.0 * overlap
                candidate_reasons.append("shared_source")
            common = adjacency.get(page_id, set()) & adjacency.get(candidate_id, set())
            if common:
                score += 1.5 * len(common)
                candidate_reasons.append("common_neighbor")
            if str(candidate["type"]) == str(page["type"]):
                score += 0.5
                candidate_reasons.append("same_type")
            if score > 0:
                scores[candidate_id] = score
                reasons[candidate_id] = candidate_reasons
        ranked = sorted(scores, key=lambda value: (-scores[value], str(pages[value]["title"]).casefold()))[:limit]
        return [{**pages[value], "score": scores[value], "reasons": reasons[value]} for value in ranked]

    def replace_graph(self, nodes: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]], digest: str) -> Dict[str, Any]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM graph_edges")
            self.conn.execute("DELETE FROM graph_nodes")
            for node in nodes:
                self.conn.execute(
                    """INSERT INTO graph_nodes(
                         id,name,type,page_id,source_id,path,source_doc,page_number,
                         char_start,char_end,confidence,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (
                        node["id"], node["name"], node["type"], node.get("page_id"),
                        node.get("source_id"), node.get("path", ""), node.get("source_doc", ""),
                        int(node.get("page_number") or 0), int(node.get("char_start") or 0),
                        int(node.get("char_end") or 0), node.get("confidence", "metadata_exact"),
                    ),
                )
            node_ids = {str(node["id"]) for node in nodes}
            for edge in edges:
                if str(edge["source"]) not in node_ids or str(edge["target"]) not in node_ids:
                    raise ValueError("graph edge references a missing endpoint: " + str(edge["id"]))
                if str(edge["source"]) == str(edge["target"]):
                    raise ValueError("graph self-loop is not allowed: " + str(edge["id"]))
                self.conn.execute(
                    """INSERT INTO graph_edges(
                         id,source,target,relation,semantic,doc_id,page_number,evidence,locator,char_start,char_end,confidence,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (
                        edge["id"], edge["source"], edge["target"], edge["relation"],
                        edge["semantic"], edge.get("doc_id", ""), int(edge.get("page_number") or 0),
                        edge.get("evidence", ""),
                        edge.get("locator", ""), int(edge.get("char_start") or 0),
                        int(edge.get("char_end") or 0), edge.get("confidence", "metadata_exact"),
                    ),
                )
            self.conn.execute(
                "INSERT INTO graph_runs(generated_at,node_count,edge_count,digest) VALUES(datetime('now'),?,?,?)",
                (len(nodes), len(edges), digest),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.graph_overview()

    def list_graph_nodes(self, node_type: str = "", query: str = "", limit: int = 2000) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if node_type.strip():
            clauses.append("type=?")
            params.append(node_type.strip().upper())
        if query.strip():
            clauses.append("name LIKE ?")
            params.append("%" + query.strip() + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            "SELECT * FROM graph_nodes" + where + " ORDER BY type,name LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [dict(row) for row in rows]

    def list_graph_edges(self, semantic: str = "", limit: int = 10000) -> List[Dict[str, Any]]:
        if semantic.strip():
            rows = self.conn.execute(
                "SELECT * FROM graph_edges WHERE semantic=? ORDER BY source,target LIMIT ?",
                (semantic.strip().upper(), limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM graph_edges ORDER BY semantic,source,target LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def graph_overview(self) -> Dict[str, Any]:
        run = self.conn.execute("SELECT * FROM graph_runs ORDER BY id DESC LIMIT 1").fetchone()
        node_types = {
            str(row["type"]): int(row["count"])
            for row in self.conn.execute("SELECT type,count(*) AS count FROM graph_nodes GROUP BY type")
        }
        edge_semantics = {
            str(row["semantic"]): int(row["count"])
            for row in self.conn.execute("SELECT semantic,count(*) AS count FROM graph_edges GROUP BY semantic")
        }
        return {
            "mode": "deterministic-local-contract",
            "generated_at": run["generated_at"] if run else None,
            "digest": run["digest"] if run else None,
            "stats": {"nodes": sum(node_types.values()), "edges": sum(edge_semantics.values())},
            "node_types": node_types,
            "edge_semantics": edge_semantics,
        }

    def graph(self, limit: int = 500) -> Dict[str, Any]:
        overview = self.graph_overview()
        nodes = self.list_graph_nodes(limit=limit)
        node_ids = {str(node["id"]) for node in nodes}
        edges = [
            edge for edge in self.list_graph_edges(limit=max(limit * 8, 2000))
            if str(edge["source"]) in node_ids and str(edge["target"]) in node_ids
        ]
        return {**overview, "nodes": nodes, "edges": edges, "truncated": overview["stats"]["nodes"] > len(nodes)}

    def create_evaluation_run(self, profile: str, metrics: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        score = float(metrics.get("score") or 0.0)
        cursor = self.conn.execute(
            """INSERT INTO evaluation_runs(profile,status,score,metrics,question_count,started_at,finished_at)
               VALUES(?,'SUCCEEDED',?,?,?,datetime('now'),datetime('now'))""",
            (profile, score, json.dumps(metrics, ensure_ascii=False), len(cases)),
        )
        run_id = int(cursor.lastrowid)
        for case in cases:
            self.conn.execute(
                """INSERT INTO evaluation_cases(
                     run_id,question_id,question,expected_no_evidence,mode,answer,sources,metrics
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id, case["question_id"], case["question"], int(bool(case.get("expected_no_evidence"))),
                    case["mode"], case["answer"], json.dumps(case.get("sources") or [], ensure_ascii=False),
                    json.dumps(case.get("metrics") or {}, ensure_ascii=False),
                ),
            )
        self.conn.commit()
        return self.get_evaluation_run(run_id) or {}

    def get_evaluation_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["metrics"] = json.loads(str(value.get("metrics") or "{}"))
        cases = self.conn.execute("SELECT * FROM evaluation_cases WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        value["cases"] = []
        for case_row in cases:
            case = dict(case_row)
            case["sources"] = json.loads(str(case.get("sources") or "[]"))
            case["metrics"] = json.loads(str(case.get("metrics") or "{}"))
            case["expected_no_evidence"] = bool(case.get("expected_no_evidence"))
            value["cases"].append(case)
        return value

    def list_evaluation_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM evaluation_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        values: List[Dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(str(value.get("metrics") or "{}"))
            values.append(value)
        return values

    @staticmethod
    def _paper_dict(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        try:
            value["authors"] = json.loads(str(value.get("authors") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value["authors"] = []
        return value

    def rebuild(self, wiki: WikiManager) -> Dict[str, int]:
        pages = list(wiki.all_pages())
        existing = {row["path"]: row for row in self.conn.execute("SELECT * FROM wiki_pages")}
        source_rows = [dict(row) for row in self.conn.execute(
            """SELECT p.id,p.title,p.source_path,p.raw_path
               FROM papers p LEFT JOIN sources s ON s.id=p.id
               WHERE p.status<>'DELETED' AND COALESCE(s.deleted_at,'')=''"""
        )]
        source_by_title = {str(row["title"]).casefold(): str(row["id"]) for row in source_rows}
        source_by_path: Dict[str, str] = {}
        source_paths: Dict[str, str] = {}
        for row in source_rows:
            source_id = str(row["id"])
            source_paths[source_id] = str(row.get("raw_path") or row.get("source_path") or "")
            for value in (row.get("raw_path"), row.get("source_path")):
                if value:
                    source_by_path[str(value)] = source_id
                    source_by_path[Path(str(value)).as_posix()] = source_id
        page_ids: Dict[str, int] = {}
        for path in pages:
            content = path.read_text(encoding="utf-8")
            front = parse_frontmatter(content)
            page_type = str(front.get("type") or path.parent.name.rstrip("s"))
            title = str(front.get("title") or front.get("name") or path.stem.replace("-", " "))
            old = existing.get(str(path))
            if old:
                page_id = int(old["id"])
                self.conn.execute("UPDATE wiki_pages SET title=?, type=?, updated_at=datetime('now') WHERE id=?", (title, page_type, page_id))
            else:
                cursor = self.conn.execute("INSERT INTO wiki_pages(title,type,path,created_at,updated_at) VALUES(?,?,?,datetime('now'),datetime('now'))", (title, page_type, str(path)))
                page_id = int(cursor.lastrowid)
            page_ids[str(path)] = page_id
            self.conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
            self.conn.execute("INSERT INTO chunks(page_id,content,source) VALUES(?,?,?)", (page_id, content, str(path)))
            self.conn.execute("DELETE FROM page_sources WHERE page_id=?", (page_id,))
            declared = front.get("sources") or front.get("source_hash") or []
            if isinstance(declared, str):
                declared_sources = [declared]
            elif isinstance(declared, list):
                declared_sources = [str(value) for value in declared]
            else:
                declared_sources = []
            source_value = str(front.get("source") or "")
            if source_value:
                matched = source_by_path.get(source_value) or source_by_path.get(Path(source_value).as_posix())
                if not matched:
                    prefix = Path(source_value).name.split("-", 1)[0]
                    prefix_matches = [source_id for source_id in source_paths if len(prefix) >= 8 and source_id.startswith(prefix)]
                    if len(prefix_matches) == 1:
                        matched = prefix_matches[0]
                if matched:
                    declared_sources.append(matched)
            title_source = source_by_title.get(title.casefold())
            if page_type == "paper" and title_source:
                declared_sources.append(title_source)
            for linked_title in wiki_links(content):
                linked_source = source_by_title.get(linked_title.casefold())
                if linked_source:
                    declared_sources.append(linked_source)
            for source_id in dict.fromkeys(value for value in declared_sources if value):
                known = self.conn.execute("SELECT id FROM sources WHERE id=? AND deleted_at IS NULL", (source_id,)).fetchone()
                if not known:
                    continue
                self.conn.execute(
                    """INSERT OR IGNORE INTO page_sources(page_id,source_id,source_path,locator,created_at)
                       VALUES(?,?,?,'',datetime('now'))""",
                    (page_id, source_id, source_paths.get(source_id, "")),
                )
            if self.fts_available:
                self.conn.execute("DELETE FROM wiki_fts WHERE page_id=?", (page_id,))
                self.conn.execute("INSERT INTO wiki_fts(title,type,path,content,page_id) VALUES(?,?,?,?,?)", (title, page_type, str(path), content, page_id))
        stale_paths = set(existing) - set(page_ids)
        for stale in stale_paths:
            page_id = int(existing[stale]["id"])
            self.conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
            self.conn.execute("DELETE FROM page_sources WHERE page_id=?", (page_id,))
            self.conn.execute("DELETE FROM embeddings WHERE page_id=?", (page_id,))
            self.conn.execute("DELETE FROM wiki_pages WHERE id=?", (page_id,))
            if self.fts_available:
                self.conn.execute("DELETE FROM wiki_fts WHERE page_id=?", (page_id,))
        self.conn.execute("DELETE FROM links")
        link_count = 0
        by_title = {str(self.conn.execute("SELECT title FROM wiki_pages WHERE id=?", (page_id,)).fetchone()[0]).lower(): page_id for page_id in page_ids.values()}
        for path, source_id in page_ids.items():
            for target in wiki_links(Path(path).read_text(encoding="utf-8")):
                target_id = by_title.get(target.lower())
                if target_id and target_id != source_id:
                    self.conn.execute("INSERT OR IGNORE INTO links(source_id,target_id,relation) VALUES(?,?,?)", (source_id, target_id, "REFERENCES"))
                    link_count += 1
        # Legacy entity pages did not carry sources[] in frontmatter. Propagate
        # provenance across explicit Wiki links after direct mappings are known.
        for _ in range(3):
            self.conn.execute(
                """INSERT OR IGNORE INTO page_sources(page_id,source_id,source_path,locator,created_at)
                   SELECT l.source_id,ps.source_id,ps.source_path,ps.locator,datetime('now')
                   FROM links l JOIN page_sources ps ON ps.page_id=l.target_id
                   WHERE NOT EXISTS (
                     SELECT 1 FROM page_sources own WHERE own.page_id=l.source_id
                   )"""
            )
        self.conn.commit()
        return {"pages": len(pages), "links": link_count, "stale": len(stale_paths)}

    def search(self, query: str, top_k: int = 8) -> List[Dict[str, Any]]:
        fetch_limit = max(top_k * 4, 16)
        if self.fts_available:
            try:
                rows = self.conn.execute("SELECT title,type,path,content,page_id,bm25(wiki_fts) AS score FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY score LIMIT ?", (query, fetch_limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                # Natural-language questions often contain stop words or punctuation.
                # A relaxed OR query keeps offline ask useful without pretending to do
                # semantic retrieval.
                stopwords = {"what", "which", "how", "why", "is", "are", "the", "a", "an", "in", "of", "to", "and", "or", "this", "that"}
                tokens = [term for term in _fallback_search_terms(query) if term not in stopwords]
                match = " OR ".join(_fts_quote(token) for token in tokens)
                if match:
                    rows = self.conn.execute("SELECT title,type,path,content,page_id,bm25(wiki_fts) AS score FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY score LIMIT ?", (match, fetch_limit)).fetchall()
        else:
            like = "%" + query + "%"
            rows = self.conn.execute("SELECT wp.title,wp.type,wp.path,c.content,wp.id AS page_id,0 AS score FROM wiki_pages wp JOIN chunks c ON c.page_id=wp.id WHERE c.content LIKE ? OR wp.title LIKE ? LIMIT ?", (like, like, fetch_limit)).fetchall()
        return [dict(row) for row in rows if not _is_placeholder_page(str(row["content"]))][:top_k]
