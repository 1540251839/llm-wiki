"""Typed research graph and focused deep update tests."""
import hashlib
import json

from fastapi.testclient import TestClient

from researchwiki.app import ingest
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.graph.research import project_research_graph
from researchwiki.models import ParsedDocument, Page, StructuredPaper, EntityMention
from researchwiki.extraction.paper import supplement_paper
from researchwiki.web import create_app


class FakeModel:
    enabled = True
    def structured(self, system, user):
        if "FOCUSED UPDATE" in system:
            return {
                "experiment_info": "在 LeCaRD 上进行测试。",
                "metrics": ["NDCG@10"],
                "datasets": [{"name": "LeCaRD", "usage_status": "testing", "role": "用于测试"}],
                "evidence": [{"field": "datasets:LeCaRD",
                              "quote": "We evaluate the method on LeCaRD for case retrieval."}],
            }
        return {
            "title": "An Evidence Based Study",
            "methods": [{"name": "Evidence Selector", "description": "选择可靠的证据。"}],
            "datasets": [{"name": "LeCaRD", "role": "文中提及"}],
            "topics": ["Legal case retrieval"],
            "research_problem": "如何在噪声下可靠地检索类案？",
        }


def test_focused_extraction_preserves_unrelated_fields_and_adds_verified_span():
    text = ("An Evidence Based Study\nAbstract: A robust method for noisy case retrieval.\n"
            "3 Method\nWe propose Evidence Selector as a robust solution.\n"
            "4 Experiments\nWe evaluate the method on LeCaRD for case retrieval.")
    document = ParsedDocument(text=text, pages=[Page(1,text)])
    previous = StructuredPaper(
        title="An Evidence Based Study", abstract="The original abstract.",
        research_problem="既有研究问题。",
        methods=[EntityMention("Evidence Selector","一种可靠方法。")],
        topics=["Legal case retrieval"], source_hash="ab",
    )
    result = supplement_paper(document, previous, FakeModel(), "experiment")
    assert result.research_problem == previous.research_problem
    assert result.abstract == previous.abstract
    assert result.methods[0].name == previous.methods[0].name
    assert result.datasets[0].usage_status == "testing"
    assert result.field_evidence[0]["status"] == "quote_matched"
    assert result.field_evidence[0]["page"] == 1
    assert not previous.datasets


def test_focused_ingest_and_four_type_graph(tmp_path):
    config = load_config(tmp_path)
    text = ("An Evidence Based Study\nAbstract: We develop an evidence selection method "
            "for legal case retrieval with noisy labels.\nKeywords: evidence, retrieval\n"
            "3 Method\nWe propose Evidence Selector as a robust solution.\n"
            "4 Experiments\nWe evaluate the method on LeCaRD for case retrieval.")
    source = tmp_path / "paper.txt"
    source.write_text(text,encoding="utf-8")
    assert ingest(config, source, quality_mode="standard", llm_client=FakeModel())["status"] == "indexed"
    digest = hashlib.sha256(text.encode()).hexdigest()
    cache = tmp_path/"data"/"extractions"/(digest+".json")
    before = json.loads(cache.read_text())
    assert before["methods"][0]["name"] == "Evidence Selector"
    result = ingest(config, source, quality_mode="standard", llm_client=FakeModel(),
                    resume=True, focus="experiment")
    assert result["status"] == "indexed"
    after = json.loads(cache.read_text())
    assert after["methods"] == before["methods"]
    assert after["research_problem"] == before["research_problem"]
    assert after["datasets"][0]["usage_status"] == "testing"

    db = Database(config.db_path)
    try:
        graph = project_research_graph(db, tmp_path, center="__all__", limit=100)
        assert {"PAPER", "METHOD", "DATASET", "TOPIC"}.issubset({n["type"] for n in graph["nodes"]})
        assert not ({"SOURCE", "PAGE_TYPE"} & {n["type"] for n in graph["nodes"]})
        assert any(e["semantic"] in {"TESTED_ON","MENTIONS_DATASET"} for e in graph["edges"])
        paper_node = next(n for n in graph["nodes"] if n["type"] == "PAPER")
        local = project_research_graph(db, tmp_path, center=paper_node["id"], depth=1)
        assert local["center"] == paper_node["id"]
        assert len(local["nodes"]) <= len(graph["nodes"])
    finally:
        db.close()
    with TestClient(create_app(tmp_path)) as client:
        response=client.get("/api/graph/research")
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "research"
        assert "固定题库与评估" not in client.get("/").text
