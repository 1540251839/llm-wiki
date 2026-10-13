"""Offline coverage for the unified queue, original evidence, and Web contracts."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from researchwiki.app import ingest
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.index.database import IngestJobConflict
from researchwiki.models import Page
from researchwiki.web import create_app
from researchwiki.web.app import _drain_ingest_queue


def source_fixture(root):
    source = root / "source.txt"
    source.write_text(
        "A Study of Auditable Research Evidence\n"
        "Abstract: This study compares precise evidence retrieval across research sources.\n"
        "The RawOnlyMarker999 technique preserves original research evidence.\n"
        "Another finding with SecondEvidenceMarker777 appears in the same source.\n",
        encoding="utf-8",
    )
    config = load_config(root)
    ingest(config, source, quality_mode="basic")
    return config, hashlib.sha256(source.read_bytes()).hexdigest()


def test_queue_operation_focus_timing_and_fresh_are_persistent(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    job = db.enqueue_ingest_job("a" * 64, "source.txt", "standard", operation="supplement",
                                focus="method", force_refresh=True)
    assert job["operation"] == "supplement" and job["focus"] == "method"
    assert job["force_refresh"] is True
    import pytest
    assert db.enqueue_ingest_job("a" * 64, "source.txt", "standard", operation="supplement",
                                 focus="method", force_refresh=True)["id"] == job["id"]
    with pytest.raises(IngestJobConflict):
        db.enqueue_ingest_job("a" * 64, "source.txt", "standard", operation="supplement", focus="experiment")
    claimed = db.claim_next_ingest_job()
    assert claimed["progress"] == {} and claimed["stage_timings"] == {}
    db.update_ingest_job(job["id"], status="FAILED", progress={"cached": 2}, stage_timings={"EXTRACT": 1.5})
    db.close()
    db = Database(tmp_path / "db.sqlite")
    row = db.get_ingest_job(job["id"])
    assert row["progress"] == {"cached": 2}
    assert row["stage_timings"] == {"EXTRACT": 1.5}
    retry = db.retry_ingest_job(job["id"])
    assert retry["force_refresh"] is False
    assert retry["operation"] == "supplement" and retry["focus"] == "method"
    retry = db.claim_next_ingest_job()
    assert retry["progress"] == {} and retry["stage_timings"] == {}
    db.close()


def test_new_database_has_independent_identity_and_restart_keeps_it(tmp_path):
    first_path = tmp_path / "first.sqlite"
    first = Database(first_path)
    identity = first.library_id
    assert len(identity) == 32 and first.overview()["library_id"] == identity
    first.close()
    first = Database(first_path)
    second = Database(tmp_path / "second.sqlite")
    assert first.library_id == identity
    assert second.library_id != identity
    first.close()
    second.close()
    with TestClient(create_app(tmp_path / "workspace")) as client:
        assert client.get("/api/health").json()["storage"]["library_id"] == client.get("/api/overview").json()["library_id"]


def test_browser_history_is_library_scoped_and_keeps_theme():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for browser cache isolation check")
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function syncLibraryIdentity(');
const end = source.indexOf('function saveAnswer(', start);
const state = {libraryId:null,askRequestId:0,askHistory:[],currentAnswer:null};
const stored = new Map([['researchwiki.theme','dark'],['researchwiki.askHistory',JSON.stringify([{question:'old',result:{answer:'old'}}])]]);
const localStorage = {getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
const nodes = new Map();
const $ = key=>{if(!nodes.has(key))nodes.set(key,{classList:{add(){}},innerHTML:'',textContent:'',disabled:false});return nodes.get(key);};
const sandbox = {state,localStorage,$,renderAskHistory(){}};
vm.createContext(sandbox);
vm.runInContext(source.slice(start,end), sandbox);
sandbox.syncLibraryIdentity('a'.repeat(32));
assert.strictEqual(state.askHistory.length,0);
assert.strictEqual(stored.has('researchwiki.askHistory'),false);
assert.strictEqual(stored.get('researchwiki.theme'),'dark');
const saved = [{question:'current',result:{answer:'valid current result'}}];
stored.set('researchwiki.askHistory',JSON.stringify({library_id:'a'.repeat(32),items:saved}));
state.libraryId=null;
sandbox.syncLibraryIdentity('a'.repeat(32));
assert.strictEqual(state.askHistory[0].question,'current');
sandbox.syncLibraryIdentity('b'.repeat(32));
assert.strictEqual(state.askHistory.length,0);
assert.strictEqual(stored.has('researchwiki.askHistory'),false);
assert.strictEqual(stored.get('researchwiki.theme'),'dark');
"""
    asset = Path(__file__).parents[1] / "researchwiki" / "web" / "static" / "app.js"
    subprocess.run([node, "-e", script, str(asset)], check=True, capture_output=True, text=True)


