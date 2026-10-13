import json

import pytest

import researchwiki.extraction.paper as extraction
from researchwiki.extraction.checkpoints import summarize_checkpoints
from researchwiki.extraction.paper import analyze_paper, document_chunks, extract_paper
from researchwiki.llm.client import StructuredOutputError
from researchwiki.models import Page, ParsedDocument


OPTIONS = {"max_chunk_chars": 2500, "min_chunk_chars": 512, "max_split_depth": 2}


def source(repeats=500):
    text = "Checkpoint Paper\n\n1 Introduction\n" + "Readable source material. " * repeats
    return ParsedDocument(text, [Page(1, text)])


class CountingLLM:
    enabled = True
    provider = "fake"
    model = "test-model"
    base_url = "https://example.test/v1"
    max_output_tokens = 8192
    json_max_retries = 1
    max_retries = 1
    timeout_seconds = 180

    def __init__(self, failure_on=None):
        self.users = []
        self.analysis_users = []
        self.failure_on = failure_on
        self.last_response_meta = {}

    def structured(self, _system, user):
        self.users.append(user)
        if self.failure_on == len(self.users):
            raise RuntimeError("network interrupted")
        self.last_response_meta = {"finish_reason": "stop", "request_attempts": 1, "json_attempts": 1,
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                                   "api_key": "SECRET", "raw_response": "PRIVATE_TEXT"}
        return {"title": "Checkpoint Paper", "methods": [{"name": "Checkpoint Method"}],
                "findings": ["Source details were read."]}

    def generate(self, _system, user, temperature=0.1):
        self.analysis_users.append(user)
        if self.failure_on == len(self.analysis_users):
            raise RuntimeError("network interrupted")
        return "Analysis of this source chunk."


def assert_covered(document, metadata):
    spans = [span for chunk in metadata["chunks"] for span in chunk["segments"]]
    for page in document.pages:
        local = [span for span in spans if span["page"] == page.number]
        assert local[0]["char_start"] == 0
        assert local[-1]["char_end"] == len(page.text)
        assert all(a["char_end"] == b["char_start"] for a, b in zip(local, local[1:]))
        assert "".join(page.text[value["char_start"]:value["char_end"]] for value in local) == page.text
    assert metadata["covered_characters"] == metadata["source_characters"]


def test_extraction_resume_only_calls_unfinished_chunks_and_preserves_metadata(tmp_path):
    document = source()
    model = CountingLLM(failure_on=2)
    events = []
    with pytest.raises(RuntimeError, match="network interrupted"):
        extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS, progress_callback=events.append)
    assert len(model.users) == 2
    assert len(list(tmp_path.glob("extraction_*.json"))) == 1
    assert events[-1]["status"] == "failed" and events[-1]["completed"] == 1
    first_complete = next(value for value in events if value["status"] == "completed")
    assert first_complete["current"] == first_complete["completed"] == 1
    assert events[-1]["current"] == 2
    model.failure_on = None
    model.users = []
    events = []
    paper = extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS, progress_callback=events.append)
    assert len(model.users) == len(document_chunks(document, 2500)) - 1
    assert paper.extraction_meta["cached_chunk_count"] == 1
    assert paper.extraction_meta["max_chunk_chars"] == 2500
    assert events[-1]["completed"] == events[-1]["total"]
    assert events[-1]["current"] == events[-1]["total"]
    assert any(value["status"] == "cached" for value in events)
    assert all("SECRET" not in json.dumps(value) and "PRIVATE_TEXT" not in json.dumps(value) for value in events)
    assert_covered(document, paper.extraction_meta)
    assert summarize_checkpoints(tmp_path)["completed"]["extraction"] == len(document_chunks(document, 2500))


def test_analysis_and_extraction_have_independent_recoverable_checkpoints(tmp_path):
    document = source(150)
    model = CountingLLM(failure_on=2)
    with pytest.raises(RuntimeError, match="network interrupted"):
        analyze_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    model.failure_on = None
    model.analysis_users = []
    notes = analyze_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    assert len(model.analysis_users) == len(document_chunks(document, 2500)) - 1
    extract_paper(document, model, analysis=notes, checkpoint_dir=tmp_path, options=OPTIONS)
    fresh = CountingLLM()
    assert analyze_paper(document, fresh, checkpoint_dir=tmp_path, options=OPTIONS) == notes
    extract_paper(document, fresh, analysis=notes, checkpoint_dir=tmp_path, options=OPTIONS)
    assert fresh.users == [] and fresh.analysis_users == []
    counts = summarize_checkpoints(tmp_path)["completed"]
    assert counts["analysis"] == counts["extraction"] == len(document_chunks(document, 2500))


