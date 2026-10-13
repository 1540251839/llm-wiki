"""No-network contracts for the merged source-first/full-text extractor."""
import copy
import json

import pytest

from researchwiki.extraction.basic import extract_basic
from researchwiki.extraction.paper import analyze_paper, enrich_paper, supplement_paper, document_chunks
from researchwiki.extraction.quality import QualityGateError
from researchwiki.extraction.synthesis import _context_batches
from researchwiki.models import EntityMention, Page, ParsedDocument, StructuredPaper
from researchwiki.verification.claims import verify_paper


class Model:
    enabled = True
    model = "unit-test"
    provider = "test"

    def __init__(self, fail_on=None):
        self.users = []
        self.systems = []
        self.fail_on = fail_on

    def structured(self, system, user):
        self.users.append(user)
        self.systems.append(system)
        if len(self.users) == self.fail_on:
            raise RuntimeError("test interruption")
        return {
            "title": "Model bibliographic guess", "authors": ["Unknown model author"], "year": 2001,
            "abstract": "Model abstract guess", "keywords": ["invented"],
            "abstract_zh": "研究如何保留完整文档的证据。", "research_problem": "如何保留完整证据？",
            "methodology": "完整分块阅读。", "experiment_info": "在具名数据集上测试。",
            "methods": [{"name": "Complete Reader", "name_zh": "完整阅读器", "principle": "块定位。"}],
            "datasets": [{"name": "TestSet", "usage_status": "testing", "statistics": "30 examples"}],
            "findings": ["A complete reader preserves all source characters."],
            "topics": ["Traceable extraction"], "topic_labels_zh": {"Traceable extraction": "可溯源抽取"},
            "evidence": [{"field": "findings.0", "quote": "A complete reader preserves all source characters."}],
            "claims": [{"statement": "A complete reader preserves all source characters.", "claim_type": "finding",
                        "evidence": {"quote": "A complete reader preserves all source characters."}}],
        }

    def generate(self, system, user, temperature=0.1):
        self.users.append(user)
        return "Notes for the whole provided chunk."


def document():
    text = "Title: Original Research Title\n\nAbstract\nA complete reader preserves all source characters.\n\n"
    text += "2 Method\n\n" + "Method source detail. " * 1900
    text += "\n\n3 Experiments\n\n" + "Experiment source detail. " * 1900
    text += "\n\n4 Limitations\n\n" + "Limitation source detail. " * 700
    return ParsedDocument(text, [Page(1, text)])


def baseline():
    return StructuredPaper(title="Original Research Title", authors=["Original Author"], year=2025,
                           abstract="Original source abstract.", keywords=["source keyword"],
                           extraction_mode="basic", source_hash="a" * 64)


def test_union_model_serialization_preserves_both_contracts():
    paper = baseline()
    paper.methods = [EntityMention("Reader", "description", "role", "principle", "statistics", "阅读器", "testing")]
    paper.claims = [{"statement": "The reader preserves source facts."}]
    paper.extraction_meta = {"version": "unit"}
    paper.quality = {"allowed": True}
    assert StructuredPaper.from_dict(paper.to_dict()) == paper


def test_enrich_full_text_keeps_bibliographic_fields_and_resume(tmp_path):
    source, before = document(), baseline()
    original = copy.deepcopy(before)
    model = Model(fail_on=3)
    with pytest.raises(RuntimeError, match="Deep paper extraction failed"):
        enrich_paper(source, before, model, checkpoint_dir=tmp_path)
    assert before == original
    model.fail_on, model.users = None, []
    result = enrich_paper(source, before, model, checkpoint_dir=tmp_path)
    assert len(model.users) == len(document_chunks(source)) - 2
    assert result.extraction_meta["cached_chunk_count"] == 2
    assert result.extraction_meta["covered_characters"] == len(source.text)
    assert result.title == original.title and result.authors == original.authors and result.year == original.year
    assert result.abstract == original.abstract and result.keywords == original.keywords
    assert result.abstract_zh and result.methods[0].principle and result.datasets[0].statistics
    assert result.methods[0].name_zh == "完整阅读器"
    assert result.field_evidence[0]["status"] == "quote_matched"
    assert result.claims and result.extraction_mode == "deep"
    assert before == original