def test_queue_runner_forwards_focused_resume_options(tmp_path):
    config, source_id = source_fixture(tmp_path)
    db = Database(config.db_path)
    source = db.get_source(source_id)
    db.enqueue_ingest_job(source_id, source["raw_path"], "standard", operation="supplement",
                          focus="experiment", force_refresh=True)
    db.close()
    with patch("researchwiki.web.app.ingest") as model_free_ingest:
        _drain_ingest_queue(tmp_path)
    assert model_free_ingest.call_count == 1
    assert model_free_ingest.call_args.kwargs["focus"] == "experiment"
    assert model_free_ingest.call_args.kwargs["operation"] == "supplement"
    assert model_free_ingest.call_args.kwargs["force_refresh"] is True
    assert model_free_ingest.call_args.kwargs["resume"] is True


def test_original_search_survives_deep_failure_but_not_hash_or_parser_changes(tmp_path):
    config, source_id = source_fixture(tmp_path)
    db = Database(config.db_path)
    db.update_paper_status(source_id, "FAILED", "A later paid extraction failed")
    evidence = db.search_source_chunks("RawOnlyMarker999")
    assert len(evidence) == 1 and evidence[0]["source_hash_verified"]
    assert evidence[0]["evidence_key"].startswith("raw:" + source_id)
    source = db.get_source(source_id)
    original = Path(source["raw_path"])
    raw = original.read_bytes()
    original.write_bytes(raw + b"changed")
    assert db.search_source_chunks("RawOnlyMarker999") == []
    original.write_bytes(raw)
    db.conn.execute("UPDATE source_chunks SET parser_version='invalid-parser'")
    db.conn.commit()
    assert db.search_source_chunks("RawOnlyMarker999") == []
    db.index_source_chunks(source_id, [Page(1, raw.decode("utf-8"))])
    assert db.search_source_chunks("RawOnlyMarker999")
    db.conn.execute("UPDATE source_chunks SET char_start=char_start+1")
    db.conn.commit()
    assert db.search_source_chunks("RawOnlyMarker999") == []
    db.close()


def test_original_excerpt_expands_word_boundaries_with_exact_offsets(tmp_path):
    config = load_config(tmp_path)
    prefix = "A Study of Auditable Original Evidence\nAbstract: This paper describes evidence provenance.\n"
    # Both initial 120-character look-behind and 600-character cutoff land in
    # ordinary English words, not in spaces or at source chunk boundaries.
    text = prefix + ("context " * 45) + "Researcher " + ("x " * 56) + "BoundaryMarker900 " + ("y " * 228) + "ExperimentalIdentifier2026 trailing evidence."
    source = tmp_path / "boundaries.txt"
    source.write_text(text, encoding="utf-8")
    ingest(config, source, quality_mode="basic")
    db = Database(config.db_path)
    try:
        matched = db.search_source_chunks("BoundaryMarker900")[0]
        start, end = matched["char_start"], matched["char_end"]
        assert matched["excerpt"] == text[start:end]
        assert "BoundaryMarker900" in matched["excerpt"]
        assert not (start > 0 and text[start - 1].isascii() and text[start - 1].isalnum() and text[start].isascii() and text[start].isalnum())
        assert not (end < len(text) and text[end - 1].isascii() and text[end - 1].isalnum() and text[end].isascii() and text[end].isalnum())
        assert len(matched["excerpt"]) <= 800
    finally:
        db.close()


def test_original_and_hybrid_answer_api_have_precise_private_safe_evidence(tmp_path):
    config, source_id = source_fixture(tmp_path)
    with patch("researchwiki.web.app.LLMClient") as model, patch("researchwiki.app.LLMClient") as answer_model:
        model.return_value.enabled = False
        answer_model.return_value.enabled = False
        answer_model.return_value.provider = "deepseek"
        answer_model.return_value.model = "mock-offline"
        with TestClient(create_app(tmp_path)) as client:
            found = client.get("/api/source-search", params={"q": "RawOnlyMarker999"}).json()
            item = found["items"][0]
            assert item["source_hash_verified"]
            assert item["evidence_key"].startswith("raw:" + source_id)
            excerpt = client.get(item["evidence_url"]).json()
            assert excerpt["text"] == item["excerpt"]
            assert client.get(f"/api/documents/{source_id}/evidence?end=999999").status_code == 422
            answer = client.post("/api/ask", json={"question": "RawOnlyMarker999"}).json()
            assert answer["mode"] == "local_evidence"
            assert answer["sources"]
            assert all(source["citation_index"] > 0 for source in answer["sources"])
            assert str(tmp_path) not in str(answer)
            only_wiki = client.post("/api/ask", json={"question": "RawOnlyMarker999", "evidence_scope": "wiki"})
            assert only_wiki.status_code == 200
            assert only_wiki.json()["mode"] == "no_evidence"
            assert client.post("/api/ask", json={"question": "marker", "evidence_scope": "invalid"}).status_code == 422
            streamed = client.post("/api/ask/stream", json={"question": "RawOnlyMarker999"})
            assert "event: result" in streamed.text
            assert client.get("/api/claims").status_code == 200
            assert client.get("/api/evaluation/questions").status_code == 404
            assert "claim-status" in client.get("/").text
    answer_model.return_value.generate.assert_not_called()


