import pytest

from researchwiki.extraction.paper import analyze_paper, document_chunks, extract_paper
from researchwiki.extraction.prompts import paper_analysis_prompt, paper_extraction_prompt
from researchwiki.extraction.quality import QualityGateError, assess_paper
from researchwiki.models import EntityMention, Page, ParsedDocument, StructuredPaper
from researchwiki.parsing.pdf import parse_file


def long_paper():
    pages = [
        Page(1, "Complete Paper\n\nAbstract\nA test of complete reading.\n" + "Introduction detail. " * 1400),
        Page(2, "2 Methods\n" + "Method detail. " * 1800),
        Page(3, "3 Experiments\n" + "Experiment setup. " * 500 + "\nMIDDLE_FACT: accuracy is 92.3%.\n" + "Ablation detail. " * 1500),
        Page(4, "4 Conclusions\nComplete document coverage matters.\n" + "Discussion detail. " * 1000),
    ]
    return ParsedDocument("\n\n".join(page.text for page in pages), pages)


class ChunkLLM:
    enabled = True
    provider = "fake"
    model = "test"

    def __init__(self):
        self.extraction_users = []
        self.analysis_users = []

    def generate(self, _system, user, temperature=0.1):
        self.analysis_users.append(user)
        return "Local analysis: " + ("MIDDLE_FACT" if "MIDDLE_FACT" in user else "other section")

    def structured(self, _system, user):
        self.extraction_users.append(user)
        claims = []
        if "MIDDLE_FACT: accuracy is 92.3%." in user:
            claims.append({"statement": "accuracy is 92.3%.", "claim_type": "experiment_result",
                           "subject": "Complete Paper", "entities": [{"type": "method", "name": "Covered Method"}],
                           "attributes": {"metric": "accuracy", "value": "92.3%"},
                           "evidence": {"quote": "MIDDLE_FACT: accuracy is 92.3%.", "page": 999, "char_start": -1},
                           "verification": {"status": "supported"}})
        return {"title": "Complete Paper", "abstract": "A test of complete reading.",
                "methods": [{"name": "Covered Method"}], "claims": claims}


def test_complete_page_coverage_and_middle_fact_is_extracted():
    document = long_paper()
    chunks = document_chunks(document)
    assert len(chunks) > 4
    assert all(len(chunk.text) <= 20000 for chunk in chunks)
    for page in document.pages:
        spans = [span for chunk in chunks for span in chunk.segments if span["page"] == page.number]
        assert spans[0]["char_start"] == 0
        assert spans[-1]["char_end"] == len(page.text)
        assert all(left["char_end"] == right["char_start"] for left, right in zip(spans, spans[1:]))
        assert "".join(page.text[span["char_start"]:span["char_end"]] for span in spans) == page.text
    model = ChunkLLM()
    paper = extract_paper(document, model)
    assert len(model.extraction_users) == len(chunks)
    assert any("MIDDLE_FACT: accuracy is 92.3%." in user for user in model.extraction_users)
    assert paper.extraction_meta["covered_characters"] == paper.extraction_meta["source_characters"]
    assert paper.extraction_meta["coverage"] == 1.0
    assert len(paper.methods) == 1  # deterministic local extraction de-duplication
    assert paper.claims[0]["evidence"] == {"quote": "MIDDLE_FACT: accuracy is 92.3%."}
    assert "verification" not in paper.claims[0]
    assert paper.claims[0]["extraction"]["pages"] == [3]
    assert document_chunks(document) == chunks


def test_high_analysis_covers_same_chunks_and_notes_stay_local():
    document = long_paper()
    model = ChunkLLM()
    analysis = analyze_paper(document, model)
    extract_paper(document, model, analysis=analysis)
    assert len(model.analysis_users) == len(model.extraction_users) == len(document_chunks(document))
    assert any("MIDDLE_FACT: accuracy is 92.3%." in user for user in model.analysis_users)
    for user in model.extraction_users:
        if "MIDDLE_FACT: accuracy is 92.3%." not in user:
            assert "Local analysis: MIDDLE_FACT" not in user


def test_prompt_formatters_never_discard_middle_source():
    text = "a" * 45000 + "MIDDLE_EVIDENCE" + "z" * 45000
    assert text in paper_analysis_prompt(text)
    assert text in paper_extraction_prompt(text)
    assert "middle omitted" not in paper_extraction_prompt(text)


def test_header_title_and_sparse_extraction_cannot_pass():
    header = StructuredPaper("Contents lists available at ScienceDirect", topics=["Graph neural networks"])
    assert not assess_paper(header)["allowed"]
    assert {value["code"] for value in assess_paper(header)["issues"]} >= {"suspicious_title", "sparse_extraction"}
    assert not assess_paper(StructuredPaper("A plausible title", topics=["Graphs"]))["allowed"]
    assert assess_paper(StructuredPaper("A valid legacy record", methods=[EntityMention("A method")]))["allowed"]

    class BrokenLLM:
        enabled = True

        def structured(self, _system, _user):
            return header.to_dict()

    with pytest.raises(QualityGateError, match="quality gate") as error:
        extract_paper(ParsedDocument("A readable source document"), BrokenLLM())
    assert error.value.stage == "extraction"
    assert not error.value.quality["allowed"]


def test_pdf_blocks_are_stable_and_keep_historical_page_offsets(tmp_path):
    import fitz

    path = tmp_path / "layout.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((70, 70), "Complete Paper")
    page.insert_text((70, 120), "2 Experiments\nAccuracy is 92.3%.")
    expected = page.get_text("text", sort=False)
    document.save(path)
    document.close()
    first, second = parse_file(path), parse_file(path)
    assert first.pages[0].text == expected
    assert first.blocks == second.blocks
    assert all(block.bbox for block in first.blocks)
    assert any(block.section == "2 Experiments" for block in first.blocks)
    for block in first.blocks:
        assert first.pages[block.page - 1].text[block.char_start:block.char_end] == block.text
    assert first.quality["allowed"]


def test_scan_and_corrupt_text_stop_before_model_calls(tmp_path):
    import fitz

    path = tmp_path / "scan.pdf"
    pdf = fitz.open()
    pdf.new_page()
    pdf.save(path)
    pdf.close()
    scanned = parse_file(path)
    assert not scanned.quality["allowed"]
    with pytest.raises(QualityGateError, match="OCR") as error:
        extract_paper(scanned, ChunkLLM())
    assert error.value.stage == "parsing"
    model = ChunkLLM()
    with pytest.raises(QualityGateError):
        extract_paper(ParsedDocument("\ufffd" * 60 + "text"), model)
    assert model.extraction_users == []


def test_structured_cache_old_and_new_fields_roundtrip():
    old = StructuredPaper.from_dict({"title": "Old Paper", "methods": ["Method"]})
    assert old.claims == [] and old.extraction_meta == {} and old.quality == {}
    new = StructuredPaper.from_dict({"title": "New Paper", "claims": [{"statement": "a fact"}],
                                     "extraction_meta": {"coverage": 1.0}, "quality": {"allowed": True}})
    assert StructuredPaper.from_dict(new.to_dict()) == new
