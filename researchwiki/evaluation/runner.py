from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..index.database import Database
from ..qa.engine import answer_with_metadata
from ..retrieval.search import search


DEFAULT_QUESTIONS: List[Dict[str, Any]] = [
    {"id": "methods", "question": "当前资料使用了哪些研究方法？", "expected_no_evidence": False},
    {"id": "datasets", "question": "当前资料涉及哪些数据集或基准？", "expected_no_evidence": False},
    {"id": "findings", "question": "当前资料报告了哪些主要结论？", "expected_no_evidence": False},
    {"id": "limitations", "question": "当前资料说明了哪些局限性？", "expected_no_evidence": False},
    {"id": "reject", "question": "__researchwiki_out_of_scope_quantum_volcano_7391__", "expected_no_evidence": True},
]


def load_questions(config: Config) -> List[Dict[str, Any]]:
    path = config.root / "evaluation" / "questions.json"
    if not path.exists():
        return [dict(value) for value in DEFAULT_QUESTIONS]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [dict(value) for value in DEFAULT_QUESTIONS]
    if not isinstance(payload, list):
        return [dict(value) for value in DEFAULT_QUESTIONS]
    questions = []
    for index, value in enumerate(payload, 1):
        if not isinstance(value, dict) or not str(value.get("question") or "").strip():
            continue
        questions.append({
            "id": str(value.get("id") or "q" + str(index)),
            "question": str(value["question"]),
            "expected_no_evidence": bool(value.get("expected_no_evidence", False)),
        })
    return questions or [dict(value) for value in DEFAULT_QUESTIONS]


def _case_metrics(result: Dict[str, Any], expected_no_evidence: bool) -> Dict[str, float]:
    sources = result.get("sources") or []
    provenance = [item for source in sources for item in (source.get("provenance") or [])]
    located = [item for item in provenance if item.get("locator") and item.get("evidence_text")]
    rejected = result.get("mode") == "no_evidence"
    retrieval_expected = rejected if expected_no_evidence else bool(sources)
    rejection_correct = rejected == expected_no_evidence
    grounded = rejected or bool(sources)
    citation_coverage = (len(located) / len(provenance)) if provenance else (1.0 if rejected else 0.0)
    return {
        "retrieval_hit": float(retrieval_expected),
        "rejection_accuracy": float(rejection_correct),
        "grounded_answer": float(grounded),
        "source_locator_coverage": round(citation_coverage, 4),
    }


def _relative(config: Config, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        return str(path.resolve().relative_to(config.root.resolve()))
    except (OSError, ValueError):
        return path.name


def _public_sources(config: Config, sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for source in sources:
        provenance = []
        for item in source.get("provenance") or []:
            provenance.append({**item, "raw_path": _relative(config, str(item.get("raw_path") or ""))})
        values.append({**source, "path": _relative(config, str(source.get("path") or "")), "provenance": provenance})
    return values


def run_evaluation(config: Config, profile: str = "optimized", questions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    profile = profile.strip().lower()
    if profile not in {"baseline", "optimized"}:
        raise ValueError("profile must be baseline or optimized")
    question_set = questions or load_questions(config)
    database = Database(config.db_path)
    try:
        cases: List[Dict[str, Any]] = []
        for item in question_set:
            question = str(item["question"])
            if profile == "baseline":
                results = database.search(question, top_k=5)
                for result in results:
                    result["sources"] = database.get_page_sources(int(result.get("page_id") or 0))
            else:
                results = search(database, question, top_k=8, vector_config={}, graph_expansion=True)
            result = answer_with_metadata(question, results, llm=None, context_config=config.retrieval)
            metrics = _case_metrics(result, bool(item.get("expected_no_evidence")))
            cases.append({
                "question_id": str(item["id"]),
                "question": question,
                "expected_no_evidence": bool(item.get("expected_no_evidence")),
                "mode": str(result.get("mode") or ""),
                "answer": str(result.get("answer") or "").replace(str(config.root.resolve()) + "/", ""),
                "sources": _public_sources(config, result.get("sources") or []),
                "metrics": metrics,
            })
        metric_names = ("retrieval_hit", "rejection_accuracy", "grounded_answer", "source_locator_coverage")
        metrics = {
            name: round(sum(case["metrics"][name] for case in cases) / max(1, len(cases)), 4)
            for name in metric_names
        }
        metrics["score"] = round(sum(metrics[name] for name in metric_names) / len(metric_names) * 100, 2)
        return database.create_evaluation_run(profile, metrics, cases)
    finally:
        database.close()
