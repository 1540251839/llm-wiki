"""Source-first lightweight imports and original-page search stay independent of LLM."""
import hashlib

from fastapi.testclient import TestClient

from researchwiki.app import ingest, rebuild, delete_source
from researchwiki.config import load_config
from researchwiki.extraction.context import extraction_context
from researchwiki.index.database import Database
from researchwiki.index.source_text import source_chunks
from researchwiki.models import Page, ParsedDocument
from researchwiki.web import create_app


def test_source_chunks_keep_exact_page_offsets_and_all_content():
    pages = [
        Page(1, "Intro paragraph. " * 120 + "\n\nMethods use ZYX-710." * 30),
        Page(2, "实验部分记录可复核的来源数据。" * 90),
    ]
    records = list(source_chunks(pages, max_chars=500))
    assert len(records) >= 4
    assert {r["page_number"] for r in records} == {1, 2}
    for r in records:
        page = pages[r["page_number"] - 1]
        assert page.text[r["char_start"]:r["char_end"]] == r["content"]
        assert len(r["content"]) <= 500
    assert "ZYX-710" in " ".join(r["content"] for r in records)


def test_quick_ingest_indexes_original_text_without_model(tmp_path):
    config = load_config(tmp_path)
    original = tmp_path / "research.txt"
    text = (
        "Research Evidence Retrieval Under Weak Supervision\n"
        "Abstract: This article studies evidence provenance in research papers. "
        "It introduces a baseline metadata approach for classification and retrieval.\n"
        "Keywords: traceability, evidence\n"
        "1. Introduction\n"
        "A special body-only term UniqueRawSource910 appears in this section.\n"
        "2. Methods\n" + ("The model uses a reproducible retrieval experiment.\n" * 120) +
        "ExperimentalSectionZXQ9 is a detail in the middle of the original paper.\n"
    )
    original.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    assert ingest(config, original, quality_mode="basic")["status"] == "indexed"
    with TestClient(create_app(tmp_path)) as client:
        doc = client.get(f"/api/documents/{digest}").json()
        assert doc["original_text_indexed"]
        assert doc["original_text_chunks"] >= 2
        found = client.get("/api/source-search", params={"q": "ExperimentalSectionZXQ9"}).json()
        assert found["source_kind"] == "original_text"
        assert found["items"], found
        row = found["items"][0]
        assert row["source_id"] == digest
        assert row["page_number"] == 1
        assert row["file_url"].startswith(f"/api/documents/{digest}/file")
        assert text[row["char_start"]:row["char_end"]] == row["excerpt"]
        assert client.get("/api/source-search", params={"q": "a" * 501}).status_code == 422
        assert "source-search-form" in client.get("/").text

    db = Database(config.db_path)
    try:
        assert db.search_source_chunks("UniqueRawSource910")
        first = db.count_source_chunks(digest)
    finally:
        db.close()
    assert ingest(config, original, quality_mode="basic")["status"] == "already_exists"
    db = Database(config.db_path)
    try:
        assert db.count_source_chunks(digest) == first
        db.delete_source_chunks(digest)
        assert db.count_source_chunks(digest) == 0
    finally:
        db.close()
    # One-time, model-free backfill of documents imported by older versions.
    assert rebuild(config)["original_sources_backfilled"] == 1
    db = Database(config.db_path)
    try:
        assert db.search_source_chunks("ExperimentalSectionZXQ9")
    finally:
        db.close()


def test_source_search_hides_deleted_documents(tmp_path):
    config = load_config(tmp_path)
    original = tmp_path / "test.txt"
    original.write_text("An Interesting Research Paper\nAbstract: We describe scientific source indexing in detail.\nUniqueDeleteEvidenceMarker123")
    ingest(config, original, quality_mode="basic")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    assert delete_source(config, digest, delete_raw=False)["status"] == "deleted"
    db = Database(config.db_path)
    try:
        assert db.search_source_chunks("UniqueDeleteEvidenceMarker123") == []
        assert db.count_source_chunks(digest) == 0
    finally:
        db.close()


def test_short_paper_deep_context_is_full_text_not_section_truncated():
    intro = "We review prior work in robust extraction.\n" * 420
    body = "Late unique methods and data details belong in the full text."
    full = "A Study on Research Wiki\n" + intro + body
    document = ParsedDocument(text=full, pages=[Page(1, full)])
    context = extraction_context(document)
    assert body in context
    assert intro[:60] in context
    assert len(context) >= len(full)