def test_enrich_and_supplement_enqueue_without_discarding_checkpoints(tmp_path):
    config, source_id = source_fixture(tmp_path)
    with patch("researchwiki.web.app.LLMClient") as model, patch("researchwiki.web.app._drain_ingest_queue"):
        model.return_value.enabled = True
        with TestClient(create_app(tmp_path)) as client:
            forced = client.post(f"/api/documents/{source_id}/enrich?force_refresh=true")
            assert forced.status_code == 400
            forced = client.post(f"/api/documents/{source_id}/enrich?force=true&confirm=true")
            assert forced.status_code == 202
            assert forced.json()["job"]["force_refresh"] is True
            assert forced.json()["job"]["operation"] == "enrich"
            conflicting = client.post(f"/api/documents/{source_id}/supplement?focus=experiment", headers={"X-RW-Config-Action": "1"})
            assert conflicting.status_code == 409
            job = forced.json()["job"]
            db = Database(config.db_path)
            db.update_ingest_job(job["id"], status="FAILED")
            db.close()
            retry = client.post(f"/api/jobs/{job['id']}/retry")
            assert retry.status_code == 202 and retry.json()["job"]["force_refresh"] is False
            db = Database(config.db_path)
            db.update_ingest_job(job["id"], status="SUCCEEDED")
            db.close()
            local = {"X-RW-Config-Action": "1"}
            supplement = client.post(f"/api/documents/{source_id}/supplement?focus=method", headers=local)
            assert supplement.status_code == 202
            assert supplement.json()["job"]["operation"] == "supplement"
            assert supplement.json()["job"]["focus"] == "method"
            assert supplement.json()["job"]["force_refresh"] is False


def test_supported_page_evidence_keeps_multiple_claims_per_source(tmp_path):
    config, source_id = source_fixture(tmp_path)
    db = Database(config.db_path)
    paper = db.get_paper(source_id)
    page_id = db.conn.execute("SELECT id FROM wiki_pages LIMIT 1").fetchone()[0]
    for index in (1, 2):
        db.conn.execute("""INSERT INTO page_evidence(page_id,claim_id,source_id,page_number,char_start,char_end,
                         evidence_text,verification_status) VALUES(?,?,?,?,?,?,?,'supported')""",
                        (page_id, f"claim_{index}", source_id, 1, index, index+4, "text"))
    db.conn.commit()
    records = db.get_page_sources(page_id)
    assert len(records) == 2 and {row["claim_id"] for row in records} == {"claim_1", "claim_2"}
    assert paper["status"] == "INDEXED"
    db.close()


def test_claim_review_validates_quote_and_rolls_back_failed_compile(tmp_path):
    from researchwiki.models import StructuredPaper
    from researchwiki.parsing.pdf import parse_file
    from researchwiki.verification import ClaimsService
    config, source_id = source_fixture(tmp_path)
    cache = tmp_path / "data" / "extractions" / (source_id + ".json")
    value = json.loads(cache.read_text())
    value["extraction_mode"] = "standard"
    value["claims"] = [{"statement": "RawOnlyMarker999 keeps the evidence intact.",
                        "evidence": {"quote": "The RawOnlyMarker999 technique preserves original research evidence."}}]
    service = ClaimsService(config)
    service.write(cache, value)
    db = Database(config.db_path)
    raw = Path(db.get_source(source_id)["raw_path"])
    db.close()
    bundle = service.prepare(source_id, StructuredPaper.from_dict(value), parse_file(raw))
    claim = next(item for item in bundle["claims"] if item["statement"] == value["claims"][0]["statement"])
    assert claim["verification"]["status"] == "pending_review"
    before = service.path(source_id).read_bytes()
    with TestClient(create_app(tmp_path)) as client:
        listing = client.get("/api/claims", params={"source_id": source_id}).json()
        row = next(item for item in listing["items"] if item["claim_id"] == claim["claim_id"])
        assert client.get(row["evidence"]["evidence_url"]).json()["text"] == row["evidence"]["quote"]
        route = f"/api/documents/{source_id}/claims/{claim['claim_id']}/review"
        invalid = client.post(route, json={"status": "supported", "reason": "Confirm quote", "quote": "Invented absent quote"})
        assert invalid.status_code == 409
        assert service.path(source_id).read_bytes() == before
        with patch("researchwiki.web.app.CompilationService.run", return_value={"status": "failed", "error": "test disk failure"}):
            failed = client.post(route, json={"status": "supported", "reason": "Reviewed the exact original paragraph"})
            assert failed.status_code == 409
        assert service.load(source_id)["audit"] == json.loads(before)["audit"]
        accepted = client.post(route, json={"status": "supported", "reason": "Reviewed the exact original paragraph"})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["claim"]["verification"]["status"] == "supported"
        assert accepted.json()["compilation"]["status"] == "succeeded"
        assert raw.read_bytes().decode("utf-8").startswith("A Study of Auditable")
