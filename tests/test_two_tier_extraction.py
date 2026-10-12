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
