"""Small, deterministic lexical baseline shared by QA and offline replay."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence


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
        content = str(page.get("content") or "")
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
        page["sources"] = database.get_page_sources(int(page["page_id"]))
        pages.append(page)
    return weighted_rank(question, pages, top_k=top_k, profile=profile)
