import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from researchwiki.app import initialize
from researchwiki.compilation import CompilationService
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.models import EntityMention, StructuredPaper
from researchwiki.wiki.entities import EntityRegistry
from researchwiki.wiki.manager import NOTE_START, WikiManager
from researchwiki.qa.retrieval import factual_text


def source(config, text, **fields):
    digest = hashlib.sha256(text.encode()).hexdigest()
    raw = config.root / "raw" / "papers" / (digest + ".txt")
    raw.write_text(text, encoding="utf-8")
    paper = StructuredPaper(source_hash=digest, source_path=str(raw), **fields)
    extraction = config.root / "data" / "extractions" / (digest + ".json")
    extraction.parent.mkdir(parents=True, exist_ok=True)
    extraction.write_text(json.dumps(paper.to_dict(), ensure_ascii=False), encoding="utf-8")
    db = Database(config.db_path)
    try:
        db.register_source(digest, str(raw), str(raw), "text/plain", raw.stat().st_size)
        db.upsert_paper(digest, paper.title, [], None, str(raw), str(raw), "EXTRACTED")
    finally:
        db.close()
    return paper, raw


def test_entity_pages_do_not_inherit_unrelated_paper_findings(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    method_fact = "Evidence Fusion combines independently estimated belief masses."
    dataset_fact = "Legal Corpus contains 120 annotated queries."
    unrelated = "The separate retrieval baseline improves ranking by 9 percent."
    paper, raw = source(config, "Fusion Study\n\n" + "\n\n".join([method_fact, dataset_fact, unrelated]),
        title="Fusion Study", methods=[EntityMention("Evidence Fusion")],
        datasets=[EntityMention("Legal Corpus")], findings=[unrelated],
        claims=[{"statement": method_fact, "claim_type": "method_usage", "entities": [{"type": "method", "name": "Evidence Fusion"}]},
                {"statement": dataset_fact, "claim_type": "dataset", "entities": [{"type": "dataset", "name": "Legal Corpus"}]}])
    service = CompilationService(config)
    preview = service.inventory()
    assert preview["supported_claims"] == 3
    assert not (tmp_path / "data" / "claims" / (paper.source_hash + ".json")).exists()
    result = service.run(source_id=paper.source_hash)
    assert result["status"] == "succeeded", result
    manifest = service.manifest()
    method = next(page for page in manifest["pages"] if page["type"] == "method")
    dataset = next(page for page in manifest["pages"] if page["type"] == "dataset")
    assert method_fact in (tmp_path / method["path"]).read_text()
    assert unrelated not in (tmp_path / method["path"]).read_text()
    assert dataset_fact not in (tmp_path / method["path"]).read_text()
    assert unrelated not in (tmp_path / dataset["path"]).read_text()
    assert unrelated in (tmp_path / result["paper_path"]).read_text()
    paper_page = next(page for page in manifest["pages"] if page["type"] == "paper")
    assert len(paper_page["evidence"]) == 3  # Three spans, even for the same source.
    assert len({span["claim_id"] for span in paper_page["evidence"]}) == 3
    for span in paper_page["evidence"]:
        assert raw.read_text()[span["char_start"]:span["char_end"]] == span["quote"]
        assert span["confidence"] == "verified_quote"
    replay = service.run()
    assert replay["status"] == "succeeded"
    assert replay["content_digest"] == result["content_digest"]
    assert replay["changes"]["updated"] == []


def test_unchecked_summaries_and_forged_cached_status_never_become_facts(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    paper, raw = source(config, "Reliable Study\n\nThe evaluation reports accuracy of 71 percent.",
        title="Reliable Study", abstract="We solve every retrieval problem.",
        findings=["The evaluation reports accuracy of 99 percent."],
        methods=[EntityMention("Unobserved Method", "It dominates every baseline.")])
    claims_path = tmp_path / "data" / "claims" / (paper.source_hash + ".json")
    claims_path.parent.mkdir(parents=True)
    claims_path.write_text(json.dumps({"claims": [{"claim_id": "forged", "statement": paper.findings[0],
        "verification": {"status": "supported"}, "evidence": {"quote": raw.read_text(), "source_id": paper.source_hash,
        "page_number": 1, "char_start": 0, "char_end": len(raw.read_text())}}]}))
    service = CompilationService(config)
    result = service.run()
    assert result["status"] == "succeeded", result
    assert result["supported_claims"] == 0
    assert result["page_types"] == {"paper": 1, "method": 1}  # Unreviewed method is navigation only.
    body = (tmp_path / next(iter(result["paper_paths"].values()))).read_text()
    assert paper.abstract not in factual_text(body)
    assert paper.findings[0] not in factual_text(body)
    assert paper.abstract in body and paper.findings[0] in body  # Clearly labeled reading pane.
    assert "It dominates every baseline." not in body
    assert "no_supported_claims" in body
    assert any(item["code"] == "claim_quarantined" for item in result["diagnostics"])


def test_quality_failure_preserves_but_quarantines_legacy_pages(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    bad, raw = source(config, "ScienceDirect\n\nOnly a journal header was extracted.",
        title="ScienceDirect", topics=["Evidence"])
    wiki = WikiManager(config.wiki_root)
    old_path, _ = wiki.ingest(bad, raw.relative_to(tmp_path).as_posix())
    old_path.write_text(old_path.read_text().replace(NOTE_START + "\n\n", NOTE_START + "\n保留我的笔记\n"))
    before = old_path.read_bytes()
    result = CompilationService(config).run()
    assert result["status"] == "succeeded", result
    assert result["ready_sources"] == 0
    assert old_path.read_bytes() == before
    assert old_path.relative_to(tmp_path).as_posix() in result["excluded_paths"]
    assert any(item["code"] == "source_quality" for item in result["diagnostics"])
    assert CompilationService(config).manifest()["pages"] == []


def test_claim_cache_and_pages_share_rollback_boundary(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    fact = "Evidence Fusion combines independent mass functions."
    paper, _ = source(config, "Atomic Study\n\n" + fact, title="Atomic Study", findings=[fact])
    original = WikiManager._atomic_write

    def fail_manifest(path, content):
        if Path(path).name == "manifest.json":
            raise OSError("simulated manifest failure")
        return original(path, content)

    with patch.object(WikiManager, "_atomic_write", side_effect=fail_manifest):
        result = CompilationService(config).run()
    assert result["status"] == "failed"
    assert not list(config.wiki_root.rglob("*.md"))
    assert not (tmp_path / "data" / "claims" / (paper.source_hash + ".json")).exists()


def test_aliases_are_explicit_and_type_scoped():
    registry = EntityRegistry()
    first = registry.resolve("method", "D-S证据理论")
    second = registry.resolve("method", "Dempster-Shafer theory")
    assert first["canonical_id"] == second["canonical_id"]
    assert registry.resolve("method", "Dempster-Shafer neural model")["canonical_id"] != first["canonical_id"]
    assert registry.resolve("topic", "D-S证据理论")["canonical_id"] != first["canonical_id"]
    assert registry.resolve("dataset", "LeCaRD")["canonical_id"] != registry.resolve("dataset", "LeCaRDv2")["canonical_id"]


def test_empty_active_corpus_excludes_retained_generated_pages(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    fact = "Evidence Fusion combines independently estimated belief masses."
    paper, _ = source(config, "Deleted Study\n\n" + fact, title="Deleted Study", methods=[EntityMention("Evidence Fusion")],
        claims=[{"statement": fact, "claim_type": "method_usage", "entities": [{"type": "method", "name": "Evidence Fusion"}]}])
    service = CompilationService(config)
    assert service.run()["status"] == "succeeded"
    old_pages = {page["path"] for page in service.manifest()["pages"]}
    db = Database(config.db_path)
    try:
        db.mark_source_deleted(paper.source_hash)
        report = service.run()
        assert report["status"] == "succeeded"
        assert report["page_count"] == 0
        assert set(report["excluded_paths"]) == old_pages
        assert all((tmp_path / path).is_file() for path in old_pages)
        assert db.rebuild(WikiManager(config.wiki_root))["pages"] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 0
    finally:
        db.close()
