import hashlib
from researchwiki.extraction.basic import extract_basic
from researchwiki.models import ParsedDocument, Page
from researchwiki.app import ingest, initialize
from researchwiki.config import load_config
from researchwiki.index.database import Database


def test_offline_extract_fields_and_known_dataset():
    sample = ("A Reliable System for Case Retrieval\n"
              "Abstract: This paper presents a new approach to legal case retrieval. "
              "We evaluate performance on LeCaRD using ranking metrics and discuss errors.\n"
              "Keywords: legal retrieval, uncertainty, evidence\n"
              "1. Introduction\n"
              "LeCaRD is a reference dataset for this study.")
    paper = extract_basic(ParsedDocument(text=sample, pages=[Page(1, sample)]))
    assert paper.title == "A Reliable System for Case Retrieval"
    assert paper.abstract and paper.abstract.startswith("This paper")
    assert paper.keywords == ["legal retrieval", "uncertainty", "evidence"]
    assert paper.datasets[0].name == "LeCaRD"
    assert not paper.methods and not paper.topics


def test_basic_ingest_needs_no_model_key(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    source = tmp_path / "study.txt"
    source.write_text("A Reliable System for Case Retrieval\nAbstract: A paper about legal case retrieval and uncertainty. It evaluates retrieval quality on LeCaRD.\nKeywords: retrieval, uncertainty\n1. Introduction\nLeCaRD is discussed.", encoding="utf-8")
    result = ingest(config, source, quality_mode="basic")
    assert result["status"] == "indexed"
    cache = tmp_path / "data" / "extractions" / (hashlib.sha256(source.read_bytes()).hexdigest() + ".json")
    assert cache.exists()
    db = Database(config.db_path)
    try:
        job_pages = db.list_wiki_pages(page_type="paper")
        assert job_pages
    finally:
        db.close()



def test_deep_failure_preserves_previously_indexed_basic_wiki(tmp_path):
    import json
    import pytest

    class BrokenModel:
        enabled = True

        def structured(self, *_args, **_kwargs):
            raise RuntimeError("model unavailable")

    config = load_config(tmp_path)
    source = tmp_path / "study.txt"
    source.write_text(
        "A Reliable Method for Legal Retrieval\n"
        "Abstract: This paper discusses legal retrieval and uncertain evidence "
        "over multiple cases and explicitly mentions LeCaRD.\n"
        "Keywords: retrieval, evidence\n"
        "1. Introduction\n"
        "We mention LeCaRD and discuss relevance.",
        encoding="utf-8",
    )
    result = ingest(config, source, quality_mode="basic")
    original_wiki = __import__("pathlib").Path(result["paper_path"]).read_bytes()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = tmp_path / "data" / "extractions" / (digest + ".json")
    original_cache = cache.read_bytes()

    with pytest.raises(RuntimeError, match="Deep paper extraction failed"):
        ingest(config, source, quality_mode="standard", llm_client=BrokenModel(), resume=True)
    assert cache.read_bytes() == original_cache
    assert __import__("pathlib").Path(result["paper_path"]).read_bytes() == original_wiki
    db = Database(config.db_path)
    try:
        assert db.get_paper(digest)["status"] == "INDEXED"
    finally:
        db.close()
    assert json.loads(cache.read_text())["extraction_mode"] == "basic"


def test_opt_in_cross_paper_summary_is_cited_and_cached(tmp_path):
    import json
    from researchwiki.compilation import CompilationService
    from researchwiki.extraction.synthesis import synthesize_topic

    class StubModel:
        enabled = True

        def structured(self, system, user):
            if "source_ids" in system:
                context = json.loads(user.split("\n\n", 1)[1])
                return {
                    "claims": [{"text": "Both papers investigate retrieval robustness.",
                                "source_ids": [p["source_id"] for p in context]}],
                    "open_questions": [],
                }
            return {
                "methods": [{"name": "Evidence Robustness", "description": "Studies relevance errors."}],
                "datasets": [{"name": "LeCaRD", "role": "Reported evaluation"}],
                "topics": ["Retrieval Robustness"],
                "research_problem": "How to remain robust to noise?",
                "experiment_info": "LeCaRD was used for retrieval evaluation.",
            }

    config = load_config(tmp_path)
    for i in (1, 2):
        source = tmp_path / ("paper" + str(i) + ".txt")
        source.write_text(
            f"A Study of Retrieval Robustness {i}\n"
            "Abstract: This paper investigates legal retrieval robustness "
            "through new evidence modeling on noisy data.\n"
            "Keywords: legal retrieval, noise\n"
            "1. Introduction\n"
            "This work uses the LeCaRD dataset.",
            encoding="utf-8",
        )
        assert ingest(config, source, quality_mode="standard", llm_client=StubModel())["status"] == "indexed"
    summary = synthesize_topic(config, "Retrieval Robustness", llm=StubModel())
    assert summary["review_status"] == "needs_review"
    assert len(summary["source_ids"]) == 2
    result = CompilationService(config).run()
    assert result["status"] == "succeeded", result
    topic_page = next(page for page in CompilationService(config).manifest()["pages"]
                      if page["type"] == "topic" and page["title"] == "Retrieval Robustness")
    text = (tmp_path / topic_page["path"]).read_text()
    assert "## 深度跨论文综合（待人工复核）" in text
    assert "Both papers investigate retrieval robustness." in text
    assert all(source_id in text for source_id in summary["source_ids"])
    from researchwiki.qa.retrieval import factual_text
    assert "Both papers investigate retrieval robustness." not in factual_text(text)
