import hashlib
import json

import pytest

from researchwiki.app import initialize
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.models import EntityMention, Page, ParsedDocument, StructuredPaper
from researchwiki.parsing.pdf import parse_file
from researchwiki.verification import ClaimsService, verify_paper


SOURCE_TEXT = "Traceable Paper\n\nELCR reaches 92.3% accuracy on LeCaRD.\n\nAblation evaluation uses five runs.\n"


def make_paper(statement="ELCR reaches 92.3% accuracy on LeCaRD.", quote=None):
    return StructuredPaper(
        title="Traceable Paper", methods=[EntityMention("ELCR")], datasets=[EntityMention("LeCaRD")],
        topics=["Dempster-Shafer theory"],
        claims=[{"statement": statement, "claim_type": "experiment_result", "subject": "ELCR",
                 "entities": [{"type": "method", "name": "ELCR"}, {"type": "dataset", "name": "LeCaRD"},
                              {"type": "topic", "name": "Dempster-Shafer theory"}],
                 "attributes": {"metric": "accuracy", "value": "92.3%"},
                 "evidence": {"quote": quote if quote is not None else "ELCR reaches 92.3% accuracy on LeCaRD.",
                              "page": 777, "char_start": -500, "char_end": -1},
                 "verification": {"status": "supported", "confidence": 1.0}}])


def setup_source(tmp_path, paper):
    config = load_config(tmp_path)
    initialize(config)
    source_id = hashlib.sha256(SOURCE_TEXT.encode()).hexdigest()
    raw = config.root / "raw" / "papers" / (source_id + ".txt")
    raw.write_text(SOURCE_TEXT)
    paper.source_hash = source_id
    paper.source_path = str(raw)
    cache = config.root / "data" / "extractions" / (source_id + ".json")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(paper.to_dict()))
    database = Database(config.db_path)
    try:
        database.register_source(source_id, str(raw), str(raw), "text/plain", raw.stat().st_size)
        database.upsert_paper(source_id, paper.title, [], None, str(raw), str(raw), "EXTRACTED")
    finally:
        database.close()
    return config, source_id, raw, cache


def test_verifier_owns_locators_and_rejects_irrelevant_associations():
    paper = make_paper()
    document = ParsedDocument(SOURCE_TEXT, [Page(1, SOURCE_TEXT)])
    result = verify_paper(paper, document, "a" * 64)
    assert result["summary"] == {"supported": 1}
    claim = result["claims"][0]
    evidence = claim["evidence"]
    assert evidence["page"] == evidence["page_number"] == 1
    assert SOURCE_TEXT[evidence["char_start"]:evidence["char_end"]] == evidence["quote"]
    assert evidence["char_start"] >= 0 and evidence["locator"].startswith("page:1#chars=")
    assert claim["verification"]["method"] == "deterministic_extractive"
    assert {(value["type"], value["name"]) for value in claim["entities"]} == {
        ("paper", "Traceable Paper"), ("method", "ELCR"), ("dataset", "LeCaRD")}


def test_focus_preserves_only_unchanged_claim_review(tmp_path):
    import copy
    paper = make_paper("ELCR has strong retrieval performance on LeCaRD.")
    paper.extraction_meta = {"generation_revision": "original", "version": "v1"}
    config, source_id, raw, _cache = setup_source(tmp_path, paper)
    service = ClaimsService(config)
    claim = service.prepare(source_id, paper, parse_file(raw))["claims"][0]
    reviewed = service.review(source_id, claim["claim_id"], "supported", "User checked the exact original passage")
    focused = copy.deepcopy(paper)
    focused.methodology = "Changed method reading only."
    focused.extraction_meta["last_supplement"] = {"focus": "method", "scope": "reliable_sections"}
    result = verify_paper(focused, parse_file(raw), source_id, existing=service.load(source_id))
    preserved = next(row for row in result["claims"] if row["claim_id"] == claim["claim_id"])
    assert preserved["verification"] == reviewed["verification"]
    focused.claims[0]["attributes"] = {"metric": "precision", "value": "92.3%"}
    changed = verify_paper(focused, parse_file(raw), source_id, existing=service.load(source_id))
    modified = next(row for row in changed["claims"] if row["claim_id"] == claim["claim_id"])
    assert modified["verification"]["status"] == "pending_review"
    assert modified["verification"]["method"] != "human_review"


