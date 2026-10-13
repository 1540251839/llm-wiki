"""Source-first QA, typed object citations and strict publication regression."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from researchwiki.app import ingest
from researchwiki.compilation import CompilationService
from researchwiki.config import load_config
from researchwiki.graph.research import _dataset_role, project_research_graph
from researchwiki.index.database import Database
from researchwiki.qa.context import assemble_context
from researchwiki.qa.engine import answer_events, answer_with_metadata
from researchwiki.qa.engine import _snippets
from researchwiki.qa.retrieval import factual_text, retrieve_hybrid
from researchwiki.qa.retrieval import named_paper_scope
from researchwiki.wiki.manager import NOTE_START, WikiManager


FACTS = ["Evidence Selector uses DemoSet for testing.",
         "DemoSet contains 85 annotated queries.",
         "Evidence Selector improves retrieval accuracy by 3 percent."]


class PaperModel:
    enabled = True

    def structured(self, *_args, **_kwargs):
        return {"title": "A Traceable Evidence Study", "research_problem": "An unreviewed explanation about 99999 queries.",
                "methods": [{"name": "Evidence Selector", "description": "An unreviewed model description."}],
                "datasets": [{"name": "DemoSet", "usage_status": "training"}],
                "topics": ["Retrieval"], "findings": ["The model invents 99999 queries."],
                "claims": [{"statement": sentence, "claim_type": "experiment_result",
                            "entities": [{"type": "method", "name": "Evidence Selector"},
                                         {"type": "dataset", "name": "DemoSet"}],
                            "evidence": {"quote": sentence}} for sentence in FACTS]}


def corpus(tmp_path):
    config = load_config(tmp_path)
    source = tmp_path / "sample.txt"
    source.write_text("A Traceable Evidence Study\nAbstract: A method for reliable retrieval.\nKeywords: reliable retrieval\n1. Introduction\n" + "\n".join(FACTS), encoding="utf-8")
    result = ingest(config, source, quality_mode="standard", llm_client=PaperModel())
    assert result["status"] == "indexed"
    source_id = hashlib.sha256(source.read_bytes()).hexdigest()
    return config, source_id, result


def test_hybrid_objects_are_distinct_and_same_source_citations_are_not_collapsed(tmp_path):
    config, source_id, result = corpus(tmp_path)
    database = Database(config.db_path)
    try:
        rows = retrieve_hybrid(config, database, "Evidence Selector DemoSet queries", top_k=8)
        assert {row["evidence_type"] for row in rows} == {"supported_claim", "raw_excerpt"}
        assert len({row["retrieval_key"] for row in rows}) == len(rows)
        supported = [row for row in rows if row["evidence_type"] == "supported_claim"]
        # Same claims appearing on paper and entity pages are one evidence object.
        assert len(supported) == 3
        assert len({row["claim_id"] for row in supported}) == 3
        assert all(row["sources"][0]["source_id"] == source_id for row in rows)
        answer = answer_with_metadata("Evidence Selector DemoSet queries", rows)
        assert answer["mode"] == "local_evidence"
        assert answer["citations"]["used"] == list(range(1, len(rows) + 1))
        assert len(answer["sources"]) == len(rows)
        assert all(source["provenance"][0]["locator"] for source in answer["sources"])
        assert "99999" not in answer["answer"]
        assert "DemoSet contains 85" in answer["answer"]
        raw_card = next(source for source in answer["sources"] if source["evidence_type"] == "raw_excerpt")
        assert raw_card["verification_status"] == "raw_excerpt"
        assert raw_card["page_id"] is None
        assert all(row["evidence_type"] == "supported_claim" for row in retrieve_hybrid(config, database, "DemoSet queries", evidence_scope="wiki"))
        with pytest.raises(ValueError, match="evidence_scope"):
            retrieve_hybrid(config, database, "DemoSet", evidence_scope="invalid")
    finally:
        database.close()


def test_basic_without_model_can_answer_original_text_and_deep_failure_does_not_hide_it(tmp_path):
    config = load_config(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("An Offline Research Study\nAbstract: DemoSet contains 85 annotated queries for retrieval experiments.\n", encoding="utf-8")
    result = ingest(config, source, quality_mode="basic")
    assert result["status"] == "indexed"
    database = Database(config.db_path)
    try:
        source_id = hashlib.sha256(source.read_bytes()).hexdigest()
        database.update_paper_status(source_id, "FAILED", "deep processing failed")
        rows = retrieve_hybrid(config, database, "DemoSet queries")
        assert rows and all(row["evidence_type"] == "raw_excerpt" for row in rows)
        assert not retrieve_hybrid(config, database, "DemoSet queries", evidence_scope="wiki")
        answer = answer_with_metadata("DemoSet queries", rows)
        assert "85 annotated queries" in answer["answer"]
        assert answer["mode"] == "local_evidence"
    finally:
        database.close()


def test_invalid_hash_or_locator_never_reaches_answer(tmp_path):
    config, source_id, result = corpus(tmp_path)
    database = Database(config.db_path)
    try:
        manifest_path = config.wiki_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for page in manifest["pages"]:
            for claim in page["claims"]:
                claim["evidence"]["char_start"] += 1
        manifest_path.write_text(json.dumps(manifest))
        assert not retrieve_hybrid(config, database, "DemoSet queries", evidence_scope="wiki")
        assert retrieve_hybrid(config, database, "DemoSet queries")  # Valid raw index remains.
        raw = config.root / "raw" / "papers" / (source_id + ".txt")
        raw.write_text(raw.read_text() + "\nTampered original")
        assert not retrieve_hybrid(config, database, "DemoSet queries")
    finally:
        database.close()


def test_context_excludes_pending_reading_and_user_notes_and_has_one_total_budget(tmp_path):
    config, _, result = corpus(tmp_path)
    database = Database(config.db_path)
    try:
        rows = retrieve_hybrid(config, database, "DemoSet queries", top_k=8)
        body = __import__("pathlib").Path(result["paper_path"]).read_text()
        body = body.replace(NOTE_START, NOTE_START + "\nHIDDEN PERSONAL FACT 99999.")
        assert "99999" not in factual_text(body)
        untyped = {"title": "Study", "content": body, "sources": rows[0]["sources"]}
        text, included, stats = assemble_context("DemoSet queries", [untyped, *rows],
                   {"max_context_chars": 8000, "response_reserve_chars": 2000, "max_page_chars": 1400})
        assert text and "99999" not in text
        assert "NOT independently supported" in text
        assert len(text) <= stats["used_chars"] <= stats["page_budget_chars"]
        assert [row["citation_index"] for row in included] == list(range(1, len(included) + 1))
    finally:
        database.close()


def test_mixed_evidence_invokes_generator_once_and_stream_failure_resets(tmp_path):
    config, _, _ = corpus(tmp_path)
    database = Database(config.db_path)
    try:
        rows = retrieve_hybrid(config, database, "DemoSet queries")
    finally:
        database.close()

    class AnswerModel:
        enabled, provider, model = True, "fake", "test"

        def __init__(self, fail=False):
            self.calls, self.fail = 0, fail

        def stream_generate(self, system, prompt, **_kwargs):
            self.calls += 1
            assert "RAW EXCERPT" in prompt and "SUPPORTED CLAIM" in prompt
            assert "99999" not in prompt
            yield "DemoSet contains 85 queries [1]."
            if self.fail:
                raise RuntimeError("private echoed model prompt must not leak")

    client = AnswerModel()
    assert answer_with_metadata("DemoSet queries", rows, client)["mode"] == "llm"
    assert client.calls == 1
    client = AnswerModel(True)
    events = list(answer_events("DemoSet queries", rows, client))
    assert client.calls == 1 and any(event["event"] == "reset" for event in events)
    assert events[-1]["data"]["mode"] == "local_evidence"
    assert "private echoed" not in events[-1]["data"]["fallback_reason"]


def test_experimental_graph_roles_are_specific_to_supported_entity_and_quote(tmp_path):
    config, _, _ = corpus(tmp_path)
    database = Database(config.db_path)
    try:
        graph = project_research_graph(database, config.root, center="__all__")
        # The model's dataset usage_status is training, but the source says testing.
        edge = next(edge for edge in graph["edges"] if edge["semantic"] == "TESTED_ON")
        assert edge["verification_status"] == "supported" and edge["claim_id"]
        assert edge["locator"] and "for testing" in edge["evidence"]
        assert not any(edge["semantic"] == "TRAINED_ON" for edge in graph["edges"])
    finally:
        database.close()
    assert _dataset_role("We trained on SetA and tested on SetB.", "SetA") == "TRAINED_ON"
    assert _dataset_role("We trained on SetA and tested on SetB.", "SetB") == "TESTED_ON"
    assert not _dataset_role("SetA is mentioned. We tested on SetB.", "SetA")


def test_multiline_original_snippets_are_relevant_exact_substrings_not_cutoff_header():
    original = "y\nA Traceable Paper\nDemoSet contains 85 queries. Other topics are discussed.\n评测采用DemoSet数据集。该数据集包含85个查询。另外介绍无关工作。"
    item = {"evidence_type": "raw_excerpt", "content": original}
    excerpts = _snippets("DemoSet queries", item)
    assert 1 <= len(excerpts) <= 2
    assert all(sentence in original and len(sentence) >= 3 for sentence in excerpts)
    assert "y" not in excerpts and "A Traceable Paper" not in excerpts
    chinese = _snippets("DemoSet 数据集 查询", item)
    assert all(sentence in original for sentence in chinese)
    assert "另外介绍无关工作。" not in chinese


def test_explicit_paper_title_scopes_supported_and_raw_evidence(tmp_path):
    config = load_config(tmp_path)
    title = "Unified Browser Acceptance Study"
    target = tmp_path / "browser.txt"
    target.write_text(title + "\nResearcher\nAbstract: This paper reports 17 observations in a controlled experiment.\nKeywords: observations\n", encoding="utf-8")
    assert ingest(config, target, quality_mode="basic")["status"] == "indexed"
    target_id = hashlib.sha256(target.read_bytes()).hexdigest()
    other = tmp_path / "verified.txt"
    fact = "The verified fixture reports 41 observations in its experiment."
    other.write_text("Verified Fixture Study\nAbstract: A verification study.\nKeywords: observations\n1. Introduction\n" + fact)

    class FixtureModel:
        enabled = True

        def structured(self, *_args, **_kwargs):
            return {"findings": [fact], "claims": [{"statement": fact, "claim_type": "finding", "evidence": {"quote": fact}}]}

    assert ingest(config, other, quality_mode="standard", llm_client=FixtureModel())["status"] == "indexed"
    other_id = hashlib.sha256(other.read_bytes()).hexdigest()
    database = Database(config.db_path)
    try:
        question = "How many observations does the " + title + " report?"
        rows = retrieve_hybrid(config, database, question)
        assert rows and all(row["sources"][0]["source_id"] == target_id for row in rows)
        result = answer_with_metadata(question, rows)
        assert "17 observations" in result["answer"] and "41 observations" not in result["answer"]
        assert "Verified Fixture" not in result["answer"]
        assert all(card["provenance"][0]["source_id"] == target_id for card in result["sources"])
        assert not retrieve_hybrid(config, database, question, evidence_scope="wiki")
        unrestricted = retrieve_hybrid(config, database, "How many observations are reported?")
        assert {row["sources"][0]["source_id"] for row in unrestricted} == {target_id, other_id}
        scope, _ = named_paper_scope(database, "Compare " + title + " with Verified Fixture Study")
        assert scope == {target_id, other_id}
    finally:
        database.close()


def test_unrelated_original_excerpt_does_not_emit_a_meaningless_header_fragment():
    fragment = "earcher\nAn Irrelevant Bibliographic Header\nThis paragraph introduces a general discussion."
    item = {"title": "A Complete Known Research Title", "type": "source", "content": fragment,
            "evidence_type": "raw_excerpt", "verification_status": "raw_excerpt",
            "sources": [{"source_id": "fake", "locator": "page:1#chars=0-100", "evidence_text": fragment}]}
    question = "How many observations does A Complete Known Research Title report?"
    assert not _snippets(question, item)
    result = answer_with_metadata(question, [item])
    assert result["mode"] == "no_evidence"
    assert "earcher" not in result["answer"]


def test_nested_title_matches_choose_longest_title_but_duplicates_remain_ambiguous():
    database = SimpleNamespace(list_papers=lambda **_kwargs: {"items": [
        {"id": "short", "title": "Unified Browser Acceptance"},
        {"id": "long", "title": "Unified Browser Acceptance Study"},
        {"id": "duplicate", "title": "Unified Browser Acceptance Study"},
        {"id": "acronym", "title": "UBAS"}]})
    assert named_paper_scope(database, "How many observations does Unified Browser Acceptance Study report?")[0] == {"long", "duplicate"}
    assert named_paper_scope(database, "What is UBAS?")[0] is None