@pytest.mark.parametrize("change", ["source", "parser", "provider", "model", "base_url", "max_output_tokens",
                                    "json_max_retries", "purpose", "analysis", "system_prompt", "user_prompt",
                                    "chunk_options", "extraction_version"])
def test_cache_identity_invalidates_when_inputs_or_behavior_change(tmp_path, monkeypatch, change):
    document = source(50)
    model = CountingLLM()
    extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    model.users.clear()
    options, purpose, analysis = dict(OPTIONS), "", ""
    if change == "source":
        document = source(51)
    elif change == "parser":
        for block in document.blocks:
            block.parser_version = "different-parser-version"
    elif change in {"provider", "model", "base_url", "max_output_tokens", "json_max_retries"}:
        original = getattr(model, change)
        setattr(model, change, original + 1 if isinstance(original, int) else original + "changed")
    elif change == "purpose":
        purpose = "A new question"
    elif change == "analysis":
        analysis = "New analysis context"
    elif change == "system_prompt":
        monkeypatch.setattr(extraction, "PAPER_EXTRACTION_SYSTEM", extraction.PAPER_EXTRACTION_SYSTEM + " changed")
    elif change == "user_prompt":
        original_prompt = extraction.paper_extraction_prompt
        monkeypatch.setattr(extraction, "paper_extraction_prompt", lambda *a, **kw: original_prompt(*a, **kw) + " changed")
    elif change == "chunk_options":
        options["max_chunk_chars"] = 2000
    elif change == "extraction_version":
        monkeypatch.setattr(extraction, "EXTRACTION_VERSION", "new-version")
    extract_paper(document, model, checkpoint_dir=tmp_path, options=options, purpose=purpose, analysis=analysis)
    assert len(model.users) == len(document_chunks(document, options["max_chunk_chars"]))


def test_force_refresh_overwrites_success_without_changing_resume_namespace(tmp_path):
    document, model = source(50), CountingLLM()
    extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    count = len(model.users)
    extract_paper(document, model, checkpoint_dir=tmp_path, options={**OPTIONS, "force_refresh": True})
    assert len(model.users) == 2 * count
    resumed = CountingLLM()
    extract_paper(document, resumed, checkpoint_dir=tmp_path, options=OPTIONS)
    assert resumed.users == []
    assert len(list(tmp_path.glob("extraction_*.json"))) == count


def test_distinct_custom_client_implementation_does_not_reuse_unknown_provider_cache(tmp_path):
    class OtherClient(CountingLLM):
        pass

    document, original = source(50), CountingLLM()
    extract_paper(document, original, checkpoint_dir=tmp_path, options=OPTIONS)
    different = OtherClient()
    extract_paper(document, different, checkpoint_dir=tmp_path, options=OPTIONS)
    assert len(different.users) == len(document_chunks(document, OPTIONS["max_chunk_chars"]))


def test_corrupt_checkpoint_is_reported_and_recomputed(tmp_path):
    document, model = source(50), CountingLLM()
    extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    files = sorted(tmp_path.glob("extraction_*.json"))
    files[0].write_text('{"unfinished":', encoding="utf-8")
    events, recovered = [], CountingLLM()
    extract_paper(document, recovered, checkpoint_dir=tmp_path, options=OPTIONS, progress_callback=events.append)
    assert len(recovered.users) == 1
    assert any(event["status"] == "cache_invalid" for event in events)
    assert summarize_checkpoints(tmp_path)["invalid"] == 0