def test_focused_sections_complete_and_unrelated_fields_are_immutable(tmp_path):
    source, previous = document(), baseline()
    previous.methods = [EntityMention("Original Method", principle="Original mechanism")]
    previous.contributions = ["An unrelated contribution."]
    previous.research_problem = "Existing problem"
    previous.claims = [{"statement": "Original method uses source evidence.", "claim_type": "method"}]
    saved = copy.deepcopy(previous)
    model = Model()
    result = supplement_paper(source, previous, model, "experiment", checkpoint_dir=tmp_path)
    assert previous == saved
    assert result.methods == previous.methods and result.contributions == previous.contributions
    assert result.abstract_zh == previous.abstract_zh and result.research_problem == previous.research_problem
    assert result.title == previous.title and result.authors == previous.authors
    assert result.datasets[0].usage_status == "testing" and result.experiment_info
    assert result.claims[0] == previous.claims[0]
    metadata = result.extraction_meta["last_supplement"]
    assert metadata["scope"] == "reliable_sections"
    spans = [span for chunk in metadata["chunks"] for span in chunk["segments"]]
    original_experiments = [block for block in source.blocks if "Experiment" in block.section]
    assert original_experiments
    assert sum(s["char_end"] - s["char_start"] for s in spans if "Experiment" in s["section"]) == sum(len(b.text) for b in original_experiments)
    assert all("Method source detail." not in user and "Limitation source detail." not in user for user in model.users)
    recovered = Model()
    supplement_paper(source, previous, recovered, "experiment", checkpoint_dir=tmp_path)
    assert recovered.users == []


def test_focus_without_reliable_sections_falls_back_to_complete_document():
    source = ParsedDocument("Original Study\n" + "Unsectioned source material. " * 1800)
    result = supplement_paper(source, baseline(), Model(), "experiment")
    meta = result.extraction_meta["last_supplement"]
    assert meta["scope"] == "full_document" and meta["coverage"] == 1


def test_language_changes_analysis_and_enrich_checkpoint_identity(tmp_path):
    source = ParsedDocument("Title: Original Research Title\nA complete reader preserves all source characters.")
    first = Model()
    analyze_paper(source, first, language="zh-CN", checkpoint_dir=tmp_path)
    enrich_paper(source, baseline(), first, language="zh-CN", checkpoint_dir=tmp_path)
    cached = Model()
    analyze_paper(source, cached, language="zh-CN", checkpoint_dir=tmp_path)
    enrich_paper(source, baseline(), cached, language="zh-CN", checkpoint_dir=tmp_path)
    assert cached.users == []
    english = Model()
    analyze_paper(source, english, language="en", checkpoint_dir=tmp_path)
    enrich_paper(source, baseline(), english, language="en", checkpoint_dir=tmp_path)
    assert len(english.users) == 2


def test_chunk_field_indices_rebind_to_merged_array_without_evidence_swap():
    text = "Title: Complete Evidence Paper\n" + ("Source padding. " * 100)
    first_quote, second_quote = "The first reader uses original text.", "The second reader preserves original text."
    text += first_quote + "\n" + ("More source padding. " * 100) + second_quote
    source = ParsedDocument(text, [Page(1, text)])

    class IndexedModel(Model):
        def structured(self, _system, user):
            if first_quote in user:
                statement = first_quote
            elif second_quote in user:
                statement = second_quote
            else:
                return {"methodology": "本文依次处理正文。"}
            return {"findings": [statement], "evidence": [{"field": "findings.0", "quote": statement}]}

    paper = enrich_paper(source, baseline(), IndexedModel(), options={"max_chunk_chars": 1000})
    assert paper.findings == [first_quote, second_quote]
    assert {(row["field"], row["quote"]) for row in paper.field_evidence} == {
        ("findings.0", first_quote), ("findings.1", second_quote)}


def test_basic_and_matched_quote_never_implicitly_become_supported():
    source = ParsedDocument("Title: Original Research Title\n\nAbstract\nA complete reader preserves all source characters.",
                            [Page(1, "Title: Original Research Title\n\nAbstract\nA complete reader preserves all source characters.")])
    paper = extract_basic(source)
    assert verify_paper(paper, source, "a" * 64)["claims"] == []
    paper.extraction_mode = "deep"
    paper.findings = ["模型解释：阅读器保持所有字符。"]
    paper.abstract = None
    paper.field_evidence = [{"field": "findings.0", "status": "quote_matched",
                             "quote": "A complete reader preserves all source characters."}]
    bundle = verify_paper(paper, source, "a" * 64)
    assert bundle["claims"][0]["verification"]["status"] == "pending_review"


