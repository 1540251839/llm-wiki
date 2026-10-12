from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..index.database import Database
from ..qa.engine import answer_with_metadata, citation_validation
from ..qa.retrieval import weighted_rank
from .questions import DEFAULT_QUESTIONS


METRIC_NAMES = ("completion", "retrieval_hit", "evidence_coverage", "citation_validity", "faithfulness", "honest_refusal", "robustness")
RULES_VERSION = "paper-replay-v3"
FIXED_CLOCK = "2026-01-01T00:00:00Z"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_questions(config: Config) -> List[Dict[str, Any]]:
    path = config.root / "evaluation" / "questions.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else copy.deepcopy(DEFAULT_QUESTIONS)
    if not isinstance(payload, list) or not payload:
        raise ValueError("Evaluation questions must be a non-empty JSON array")
    seen = set()
    for item in payload:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip() or not str(item.get("question") or "").strip():
            raise ValueError("Each evaluation question needs id and question")
        if item["id"] in seen:
            raise ValueError("Duplicate evaluation question id: " + str(item["id"]))
        seen.add(item["id"])
    return payload


def _relative(config: Config, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve().relative_to(config.root.resolve()))
    except (OSError, ValueError):
        return path.name


def snapshot_corpus(config: Config) -> List[Dict[str, Any]]:
    """A consistent read-only transaction; no ingestion or answer side effects."""
    if not config.db_path.exists():
        return []
    connection = sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        rows = connection.execute("SELECT wp.id AS page_id,wp.title,wp.type,wp.path,c.content FROM wiki_pages wp JOIN chunks c ON c.page_id=wp.id ORDER BY wp.path,c.id").fetchall()
        pages = []
        for row in rows:
            sources = connection.execute("SELECT ps.*,s.original_name,s.raw_path,s.sha256 FROM page_sources ps LEFT JOIN sources s ON s.id=ps.source_id WHERE ps.page_id=? ORDER BY ps.source_id,ps.locator", (row["page_id"],)).fetchall()
            provenance = []
            for source in sources:
                value = dict(source)
                value.pop("page_id", None)
                value.pop("id", None)
                value.pop("created_at", None)
                value.pop("updated_at", None)
                for key in ("raw_path", "source_path", "original_path"):
                    if key in value:
                        value[key] = _relative(config, str(value[key] or ""))
                provenance.append(value)
            content = str(row["content"] or "").replace(str(config.root.resolve()) + "/", "")
            pages.append({"title": row["title"], "type": row["type"], "path": _relative(config, row["path"]), "content": content, "sources": provenance})
        return pages
    finally:
        connection.close()


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _case_metrics(result: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
    sources = result.get("sources") or []
    claims = result.get("claims") or []
    reject_expected = bool(expected.get("expected_no_evidence"))
    expected_mode = str(expected.get("expected_mode") or ("no_evidence" if reject_expected else "local_evidence"))
    answered = result.get("mode") == "local_evidence"
    refused = result.get("mode") in {"no_evidence", "clarification"}
    expected_titles = expected.get("expected_source_titles") or []
    titles = [str(source.get("title") or "").casefold() for source in sources]
    hit = (sum(any(str(wanted).casefold() in title for title in titles) for wanted in expected_titles) / len(expected_titles)) if expected_titles else float(bool(sources))
    claim_text = _normal(" ".join(str(claim.get("text") or "") for claim in claims))
    expected_terms = expected.get("expected_terms") or []
    coverage = (sum(_normal(str(term)) in claim_text for term in expected_terms) / len(expected_terms)) if expected_terms else None
    by_index = {source["citation_index"]: source for source in sources}
    supported = [bool(claim.get("text")) and _normal(claim["text"]) in _normal(str(by_index.get(claim.get("citation_index"), {}).get("wiki_excerpt") or "")) for claim in claims]
    honest = result.get("mode") == expected_mode if reject_expected else not refused
    metrics: Dict[str, Any] = {
        "completion": float(bool(str(result.get("answer") or "").strip()) and result.get("mode") in {"local_evidence", "no_evidence", "clarification"}),
        "retrieval_hit": None if reject_expected else round(hit, 4),
        "evidence_coverage": None if reject_expected else (round(coverage, 4) if coverage is not None else None),
        "citation_validity": None if reject_expected else float(citation_validation(result.get("answer", ""), sources)["valid"]),
        "faithfulness": None if not claims else round(sum(supported) / len(supported), 4),
        "honest_refusal": float(honest),
        "robustness": float(honest and (reject_expected or (answered and hit == 1.0 and (coverage is None or coverage == 1.0)))) if expected.get("category") in {"adversarial", "robustness", "clarification"} else None,
    }
    located = [evidence for source in sources for evidence in source.get("provenance") or [] if evidence.get("locator") and evidence.get("evidence_text")]
    total = sum(len(source.get("provenance") or []) for source in sources)
    metrics["source_locator_coverage"] = round(len(located) / total, 4) if total else (None if reject_expected else 0.0)
    metrics["expected"] = {key: expected[key] for key in ("category", "expected_mode", "expected_source_titles", "expected_terms") if key in expected}
    metrics["checks"] = {"mode_matches": bool(honest), "supported_claims": sum(supported), "claim_count": len(claims), "cited_source_count": len(sources)}
    labels = {"completion": "回答未完成", "retrieval_hit": "预期来源检索不全", "evidence_coverage": "预期事实覆盖不全", "citation_validity": "编号引用无效或缺失", "faithfulness": "摘录不受所引 Wiki 文本支持", "honest_refusal": "拒答或澄清行为与预期不符", "robustness": "输入鲁棒性未达预期"}
    metrics["reasons"] = [labels[name] for name in METRIC_NAMES if metrics[name] is not None and metrics[name] < 1.0]
    metrics["passed"] = not metrics["reasons"]
    return metrics


def _evaluate(config: Config, profile: str, questions: List[Dict[str, Any]], pages: List[Dict[str, Any]], clock: str = FIXED_CLOCK) -> Dict[str, Any]:
    cases = []
    for expected in questions:
        question = str(expected["question"])
        results = weighted_rank(question, pages, top_k=8, profile=profile)
        result = answer_with_metadata(question, results, llm=None, context_config={**config.retrieval, "profile": profile, "top_k": 8})
        cases.append({"question_id": str(expected["id"]), "question": question, "expected_no_evidence": bool(expected.get("expected_no_evidence")),
                      "mode": result["mode"], "answer": result["answer"], "sources": result["sources"], "metrics": _case_metrics(result, expected)})
    metrics: Dict[str, Any] = {}
    counts = {}
    for name in (*METRIC_NAMES, "source_locator_coverage"):
        values = [case["metrics"][name] for case in cases if case["metrics"][name] is not None]
        metrics[name] = round(sum(values) / len(values), 4) if values else None
        counts[name] = len(values)
    values = [metrics[name] for name in METRIC_NAMES if metrics[name] is not None]
    metrics["score"] = round(100 * sum(values) / len(values), 2) if values else 0.0
    metrics.update({"rejection_accuracy": metrics["honest_refusal"], "grounded_answer": metrics["faithfulness"],
                    "corpus_fingerprint": _digest(pages), "question_set_hash": _digest(questions), "clock": clock, "rules_version": RULES_VERSION,
                    "metric_applicable_counts": counts, "corpus_pages": len(pages), "model_calls": 0,
                    "scope": "frozen_local_wiki", "score_note": "固定题库与确定性规则的回放分数；faithfulness 只检查摘录与所引 Wiki 文本一致，不证明原论文结论真实。"})
    return {"profile": profile, "metrics": metrics, "question_count": len(cases), "cases": cases}


def _persist(config: Config, result: Dict[str, Any], pages: List[Dict[str, Any]], questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    artifact = {"pages": pages, "questions": questions, "clock": result["metrics"]["clock"], "rules_version": RULES_VERSION,
                "retrieval": {key: config.retrieval[key] for key in ("top_k", "max_context_chars", "response_reserve_chars", "max_page_chars") if key in config.retrieval}}
    artifact_hash = _digest(artifact)
    path = config.root / "data" / "evaluation" / "snapshots" / (artifact_hash + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish complete files atomically; concurrent writers publish identical bytes.
    if path.exists():
        if _digest(json.loads(path.read_text(encoding="utf-8"))) != artifact_hash:
            raise ValueError("Existing evaluation snapshot failed its content hash")
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".snapshot-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(artifact, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    result["metrics"].update({"snapshot_ref": str(path.relative_to(config.root)), "snapshot_hash": artifact_hash})
    database = Database(config.db_path)
    try:
        return database.create_evaluation_run(result["profile"], result["metrics"], result["cases"])
    finally:
        database.close()


def run_evaluation(config: Config, profile: str = "optimized", questions: Optional[List[Dict[str, Any]]] = None, *, persist: bool = True) -> Dict[str, Any]:
    profile = profile.strip().lower()
    if profile not in {"baseline", "optimized"}:
        raise ValueError("profile must be baseline or optimized")
    question_set = copy.deepcopy(questions if questions is not None else load_questions(config))
    if not question_set:
        raise ValueError("Evaluation question set cannot be empty")
    pages = snapshot_corpus(config)
    result = _evaluate(config, profile, question_set, pages)
    return _persist(config, result, pages, question_set) if persist else result


def compare_profiles(config: Config, *, persist: bool = True) -> Dict[str, Any]:
    questions = load_questions(config)
    pages = snapshot_corpus(config)
    baseline = _evaluate(config, "baseline", questions, pages)
    optimized = _evaluate(config, "optimized", questions, pages)
    regressions = []
    for before, after in zip(baseline["cases"], optimized["cases"]):
        for metric in METRIC_NAMES:
            left, right = before["metrics"][metric], after["metrics"][metric]
            if left is not None and (right is None or right < left):
                regressions.append({"question_id": before["question_id"], "metric": metric, "baseline": left, "optimized": right})
    deltas = {name: round(optimized["metrics"][name] - baseline["metrics"][name], 4) if baseline["metrics"][name] is not None and optimized["metrics"][name] is not None else None for name in METRIC_NAMES}
    gate = {"passed": not regressions, "regressions": regressions, "metric_deltas": deltas}
    baseline["metrics"]["comparison_gate"] = gate
    optimized["metrics"]["comparison_gate"] = gate
    if persist:
        baseline = _persist(config, baseline, pages, questions)
        optimized = _persist(config, optimized, pages, questions)
    return {"baseline": baseline, "optimized": optimized, "gate": gate, "corpus_fingerprint": _digest(pages), "question_set_hash": _digest(questions)}


def replay_evaluation(config: Config, run_id: int) -> Dict[str, Any]:
    database = Database(config.db_path)
    try:
        old = database.get_evaluation_run(run_id)
    finally:
        database.close()
    if not old or not old["metrics"].get("snapshot_hash"):
        raise ValueError("Evaluation run has no replay snapshot")
    snapshot_hash = str(old["metrics"]["snapshot_hash"])
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash):
        raise ValueError("Invalid evaluation snapshot hash")
    path = config.root / "data" / "evaluation" / "snapshots" / (snapshot_hash + ".json")
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if _digest(artifact) != snapshot_hash or artifact["rules_version"] != RULES_VERSION:
        raise ValueError("Replay snapshot changed or requires another rules version")
    frozen = copy.copy(config)
    frozen.retrieval = artifact["retrieval"]
    result = _evaluate(frozen, old["profile"], artifact["questions"], artifact["pages"], artifact["clock"])
    result["metrics"]["replay_of"] = run_id
    return _persist(frozen, result, artifact["pages"], artifact["questions"])
