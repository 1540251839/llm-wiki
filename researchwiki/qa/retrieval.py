"""Small, deterministic lexical baseline shared by QA and offline replay."""
from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence, Optional, Set, Tuple


ALIASES = {
    "数据集": ("dataset", "benchmark"), "基准": ("benchmark", "dataset"),
    "方法": ("method", "approach"), "局限": ("limitation", "limitations"),
    "限制": ("limitation", "limitations"), "结论": ("finding", "findings", "result"),
    "结果": ("result", "results", "finding"), "指标": ("metric", "ndcg"),
    "噪声": ("noise", "noisy"), "不确定性": ("uncertainty", "evidential"),
    "概念": ("concept",), "规则": ("rule",), "事实": ("fact",),
    "候选": ("candidate",), "查询": ("query", "queries"),
    "对比": ("compare", "comparison"), "比较": ("compare", "comparison"),
    "骨干": ("backbone",), "损失": ("loss",),
}
STOPWORDS = {"what", "which", "how", "why", "is", "are", "the", "a", "an", "in", "of", "to", "and", "or", "this", "that", "does", "do", "for", "with", "please", "paper", "papers", "source", "sources", "cite", "give", "many"}
GENERIC_ZH = ("请给出", "请说明", "请问", "告诉我", "是什么", "有哪些", "什么", "如何", "为什么", "多少", "有没有", "当前", "资料", "论文", "引用", "证据", "请", "的", "了", "和", "与", "中", "吗", "呢")


def factual_text(content: str) -> str:
    """Protected notes and unreviewed model prose are not research evidence."""
    value = str(content or "")
    for marker in ("user-notes", "pending-reading"):
        value = re.sub(r"<!--\s*" + marker + r":start\s*-->.*?(?:<!--\s*" + marker + r":end\s*-->|\Z)", "", value, flags=re.S)
    return value


def named_paper_scope(database: Any, question: str) -> Tuple[Optional[Set[str]], str]:
    """A complete known title is an explicit source constraint, not a keyword.

    Short acronyms/generic one-word labels do not scope a question. Exact title
    comparisons retain every separately named paper; nested title matches pick
    the longest title rather than accidentally admitting a different paper.
    """
    from ..wiki.manager import normalize_name
    normalized = normalize_name(question)
    records = database.list_papers(page_size=100000).get("items", [])
    matches = []
    for record in records:
        title = normalize_name(str(record.get("title") or ""))
        if not (len(re.findall(r"[\u3400-\u9fff]", title)) >= 6 or
                len(re.sub(r"\s+", "", title)) >= 12 and len(title.split()) >= 2):
            continue
        pattern = re.escape(title)
        if not re.search(r"[\u3400-\u9fff]", title):
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
        for match in re.finditer(pattern, normalized):
            matches.append((match.start(), match.end(), str(record["id"])))
    if not matches:
        return None, question
    selected, spans = set(), []
    for start, end, source_id in sorted(matches, key=lambda value: (-(value[1] - value[0]), value[0], value[2])):
        # Equal spans (duplicate paper titles) keep each source; only strict
        # shorter nested matches are excluded.
        if any(first <= start and end <= last and (first, last) != (start, end) for first, last in spans):
            continue
        selected.add(source_id)
        spans.append((start, end))
    remainder = normalized
    for start, end in sorted(set(spans), reverse=True):
        remainder = remainder[:start] + " " * (end - start) + remainder[end:]
    return selected, remainder.strip()


def query_terms(question: str, optimized: bool = True) -> List[str]:
    value = question.casefold()
    terms = [token for token in re.findall(r"[a-z0-9][a-z0-9_-]*", value) if token not in STOPWORDS and len(token) > 1]
    for generic in GENERIC_ZH:
        value = value.replace(generic, " ")
    for phrase in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(phrase) >= 2:
            terms.append(phrase)
            terms.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
    if optimized:
        for key, aliases in ALIASES.items():
            if key in question:
                terms.extend(aliases)
    return sorted(set(terms))