def test_checksum_mismatch_cannot_silently_change_cached_payload(tmp_path):
    document, model = source(50), CountingLLM()
    extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    path = next(tmp_path.glob("extraction_*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entry"]["payload"]["title"] = "Changed without checksum"
    path.write_text(json.dumps(data), encoding="utf-8")
    events, recovered = [], CountingLLM()
    result = extract_paper(document, recovered, checkpoint_dir=tmp_path, options=OPTIONS, progress_callback=events.append)
    assert len(recovered.users) == 1 and result.title == "Checkpoint Paper"
    assert any(event["status"] == "cache_invalid" for event in events)


class SplittingLLM(CountingLLM):
    def __init__(self, threshold, reason="truncated", fail_after_successes=None):
        super().__init__()
        self.threshold = threshold
        self.reason = reason
        self.fail_after_successes = fail_after_successes
        self.successes = 0

    def structured(self, system, user):
        # The fixture counts failed and successful requests just as a provider.
        if len(user) > self.threshold:
            self.users.append(user)
            self.last_response_meta = {"finish_reason": "length", "request_attempts": 1}
            raise StructuredOutputError("output was truncated", reason=self.reason, retryable=True)
        if self.fail_after_successes == self.successes:
            self.users.append(user)
            raise RuntimeError("network interrupted")
        self.successes += 1
        return super().structured(system, user)


@pytest.mark.parametrize("reason", ["truncated", "invalid_json"])
def test_bounded_split_recovery_keeps_full_source_and_resumes_split_plan(tmp_path, reason):
    document = source(180)
    opts = {"max_chunk_chars": 8000, "min_chunk_chars": 512, "max_split_depth": 2}
    model = SplittingLLM(3000, reason=reason)
    events = []
    paper = extract_paper(document, model, checkpoint_dir=tmp_path, options=opts, progress_callback=events.append)
    assert paper.extraction_meta["planned_chunk_count"] == 1
    assert paper.extraction_meta["chunk_count"] == 2
    assert_covered(document, paper.extraction_meta)
    assert len(model.users) == 3
    assert events[-1]["total"] == events[-1]["completed"] == 2
    assert any(value["status"] == "split" and value["reason"] == reason for value in events)
    resumed = SplittingLLM(3000, reason=reason)
    again = extract_paper(document, resumed, checkpoint_dir=tmp_path, options=opts)
    assert resumed.users == []
    assert again.extraction_meta["cached_chunk_count"] == 2
    assert [value["chunk_id"] for value in again.extraction_meta["chunks"]] == [value["chunk_id"] for value in paper.extraction_meta["chunks"]]


def test_failed_second_split_child_does_not_repeat_parent_or_first_child(tmp_path):
    document = source(180)
    opts = {"max_chunk_chars": 8000, "min_chunk_chars": 512, "max_split_depth": 2}
    broken = SplittingLLM(3000, fail_after_successes=1)
    with pytest.raises(RuntimeError, match="network interrupted"):
        extract_paper(document, broken, checkpoint_dir=tmp_path, options=opts)
    assert len(broken.users) == 3
    resumed = SplittingLLM(3000)
    paper = extract_paper(document, resumed, checkpoint_dir=tmp_path, options=opts)
    assert len(resumed.users) == 1
    assert paper.extraction_meta["cached_chunk_count"] == 1
    assert_covered(document, paper.extraction_meta)


def test_split_budget_stops_and_never_checkpoints_truncated_content(tmp_path):
    document = source(180)
    opts = {"max_chunk_chars": 8000, "min_chunk_chars": 512, "max_split_depth": 1}
    model = SplittingLLM(10)
    with pytest.raises(RuntimeError, match="truncated"):
        extract_paper(document, model, checkpoint_dir=tmp_path, options=opts)
    assert len(model.users) == 2  # parent, then first child; no unbounded loop
    assert summarize_checkpoints(tmp_path)["completed"]["extraction"] == 0
    assert summarize_checkpoints(tmp_path)["split_plans"] == 1


def test_transport_timeout_does_not_fan_out_to_many_split_calls(tmp_path):
    document = source(180)
    model = CountingLLM(failure_on=1)
    with pytest.raises(RuntimeError, match="network interrupted"):
        extract_paper(document, model, checkpoint_dir=tmp_path, options=OPTIONS)
    assert len(model.users) == 1 and list(tmp_path.glob("*.json")) == []


def test_invalid_schema_is_not_written_as_completed_checkpoint(tmp_path):
    class InvalidPayload(CountingLLM):
        def structured(self, _system, _user):
            return {"title": "Malformed Paper", "claims": "not-an-array"}

    with pytest.raises(RuntimeError, match="invalid paper extraction JSON"):
        extract_paper(source(20), InvalidPayload(), checkpoint_dir=tmp_path, options=OPTIONS)
    assert list(tmp_path.glob("*.json")) == []


def test_short_sections_are_coalesced_without_changing_parser_labels_or_offsets():
    text = "Checkpoint Paper\n\n" + "\n\n".join("## Section " + str(index) + "\n" + "A short paragraph. " * 4 for index in range(40))
    document = ParsedDocument(text, [Page(1, text)])
    chunks = document_chunks(document, 12000)
    assert len(chunks) == 1
    assert len(chunks[0].metadata()["sections"]) == 41
    assert all(len(value.text) <= 12000 for value in chunks)
    assert_covered(document, {"chunks": [value.metadata() for value in chunks],
                             "covered_characters": len(text), "source_characters": len(text)})


def test_compact_labels_keep_full_section_and_complete_block_metadata():
    section = ("A deliberately long section label " + "with precise parser context " * 3).strip()
    first = "## " + section + "\n\n" + "\n\n".join("Body paragraph " + str(index) + ". " * 30 for index in range(40))
    second = "\n\n".join("More page text " + str(index) + ". " * 30 for index in range(40))
    pages = [Page(1, first), Page(2, second)]
    document = ParsedDocument("\n\n".join(page.text for page in pages), pages)
    chunks = document_chunks(document, 12000)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text.startswith("\n[SECTION " + section + "]\n[PAGE 1; CHARS 0:")
    assert chunk.text.count("[SECTION ") == 1
    assert chunk.text.count("[PAGE ") == 2
    assert "BLOCK" not in chunk.text
    assert len(chunk.segments) > 70
    assert all(value["block_id"] for value in chunk.segments)
    assert set(chunk.metadata()["block_ids"]) == {value.block_id for value in document.blocks}
    old_annotation_characters = sum(len("\n[PAGE {page}; CHARS {char_start}:{char_end}; SECTION {section}; BLOCK {block_id}]\n".format(**span))
                                   for span in chunk.segments)
    actual_annotation_characters = len(chunk.text) - len(first) - len(second)
    assert actual_annotation_characters < old_annotation_characters / 5
    assert_covered(document, {"chunks": [chunk.metadata()], "covered_characters": len(first) + len(second),
                             "source_characters": len(first) + len(second)})


def test_chunk_starts_and_split_children_always_carry_current_section_label():
    text = "## Detailed Section\n\n" + "\n\n".join("Paragraph context. " * 30 for _ in range(40))
    document = ParsedDocument(text, [Page(1, text)])
    chunks = document_chunks(document, 2500)
    assert len(chunks) > 2
    assert all(value.text.startswith("\n[SECTION " + value.segments[0]["section"] + "]\n[PAGE ") for value in chunks)
    assert all(value.text.count("[SECTION ") == 1 and value.text.count("[PAGE ") == 1 for value in chunks)
    children = extraction._split_chunk(document, chunks[0], 512)
    assert len(children) == 2
    assert all(value.text.startswith("\n[SECTION " + value.segments[0]["section"] + "]\n[PAGE ") for value in children)
    assert all(value.text.count("[SECTION ") == 1 and value.text.count("[PAGE ") == 1 for value in children)
    assert chunks == document_chunks(document, 2500)


def test_analysis_split_notes_remain_local_to_parent_chunk(tmp_path):
    class SplitAnalysis(CountingLLM):
        def generate(self, _system, user, temperature=0.1):
            self.analysis_users.append(user)
            if len(user) > 3000:
                raise StructuredOutputError("output was truncated", reason="truncated", retryable=True)
            return "LOCAL_ANALYSIS"

    document = source(180)
    opts = {"max_chunk_chars": 8000, "min_chunk_chars": 512, "max_split_depth": 2}
    model = SplitAnalysis()
    notes = analyze_paper(document, model, checkpoint_dir=tmp_path, options=opts)
    assert len(model.analysis_users) == 3
    root = document_chunks(document, 8000)[0]
    assert notes.startswith("<!-- " + root.chunk_id + " -->")
    assert extraction._analysis_for_chunk(notes, root).count("LOCAL_ANALYSIS") == 2
    fresh = SplitAnalysis()
    assert analyze_paper(document, fresh, checkpoint_dir=tmp_path, options=opts) == notes
    assert fresh.analysis_users == []
