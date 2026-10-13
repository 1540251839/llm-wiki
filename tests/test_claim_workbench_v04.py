import hashlib
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from researchwiki.app import ingest, rebuild
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.verification import ClaimsService
from researchwiki.web import create_app
from researchwiki.wiki.manager import WikiManager


NO_KEYS = {key: "" for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY")}


class FixtureModel:
    enabled = True

    def structured(self, _system, _user):
        statements = ["Method Alpha accuracy is 70%.", "Method Alpha uses Dataset Beta."]
        return {"title": "A Source Study", "methods": [{"name": "Method Alpha"}],
                "claims": [{"statement": statement, "claim_type": "finding",
                            "entities": [{"type": "method", "name": "Method Alpha"}],
                            "evidence": {"quote": statement, "page_number": 999}}
                           for statement in statements]}


def seed(tmp_path):
    raw = tmp_path / "study.txt"
    raw.write_text("A Source Study\n\nMethod Alpha accuracy is 70%.\n\nMethod Alpha uses Dataset Beta.")
    config = load_config(tmp_path)
    ingest(config, raw, llm_client=FixtureModel())
    return config, hashlib.sha256(raw.read_bytes()).hexdigest()


def test_claim_review_api_and_multiple_evidence_citation_order(tmp_path):
    with patch.dict(os.environ, NO_KEYS):
        config, source_id = seed(tmp_path)
        with TestClient(create_app(tmp_path)) as client:
            claims = client.get("/api/claims").json()
            assert claims["summary"]["supported"] == 2
            assert all(claim["evidence"]["page_number"] == 1 for claim in claims["items"])
            method = client.get("/api/wiki/pages?type=method").json()["items"][0]
            page = client.get(f"/api/wiki/pages/{method['id']}").json()
            assert [evidence["citation_index"] for evidence in page["sources"]] == [1, 2]
            assert len({evidence["claim_id"] for evidence in page["sources"]}) == 2
            claim = claims["items"][0]
            route = f"/api/documents/{source_id}/claims/{claim['claim_id']}/review"
            before = ClaimsService(config).path(source_id).read_bytes()
            rejected = client.post(route, json={"status": "supported", "reason": "fixture correction", "quote": "not in source"})
            assert rejected.status_code == 409
            assert ClaimsService(config).path(source_id).read_bytes() == before
            decision = client.post(route, json={"status": "pending_review", "reason": "requires human semantic review"})
            assert decision.status_code == 200, decision.text
            assert decision.json()["claim"]["verification"]["method"] == "human_review"
            page = client.get(f"/api/wiki/pages/{method['id']}").json()
            assert claim["statement"] not in page["content"]
            assert len(page["sources"]) == 1
            # A generic resolved warning never approves a factual claim.
            assert client.get("/api/claims?status=pending_review").json()["total"] == 1


def test_incremental_index_repairs_missing_fts_and_stale_approval_is_paused(tmp_path):
    with patch.dict(os.environ, NO_KEYS):
        config, source_id = seed(tmp_path)
        db = Database(config.db_path)
        try:
            chunk_ids = [row[0] for row in db.conn.execute("SELECT id FROM chunks ORDER BY id")]
            db.conn.execute("DELETE FROM wiki_fts")
            db.conn.commit()
            stats = db.rebuild(WikiManager(config.wiki_root))
            assert stats["changed"] == 0
            assert chunk_ids == [row[0] for row in db.conn.execute("SELECT id FROM chunks ORDER BY id")]
            assert db.search("Method Alpha")
        finally:
            db.close()
        service = ClaimsService(config)
        claim = service.load(source_id)["claims"][0]
        service.review(source_id, claim["claim_id"], "contradicted", "Fixture: revoke before recompilation")
        stats = rebuild(config)
        assert stats["compiled_pages_paused"] == 2
        db = Database(config.db_path)
        try:
            assert db.search("Method Alpha") == []
            assert db.conn.execute("SELECT COUNT(*) FROM page_evidence").fetchone()[0] == 0
        finally:
            db.close()


def test_indexed_reextraction_requires_explicit_force_and_stays_in_queue(tmp_path):
    with patch.dict(os.environ, NO_KEYS):
        config, source_id = seed(tmp_path)
        with patch("researchwiki.web.app._drain_ingest_queue", return_value=None), patch("researchwiki.web.app.LLMClient") as model:
            model.return_value.enabled = True
            model.return_value.provider = "deepseek"
            model.return_value.model = "mock-no-network"
            with TestClient(create_app(tmp_path)) as client:
                route = f"/api/documents/{source_id}/retry"
                assert client.post(route).status_code == 409
                assert client.post(route + "?force=true&quality_mode=high").status_code == 400
                response = client.post(route + "?force=true&confirm=true&quality_mode=high")
                assert response.status_code == 202, response.text
                assert response.json()["job"]["quality_mode"] == "high"
                assert response.json()["job"]["status"] == "PENDING"
                assert response.json()["job"]["force_refresh"] is True
                model.return_value.generate.assert_not_called()