def test_basic_unknown_and_scan_metadata_are_explicitly_missing():
    paper = extract_basic(ParsedDocument("", [], quality={"allowed": False, "issues": [
        {"code": "ocr_required", "severity": "error", "message": "请先OCR"}]}))
    assert paper.title == "Untitled Paper" and paper.extraction_mode == "basic"
    assert not paper.claims and not paper.authors and paper.abstract is None
    assert paper.quality["allowed"] is False
    assert {row["code"] for row in paper.quality["issues"]} >= {"ocr_required", "uncertain_title", "missing_metadata"}


def test_bad_text_blocks_deep_before_any_model_request():
    source, model = ParsedDocument("\ufffd" * 100), Model()
    with pytest.raises(RuntimeError, match="quality gate"):
        enrich_paper(source, baseline(), model)
    assert not model.users


def test_synthesis_batches_retain_every_character_and_list_item():
    original = [{"source_id": "a", "title": "Paper A", "methodology": "全文中段事实。" * 4000,
                 "findings": [str(i) + " finding" for i in range(150)]},
                {"source_id": "b", "title": "Paper B", "methodology": "small", "findings": []}]
    batches = _context_batches(original, 2000)
    assert len(batches) > 1
    assert all(len(json.dumps(batch, ensure_ascii=False)) <= 2000 for batch in batches)
    fragments = [item for batch in batches for item in batch if item["source_id"] == "a"]
    assert fragments[0]["fragment_start"] == 0
    assert all(left["fragment_end"] == right["fragment_start"] for left, right in zip(fragments, fragments[1:]))
    payload = json.loads("".join(item["structured_fragment"] for item in fragments))
    assert payload["methodology"] == original[0]["methodology"] and payload["findings"] == original[0]["findings"]
    assert next(item for batch in batches for item in batch if item["source_id"] == "b") == original[1]


def test_synthesis_failure_resumes_only_completed_batches(tmp_path, monkeypatch):
    from researchwiki.config import load_config
    import researchwiki.extraction.synthesis as synthesis

    config = load_config(tmp_path)
    config.ingestion["max_chunk_chars"] = 2000
    sources = ["a" * 64, "b" * 64]
    folder = tmp_path / "data" / "extractions"
    folder.mkdir(parents=True)
    for source_id in sources:
        paper = StructuredPaper(title="Complete Comparison " + source_id[0], source_hash=source_id,
                                topics=["Complete Topic"], extraction_mode="deep",
                                methodology="All structured facts must be read. " * 200)
        (folder / (source_id + ".json")).write_text(json.dumps(paper.to_dict()))

    class Database:
        def __init__(self, _path):
            pass

        def list_papers(self, **_kwargs):
            return {"items": [{"status": "INDEXED", "id": source_id} for source_id in sources]}

        def close(self):
            pass

    class SynthesisModel(Model):
        def structured(self, system, user):
            self.users.append(user)
            if len(self.users) == self.fail_on:
                raise RuntimeError("Interrupted synthesis")
            batch = json.loads(user.split("\n\n", 1)[1])
            ids = list(dict.fromkeys(item["source_id"] for item in batch))
            return {"claims": [{"text": "A source-bounded unverified comparison.", "source_ids": ids}], "open_questions": []}

    monkeypatch.setattr(synthesis, "Database", Database)
    interrupted = SynthesisModel(fail_on=2)
    with pytest.raises(RuntimeError, match="Interrupted synthesis"):
        synthesis.synthesize_topic(config, "Complete Topic", interrupted)
    resumed = SynthesisModel()
    result = synthesis.synthesize_topic(config, "Complete Topic", resumed)
    assert result["cached_batch_count"] == 1
    assert len(resumed.users) == result["batch_count"] - 1
    assert result["source_ids"] == sources and result["review_status"] == "needs_review"
    assert result["comparison_scope"] == "within_batches"
    cached = SynthesisModel()
    synthesis.synthesize_topic(config, "Complete Topic", cached)
    assert cached.users == []