def test_fresh_focus_invalidates_all_old_human_decisions(tmp_path):
    import copy
    paper = make_paper("ELCR has strong retrieval performance on LeCaRD.")
    paper.extraction_meta = {"generation_revision": "original", "version": "v1"}
    config, source_id, raw, _cache = setup_source(tmp_path, paper)
    service = ClaimsService(config)
    claim = service.prepare(source_id, paper, parse_file(raw))["claims"][0]
    service.review(source_id, claim["claim_id"], "supported", "Manually checked")
    refreshed = copy.deepcopy(paper)
    refreshed.extraction_meta.update({"generation_revision": "new", "last_supplement": {"focus": "method"}})
    result = verify_paper(refreshed, parse_file(raw), source_id, existing=service.load(source_id))
    assert result["claims"][0]["verification"]["status"] == "pending_review"
    assert result["claims"][0]["verification"]["method"] != "human_review"


def test_located_paraphrase_requires_review_and_unlocated_quote_is_insufficient():
    document = ParsedDocument(SOURCE_TEXT, [Page(1, SOURCE_TEXT)])
    paraphrase = verify_paper(make_paper("ELCR has strong retrieval performance on LeCaRD."), document, "a" * 64)["claims"][0]
    assert paraphrase["verification"]["status"] == "pending_review"
    assert paraphrase["verification"]["quote_located"]
    unlocated = verify_paper(make_paper("ELCR achieves 99.9% accuracy.", "Unpublished 99.9% result."), document, "a" * 64)["claims"][0]
    assert unlocated["verification"]["status"] == "insufficient"
    assert unlocated["evidence"]["page_number"] is None


def test_human_support_requires_quote_in_hash_bound_raw_source(tmp_path):
    paper = make_paper("ELCR achieves 99.9% accuracy.", "Unpublished 99.9% result.")
    config, source_id, raw, _cache = setup_source(tmp_path, paper)
    service = ClaimsService(config)
    claim = service.prepare(source_id, paper, parse_file(raw))["claims"][0]
    with pytest.raises(ValueError, match="原文"):
        service.review(source_id, claim["claim_id"], "supported", "Checked carefully")
    # A human can explicitly mark the unsupported result as contradicted.
    service.review(source_id, claim["claim_id"], "contradicted", "No such result appears in the paper")
    original = raw.read_text()
    raw.write_text(original + "\nTampered text")
    with pytest.raises(ValueError, match="哈希"):
        service.review(source_id, claim["claim_id"], "supported", "Attempt after tampering",
                       quote="ELCR reaches 92.3% accuracy on LeCaRD.")
    assert service.load(source_id)["claims"][0]["verification"]["status"] == "contradicted"


def test_correction_and_review_survive_prepare_and_preserve_audit(tmp_path):
    paper = make_paper("ELCR achieves excellent performance on LeCaRD.")
    config, source_id, raw, _cache = setup_source(tmp_path, paper)
    service = ClaimsService(config)
    claim = service.prepare(source_id, paper, parse_file(raw))["claims"][0]
    corrected = service.review(source_id, claim["claim_id"], "supported", "Replaced summary with exact reported result",
                               statement="ELCR reaches 92.3% accuracy on LeCaRD.",
                               quote="ELCR reaches 92.3% accuracy on LeCaRD.")
    assert corrected["claim_id"] == claim["claim_id"]
    assert corrected["verification"]["method"] == "human_review"
    assert len(service.load(source_id)["audit"]) == 1
    after = service.prepare(source_id, paper, parse_file(raw))
    assert after["claims"][0]["statement"] == corrected["statement"]
    assert after["claims"][0]["verification"] == corrected["verification"]
    assert after["audit"][0]["before_claim"]["statement"] == paper.claims[0]["statement"]
    service.review(source_id, claim["claim_id"], "pending_review", "A second reviewer requests a wording check")
    repeated = service.prepare(source_id, paper, parse_file(raw))
    assert repeated["claims"][0]["verification"]["status"] == "pending_review"
    assert repeated["claims"][0]["statement"] == corrected["statement"]
    assert len(repeated["audit"]) == 2
    assert raw.read_text() == SOURCE_TEXT


