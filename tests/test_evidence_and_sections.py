"""Evidence-location and section-selection regression tests."""
from researchwiki.extraction.context import extraction_context
from researchwiki.extraction.evidence import locate_quote, verify_field_evidence
from researchwiki.models import ParsedDocument, Page, StructuredPaper


def test_deep_context_keeps_method_and_experiment_middle():
    original = (
        "Reliable Research Paper\nAbstract\n" + "background " * 9000 +
        "\n2 Method\n" + "Our method uses a conditional evidence gate.\n" * 30 +
        "\n4 Experiments\n" + "We evaluate on LeCaRD using NDCG@10.\n" * 30 +
        "\n5 Conclusion\nOur method is limited by missing labels.\n" +
        "\nReferences\n" + "irrelevant " * 12000
    )
    document = ParsedDocument(text=original, pages=[Page(1, original)])
    context = extraction_context(document)
    assert "Our method uses a conditional evidence gate" in context
    assert "We evaluate on LeCaRD using NDCG@10" in context
    assert "Our method is limited by missing labels" in context
    assert "irrelevant irrelevant irrelevant" not in context
    assert len(context) <= 65000


def test_quotes_must_exist_in_original_source():
    pages = [Page(1, "A few words."), Page(2, "LeCaRD is used in the final benchmark for ranking.")]
    original = ParsedDocument(text="\n".join(x.text for x in pages), pages=pages)
    exact = locate_quote(original, "LeCaRD is used in the final benchmark for ranking.")
    assert exact["status"] == "quote_matched"
    assert exact["page"] == 2
    assert pages[1].text[exact["char_start"]:exact["char_end"]] == exact["quote"]
    fake = locate_quote(original, "There are 200 thousand labeled examples in this set.")
    assert fake["status"] == "unverified"
    records = verify_field_evidence(original, [
        {"field": "experiment_info", "quote": "LeCaRD is used in the final benchmark for ranking."},
        {"field": "findings.0", "quote": "There are 200 thousand labeled examples in this set."},
        {"field": "../../escape", "quote": "LeCaRD is used in the final benchmark for ranking."},
    ])
    assert len(records) == 2
    assert records[0]["status"] == "quote_matched"
    assert records[1]["status"] == "unverified"


def test_old_cache_remains_compatible_and_dataset_role_is_explicit():
    paper = StructuredPaper.from_dict({
        "title": "Old Study",
        "datasets": [{"name": "LeCaRD", "role": "Mentioned in prior work"}],
    })
    assert paper.field_evidence == []
    assert paper.datasets[0].usage_status == "mentioned"