def weighted_rank(question: str, pages: Sequence[Dict[str, Any]], top_k: int = 8, profile: str = "optimized") -> List[Dict[str, Any]]:
    """title=8, headings=3, body=1; exact acronym/name matches anchor retrieval."""
    terms = query_terms(question, optimized=profile == "optimized")
    explicit_names = [term for term in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", question) if term.casefold() not in STOPWORDS and (term.isupper() or any(c.isupper() for c in term[1:]))]
    ranked = []
    for page in pages:
        content = factual_text(str(page.get("content") or ""))
        body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, flags=re.S).casefold()
        title = str(page.get("title") or "").casefold()
        headings = " ".join(re.findall(r"^#{1,6}\s+(.+)$", body, flags=re.M))
        score = sum(8 * (term in title) + 3 * (term in headings) + min(body.count(term), 3) for term in terms)
        if explicit_names and not any(term.casefold() in title + " " + body for term in explicit_names) and not (len(title) >= 4 and title in question.casefold()):
            continue
        if score <= 0 or not body.strip():
            continue
        ranked.append({**page, "score": float(score), "retrieval_signals": ["weighted_lexical", profile]})
    ranked.sort(key=lambda item: (-item["score"], str(item.get("title") or "").casefold(), str(item.get("path") or "")))
    return ranked[:top_k]


def retrieve_evidence(database: Any, question: str, top_k: int = 8, profile: str = "optimized") -> List[Dict[str, Any]]:
    rows = database.conn.execute("SELECT wp.id AS page_id,wp.title,wp.type,wp.path,c.content FROM wiki_pages wp JOIN chunks c ON c.page_id=wp.id ORDER BY wp.path").fetchall()
    pages = []
    for row in rows:
        page = dict(row)
        pages.append(page)
    ranked = weighted_rank(question, pages, top_k=top_k, profile=profile)
    for page in ranked:
        page["sources"] = database.get_page_sources(int(page["page_id"]))
    return ranked


def _validated_span(config: Any, database: Any, span: Dict[str, Any], documents: Dict[str, Any]) -> Dict[str, Any]:
    """Check the current immutable file and parser slice, never a model locator."""
    from ..parsing.pdf import parse_file
    source_id = str(span.get("source_id") or span.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_id):
        return {}
    if source_id not in documents:
        record = database.get_source(source_id) or {}
        if not record or record.get("deleted_at"):
            documents[source_id] = None
        else:
            raw_path = Path(str(record.get("raw_path") or span.get("raw_path") or ""))
            if not raw_path.is_absolute():
                raw_path = config.root / raw_path
            try:
                digest = hashlib.sha256()
                with raw_path.open("rb") as handle:
                    for part in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(part)
                if digest.hexdigest() != source_id:
                    raise ValueError("source hash changed")
                documents[source_id] = (parse_file(raw_path), record, raw_path)
            except (OSError, ValueError, RuntimeError):
                documents[source_id] = None
    cached = documents[source_id]
    if cached is None:
        return {}
    document, record, raw_path = cached
    try:
        page_number, start, end = int(span.get("page_number")), int(span.get("char_start")), int(span.get("char_end"))
        page = next(page for page in document.pages if page.number == page_number)
        quote = str(span.get("evidence_text") or span.get("quote") or span.get("excerpt") or span.get("content") or "")
        if not quote.strip() or not 0 <= start < end <= len(page.text) or page.text[start:end] != quote:
            return {}
    except (StopIteration, TypeError, ValueError):
        return {}
    return {**span, "source_id": source_id, "raw_path": str(raw_path),
            "original_name": record.get("original_name") or span.get("original_name") or raw_path.name,
            "page_number": page_number, "char_start": start, "char_end": end,
            "evidence_text": quote, "quote": quote,
            "locator": f"page:{page_number}#chars={start}-{end}", "source_hash_verified": True}