def test_reextraction_invalidates_old_human_decision_and_correction(tmp_path):
    paper = make_paper("ELCR has strong performance on LeCaRD.")
    config, source_id, raw, cache = setup_source(tmp_path, paper)
    service = ClaimsService(config)
    claim = service.prepare(source_id, paper, parse_file(raw))["claims"][0]
    service.review(source_id, claim["claim_id"], "supported", "Source manually checked",
                   statement="ELCR reaches 92.3% accuracy on LeCaRD.")
    # Same statement and stable claim id, but the original extracted attributes
    # change. That is a new extraction input, not a previously approved fact.
    paper.claims[0]["attributes"]["metric"] = "MRR"
    cache.write_text(json.dumps(paper.to_dict()))
    rebuilt = service.prepare(source_id, paper, parse_file(raw))
    assert rebuilt["claims"][0]["statement"] == paper.claims[0]["statement"]
    assert "correction" not in rebuilt["claims"][0]
    assert rebuilt["claims"][0]["verification"]["method"] != "human_review"
    assert rebuilt["claims"][0]["verification"]["status"] == "pending_review"
    assert len(rebuilt["audit"]) == 1


@pytest.mark.parametrize("label", [
    "文本检索模块", "重排序", "文本生成", "自动评估", "人工评估", "知识库",
    "Training robustness", "Training objective", "Dempster-Shafer theory", "Accuracy",
])
def test_bare_entity_roles_are_keyword_hits_not_supported_facts(label):
    paper = StructuredPaper(title="A scientific paper", methods=[EntityMention("Model", role=label)])
    source = "A scientific paper\n\n" + label + "\n"
    result = verify_paper(paper, ParsedDocument(source, [Page(1, source)]), "a" * 64)
    claim = result["claims"][0]
    assert claim["evidence"]["located"]
    assert claim["verification"]["status"] == "pending_review"
    assert not claim["verification"]["proposition"]
    assert "关键词" in claim["verification"]["reason"]


@pytest.mark.parametrize("statement", [
    "Accuracy is70%.", "Accuracy is 70%.", "准确率为90%。", "准确率达到90%。",
    "The model improves retrieval quality.", "This paper studies noisy labels.",
    "Ablation evaluation uses five runs.", "ELCR reaches 92.3% accuracy on LeCaRD.",
    "Accuracy = 70%.", "The method is robust to noise.",
])
def test_complete_extractive_propositions_still_receive_source_support(statement):
    paper = StructuredPaper(title="A scientific paper", findings=[statement])
    source = "A scientific paper\n\n" + statement + "\n"
    claim = verify_paper(paper, ParsedDocument(source, [Page(1, source)]), "a" * 64)["claims"][0]
    assert claim["verification"]["status"] == "supported"
    assert claim["verification"]["proposition"]
    assert claim["verification"]["method"] == "deterministic_extractive"


def test_paper_title_is_not_automatically_a_fact_even_if_grammatical():
    title = "Evidence improves learning"
    paper = StructuredPaper(title=title, findings=[title])
    claim = verify_paper(paper, ParsedDocument(title, [Page(1, title)]), "a" * 64)["claims"][0]
    assert claim["verification"]["status"] == "pending_review"
    assert "标题" in claim["verification"]["reason"]
