import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from researchwiki.app import ingest, initialize, delete_source
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.web import create_app
from researchwiki.verification import ClaimsService


KEYLESS = {name: "" for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY")}


class IntegrationModel:
    enabled = True
    provider = "fixture"
    model = "fixture-model"
    base_url = "https://fixture.invalid"

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = 0
        self.last_response_meta = {}

    def structured(self, _system, _user):
        self.calls += 1
        self.last_response_meta = {"finish_reason": "stop", "requests": 1,
                                  "usage": {"completion_tokens": 5}, "usage_totals": {"completion_tokens": 12},
                                  "api_key": "NEVER_LOG_THIS", "raw_response": "PRIVATE_OUTPUT"}
        if self.calls == self.fail_on:
            raise RuntimeError("fixture interrupted")
        statement = "Method Alpha preserves source evidence."
        return {"title": "Integration Study", "methods": [{"name": "Method Alpha"}],
                "claims": [{"statement": statement, "claim_type": "method",
                            "entities": [{"type": "method", "name": "Method Alpha"}], "evidence": {"quote": statement}}]}


def setup_job(root):
    config = load_config(root)
    config.ingestion["max_chunk_chars"] = 1200
    config.ingestion["min_chunk_chars"] = 512
    initialize(config)
    source = root / "source.txt"
    source.write_text("Integration Study\n\nMethod Alpha preserves source evidence.\n\n" + "Readable source detail. " * 220)
    source_id = hashlib.sha256(source.read_bytes()).hexdigest()
    db = Database(config.db_path)
    job = db.enqueue_ingest_job(source_id, str(source))
    db.claim_next_ingest_job(job["id"])
    db.close()
    return config, source, source_id, job["id"]


def test_failed_ingest_retains_checkpoint_and_api_progress_resumes(tmp_path):
    with patch.dict(os.environ, KEYLESS):
        config, source, source_id, job_id = setup_job(tmp_path)
        with pytest.raises(RuntimeError, match="fixture interrupted"):
            ingest(config, source, llm_client=IntegrationModel(fail_on=2), job_id=job_id)
        db = Database(config.db_path)
        try:
            failed = db.get_ingest_job(job_id)
            assert failed["status"] == "FAILED"
            assert failed["progress"]["completed"] == 1
            assert failed["progress"]["current"] == 2
            assert failed["progress"]["model_totals"]["extraction"]["requests"] == 2
            assert "EXTRACT" in failed["stage_timings"]
            assert db.retry_ingest_job(job_id)["force_refresh"] is False
            db.claim_next_ingest_job(job_id)
        finally:
            db.close()
        model = IntegrationModel()
        result = ingest(config, source, resume=True, llm_client=model, job_id=job_id)
        assert result["model_totals"]["extraction"]["cached"] == 1
        assert result["model_totals"]["extraction"]["requests"] == model.calls
        assert result["model_totals"]["extraction"]["completion_tokens"] == 12 * model.calls
        assert result["stage_timings"]["PARSE"] >= 0
        with TestClient(create_app(tmp_path)) as client:
            detail = client.get(f"/api/documents/{source_id}").json()
            job = detail["job"]
            assert job["status"] == "SUCCEEDED"
            assert job["progress"]["cached"] == 1
            assert job["progress"]["completed"] == job["progress"]["total"]
            assert job["stage_timings"]["total_seconds"] >= 0
        log = (tmp_path / "logs" / "researchwiki.log").read_text()
        assert "NEVER_LOG_THIS" not in log and "PRIVATE_OUTPUT" not in log


def test_recoverable_delete_preserves_both_jsons_and_checkpoint_tree(tmp_path):
    with patch.dict(os.environ, KEYLESS):
        config, source, source_id, job_id = setup_job(tmp_path)
        ingest(config, source, llm_client=IntegrationModel(), job_id=job_id)
        before = {name: (tmp_path / "data" / name / (source_id + ".json")).read_bytes()
                  for name in ("claims", "extractions")}
        checkpoint = tmp_path / "data" / "ingest_checkpoints" / source_id
        files = len(list(checkpoint.glob("*.json")))
        deleted = delete_source(config, source_id)
        trash = Path(deleted["recoverable_from"]) / "files" / "data"
        assert all((trash / name / (source_id + ".json")).read_bytes() == content for name, content in before.items())
        assert len(list((trash / "ingest_checkpoints" / source_id).glob("*.json"))) == files
        assert not checkpoint.exists()


def test_resuming_identical_facts_preserves_review_but_explicit_fresh_invalidates(tmp_path):
    with patch.dict(os.environ, KEYLESS):
        config, source, source_id, job_id = setup_job(tmp_path)
        ingest(config, source, llm_client=IntegrationModel(), job_id=job_id)
        service = ClaimsService(config)
        claim = service.load(source_id)["claims"][0]
        service.review(source_id, claim["claim_id"], "pending_review", "Fixture: manual semantic review pending")
        resumed = IntegrationModel()
        ingest(config, source, resume=True, llm_client=resumed)
        assert resumed.calls == 0
        verification = service.load(source_id)["claims"][0]["verification"]
        assert verification["method"] == "human_review"
        assert verification["status"] == "pending_review"
        fresh = IntegrationModel()
        ingest(config, source, resume=True, force_refresh=True, llm_client=fresh)
        assert fresh.calls > 0
        assert service.load(source_id)["claims"][0]["verification"]["method"] == "deterministic_extractive"