def retrieve_hybrid(config: Any, database: Any, question: str, top_k: int = 8,
                    evidence_scope: str = "hybrid") -> List[Dict[str, Any]]:
    """Recall supported Wiki claims and exact original-text spans in one pass.

    A retrieval identity names an evidence object, never a shared SQLite integer
    or just its source document. This list is already ranked for QA consumption.
    """
    if evidence_scope not in {"hybrid", "wiki"}:
        raise ValueError("evidence_scope must be hybrid or wiki")
    if not question.strip() or top_k <= 0:
        return []
    from ..retrieval.search import search
    top_k = min(int(top_k), 100)
    recall_k = min(100, max(top_k * 4, 24))
    source_scope, residual_question = named_paper_scope(database, question)
    wiki_pages = search(database, question, top_k=recall_k,
                        vector_config=config.retrieval.get("vector", {}),
                        graph_expansion=bool(config.retrieval.get("graph_expansion", True)))
    try:
        manifest = json.loads((config.wiki_root / "manifest.json").read_text(encoding="utf-8"))
        claims_by_path = {str((config.root / page["path"]).resolve()): page.get("claims", [])
                          for page in manifest.get("pages", []) if isinstance(page, dict)}
    except (OSError, ValueError, TypeError, KeyError):
        claims_by_path = {}
    documents: Dict[str, Any] = {}
    supported, seen = [], set()
    for page in wiki_pages:
        path = Path(str(page.get("path") or ""))
        if not path.is_absolute():
            path = config.root / path
        for claim in claims_by_path.get(str(path.resolve()), []):
            if not isinstance(claim, dict) or (claim.get("verification") or {}).get("status") != "supported":
                continue
            source_id, claim_id = str(claim.get("source_id") or ""), str(claim.get("claim_id") or "")
            if source_scope is not None and source_id not in source_scope:
                continue
            if not claim_id or not str(claim.get("statement") or "").strip():
                continue
            key = "wiki:" + source_id + ":" + claim_id
            if key in seen:
                continue
            span = _validated_span(config, database, {**(claim.get("evidence") or {}), "source_id": source_id,
                                   "claim_id": claim_id, "verification_status": "supported"}, documents)
            if not span:
                continue
            candidate = {**page, "retrieval_key": key, "evidence_type": "supported_claim", "claim_id": claim_id,
                         "verification_status": "supported", "content": str(claim["statement"]), "sources": [span]}
            # A match elsewhere in the page cannot make an unrelated claim relevant.
            matched = weighted_rank(question, [candidate], top_k=1)
            if not matched and not any(signal in page.get("retrieval_signals", []) for signal in ("vector", "graph")):
                continue
            seen.add(key)
            supported.append(candidate)
    supported.sort(key=lambda value: (-float(value.get("hybrid_score") or 0),
                   -float(next(iter(weighted_rank(question, [value], top_k=1)), {}).get("score") or 0), value["retrieval_key"]))
    original = []
    if evidence_scope == "hybrid":
        if source_scope is None:
            raw_rows = database.search_source_chunks(question, limit=min(40, recall_k))
        else:
            raw_rows = [row for source_id in sorted(source_scope)
                        for row in database.search_source_chunks(residual_question or question,
                                   limit=min(40, recall_k), source_id=source_id)]
        for row in raw_rows:
            if source_scope is not None and str(row.get("source_id") or "") not in source_scope:
                continue
            span = _validated_span(config, database, row, documents)
            if not span:
                continue
            key = "raw:" + span["source_id"] + ":" + str(span["page_number"]) + ":" + str(span["char_start"]) + ":" + str(span["char_end"])
            if key in seen:
                continue
            seen.add(key)
            span.update(confidence="raw_excerpt", verification_status="raw_excerpt", evidence_type="raw_excerpt")
            original.append({"retrieval_key": key, "evidence_type": "raw_excerpt", "verification_status": "raw_excerpt",
                             "title": str(row.get("title") or span["original_name"]), "type": "source",
                             "path": span["raw_path"], "content": span["evidence_text"], "sources": [span],
                             "retrieval_signals": ["original_text"], "score": float(row.get("score") or 0)})
    # Supported findings lead; reserve original-text coverage when both exist.
    wiki_slots = top_k if not original else max(1, top_k - max(1, top_k // 3))
    values = supported[:wiki_slots] + original[:max(0, top_k - min(len(supported), wiki_slots))]
    if len(values) < top_k:
        values.extend(supported[wiki_slots:wiki_slots + top_k - len(values)])
    return values[:top_k]
