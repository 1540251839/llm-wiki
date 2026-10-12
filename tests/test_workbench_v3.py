import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from researchwiki.app import ingest
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.parsing.pdf import parse_file
from researchwiki.web import create_app
from researchwiki.compilation import compile_wiki
from researchwiki.app import rebuild
from researchwiki.wiki.manager import parse_frontmatter


class PaperModel:
    enabled = True

    def structured(self, _system, _user):
        return {"title": "Evidence Linking", "authors": ["Researcher"], "year": 2026,
                "abstract": "Evidence Linking preserves reliable source locations.",
                "methods": [{"name": "Trace Matching", "role": "Match exact source text."}],
                "datasets": [{"name": "PaperBench"}], "topics": ["Traceability"],
                "findings": ["Exact source locations make claims auditable."]}


NO_KEYS = {key: "" for key in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY", "OPENAI_API_KEY")}


def seed(root):
    path = root / "paper.txt"
    path.write_text("Evidence Linking\n\nTrace Matching preserves   exact spacing.\n\nPaperBench tests Traceability.\nExact source locations make claims auditable.")
    config = load_config(root)
    ingest(config, path, llm_client=PaperModel())
    return config, hashlib.sha256(path.read_bytes()).hexdigest()


def test_compile_evidence_backlinks_and_frozen_replay(tmp_path):
    with patch.dict(os.environ, NO_KEYS):
        config, source_id = seed(tmp_path)
        raw = tmp_path / "raw" / "papers" / (source_id + ".txt")
        before = raw.read_bytes()
        with TestClient(create_app(tmp_path)) as client:
            state = client.get("/api/compilation").json()
            assert state["inventory"]["ready_sources"] == 1
            built = client.post("/api/compilation/build").json()
            assert built["status"] == "succeeded", built
            assert built["index"]["pages"] >= 4
            repeated = client.post("/api/compilation/build").json()
            assert repeated["content_digest"] == built["content_digest"]
            assert raw.read_bytes() == before
            assert len(client.get("/api/compilation/history").json()["items"]) == 2
            assert client.get("/api/documents", params={"file_type": "txt"}).json()["total"] == 1
            assert client.get("/api/documents", params={"file_type": "pdf"}).json()["total"] == 0
            pages = client.get("/api/wiki/pages").json()["items"]
            assert all(Path(p["path"]).name not in {"index.md", "log.md"} for p in pages)
            found_backlink = False
            for item in pages:
                page = client.get(f"/api/wiki/pages/{item['id']}").json()
                found_backlink |= bool(page["backlinks"])
                for evidence in page["sources"]:
                    assert evidence["citation_index"] >= 1
                    exact = client.get(evidence["evidence_url"])
                    assert exact.status_code == 200, exact.text
                    assert exact.json()["text"] == evidence["evidence_text"]
                    assert exact.json()["source_hash_verified"]
                    assert evidence["paragraph"] > 0
            assert found_backlink
            response = client.get(f"/api/documents/{source_id}/file?inline=true")
            assert response.headers["content-disposition"].startswith("inline")
            assert client.get(f"/api/documents/{source_id}/evidence?page=999").status_code == 422
            comparison = client.post("/api/evaluation/compare").json()
            assert comparison["baseline"]["metrics"]["corpus_fingerprint"] == comparison["optimized"]["metrics"]["corpus_fingerprint"]
            replay = client.post(f"/api/evaluation/runs/{comparison['optimized']['id']}/replay").json()
            assert replay["score"] == comparison["optimized"]["score"]
            assert replay["metrics"]["replay_of"] == comparison["optimized"]["id"]
            assert raw.read_bytes() == before
            raw.write_text("tampered source")
            assert client.get(f"/api/documents/{source_id}/evidence").status_code == 409
            assert client.post("/api/compilation/build").json()["status"] == "failed"


def test_docx_parsing_and_type_filter(tmp_path):
    path = tmp_path / "paper.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Evidence Linking</w:t></w:r></w:p><w:p><w:r><w:t>Trace Matching</w:t></w:r></w:p></w:body></w:document>')
    doc = parse_file(path)
    assert doc.text == "Evidence Linking\n\nTrace Matching"
    assert len(doc.pages) == 1
    with patch.dict(os.environ, NO_KEYS):
        ingest(load_config(tmp_path), path, llm_client=PaperModel())
        with TestClient(create_app(tmp_path)) as client:
            docs = client.get("/api/documents?file_type=docx").json()
            assert docs["total"] == 1
            assert docs["items"][0]["file_type"] == "docx"


def test_failed_retry_validation_does_not_mutate_state(tmp_path):
    config, source_id = seed(tmp_path)
    db = Database(config.db_path)
    db.update_paper_status(source_id, "FAILED", "original error")
    db.close()
    with TestClient(create_app(tmp_path)) as client:
        assert client.post(f"/api/documents/{source_id}/retry?quality_mode=bogus").status_code == 400
        detail = client.get(f"/api/documents/{source_id}").json()
        assert detail["status"] == "FAILED"
        assert detail["error_message"] == "original error"


def test_sse_sends_model_deltas_and_sources_before_result(tmp_path):
    seed(tmp_path)

    class StreamModel:
        enabled = True
        provider = "test"
        model = "fake"

        def __init__(self, config):
            pass

        def stream_generate(self, system, user, temperature=0):
            yield "Evidence "
            yield "Linking preserves source locations [1]."

    with TestClient(create_app(tmp_path)) as client, patch("researchwiki.app.LLMClient", StreamModel):
        response = client.post("/api/ask/stream", json={"question": "What is Evidence Linking?"})
        events = [block for block in response.text.split("\n\n") if block]
        assert sum(block.startswith("event: delta") for block in events) == 2
        assert response.text.index("event: sources") < response.text.index("event: delta") < response.text.index("event: result")
        final = json.loads(events[-1].split("data: ", 1)[1])
        assert final["mode"] == "llm"
        assert final["citations"]["valid"]
        assert str(tmp_path) not in json.dumps(final)


def test_wiki_citation_order_follows_compiler_not_filename(tmp_path):
    config, first = seed(tmp_path)
    other = tmp_path / "another.txt"
    other.write_text("A second paper about Trace Matching and Evidence Linking.")
    class SecondPaperModel(PaperModel):
        def structured(self, system, user):
            return {**super().structured(system, user), "title": "Second Evidence Study"}
    ingest(config, other, llm_client=SecondPaperModel())
    assert compile_wiki(config)["status"] == "succeeded"
    rebuild(config)
    db = Database(config.db_path)
    pages = db.list_wiki_pages(page_type="method")
    page = db.get_wiki_page(pages[0]["id"])
    expected = parse_frontmatter(page["content"])["sources"]
    assert len(expected) == 2
    # Make source-name sorting intentionally disagree with citation numbering.
    db.conn.execute("UPDATE sources SET original_name='z.pdf' WHERE id=?", (expected[0],))
    db.conn.execute("UPDATE sources SET original_name='a.pdf' WHERE id=?", (expected[1],))
    db.conn.commit()
    db.close()
    with TestClient(create_app(tmp_path)) as client:
        result = client.get(f"/api/wiki/pages/{page['id']}").json()
        assert [source["source_id"] for source in result["sources"]] == expected
        assert [source["citation_index"] for source in result["sources"]] == [1, 2]
