import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from researchwiki.app import ingest
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.web import create_app


class FakeLLM:
    enabled = True

    def structured(self, _system, _user):
        return {
            "title": "Traceable Knowledge Systems",
            "authors": ["Ada Researcher"],
            "year": 2026,
            "abstract": "A study of traceable knowledge layers for research documents.",
            "methods": [{"name": "Evidence Linking", "role": "Connect answers to sources."}],
            "datasets": [{"name": "PaperBench"}],
            "topics": ["Traceable Knowledge"],
            "contributions": ["A provenance-aware pipeline."],
            "findings": ["Source links make answers auditable."],
        }


NO_KEYS = {
    "DEEPSEEK_API_KEY": "",
    "MOONSHOT_API_KEY": "",
    "KIMI_API_KEY": "",
    "OPENAI_API_KEY": "",
}


class ResearchWikiWebTest(unittest.TestCase):
    @staticmethod
    def _wait_status(client: TestClient, paper_id: str, expected: str, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        document = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/documents/{paper_id}")
            if response.status_code == 200:
                document = response.json()
                if document["status"] == expected:
                    return document
            time.sleep(0.02)
        raise AssertionError(f"document did not reach {expected}: {document}")

    def test_health_static_assets_and_failed_upload_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, NO_KEYS, clear=False):
            root = Path(directory)
            with TestClient(create_app(root)) as client:
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["llm"]["provider"], "deepseek")
                self.assertFalse(health.json()["llm"]["configured"])

                self.assertIn("ResearchWiki", client.get("/").text)
                self.assertEqual(client.get("/assets/app.js").status_code, 200)
                self.assertEqual(client.get("/api/overview").json()["documents"], 0)

                payload = b"A paper\nAbstract\nEvidence-aware knowledge management."
                response = client.post(
                    "/api/documents",
                    files={"file": ("sample.txt", payload, "text/plain")},
                )
                self.assertEqual(response.status_code, 202)
                paper_id = hashlib.sha256(payload).hexdigest()

                document = self._wait_status(client, paper_id, "FAILED")
                self.assertEqual(document["status"], "FAILED")
                self.assertEqual(document["quality"]["level"], "error")
                self.assertTrue(document["retryable"])
                self.assertTrue(document["raw_ref"].startswith("raw/papers/"))
                self.assertFalse(document["raw_ref"].startswith("/"))
                self.assertIn("API key", document["error_message"])

                duplicate = client.post(
                    "/api/documents",
                    files={"file": ("sample.txt", payload, "text/plain")},
                )
                self.assertEqual(duplicate.json()["action"], "retry_required")

                retry = client.post(f"/api/documents/{paper_id}/retry")
                self.assertEqual(retry.status_code, 202)
                self._wait_status(client, paper_id, "FAILED")

                bad = client.post(
                    "/api/documents",
                    files={"file": ("sample.exe", b"not a document", "application/octet-stream")},
                )
                self.assertEqual(bad.status_code, 415)

    def test_indexed_document_wiki_search_and_offline_qa(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, NO_KEYS, clear=False):
            root = Path(directory)
            source = root / "traceable.txt"
            source.write_text("Traceable Knowledge Systems\nAbstract\nSource links make answers auditable.", encoding="utf-8")
            ingest(load_config(root), source, llm_client=FakeLLM())

            with TestClient(create_app(root)) as client:
                documents = client.get("/api/documents").json()
                self.assertEqual(documents["total"], 1)
                self.assertEqual(documents["items"][0]["status"], "INDEXED")

                pages = client.get("/api/wiki/pages").json()
                self.assertGreaterEqual(pages["total"], 4)
                self.assertTrue(all(not item["path"].startswith("/") for item in pages["items"]))
                page = client.get(f"/api/wiki/pages/{pages['items'][0]['id']}").json()
                self.assertTrue(page["sources"])
                self.assertIn("related", page)
                self.assertNotIn(str(root), str(page["sources"]))

                graph = client.get("/api/graph").json()
                self.assertGreaterEqual(len(graph["nodes"]), 4)
                self.assertGreater(client.get("/api/graph/nodes", params={"type": "PAGE"}).json()["total"], 0)
                self.assertGreater(client.get("/api/graph/edges", params={"semantic": "HAS_SOURCE"}).json()["total"], 0)
                rebuilt_graph = client.post("/api/graph/extract")
                self.assertEqual(rebuilt_graph.status_code, 200)
                self.assertTrue(rebuilt_graph.json()["ok"])

                search = client.get("/api/search", params={"q": "traceable knowledge"})
                self.assertEqual(search.status_code, 200)
                self.assertGreater(search.json()["total"], 0)

                answer = client.post("/api/ask", json={"question": "What makes the knowledge traceable?"})
                self.assertEqual(answer.status_code, 200)
                self.assertEqual(answer.json()["mode"], "local_evidence")
                self.assertTrue(answer.json()["sources"])
                self.assertNotIn(str(root), answer.json()["answer"])
                provenance = answer.json()["sources"][0]["provenance"]
                self.assertTrue(provenance[0]["locator"])
                self.assertTrue(provenance[0]["evidence_text"])

                streamed = client.post("/api/ask/stream", json={"question": "What makes the knowledge traceable?"})
                self.assertEqual(streamed.status_code, 200)
                self.assertIn("event: status", streamed.text)
                self.assertIn("event: result", streamed.text)

                no_evidence = client.post("/api/ask", json={"question": "Unrelated quantum volcano question"})
                self.assertEqual(no_evidence.status_code, 200)
                self.assertEqual(no_evidence.json()["mode"], "no_evidence")

                reviews = client.get("/api/reviews").json()
                self.assertGreaterEqual(reviews["total"], 1)
                resolved = client.post(f"/api/reviews/{reviews['items'][0]['id']}/resolve")
                self.assertEqual(resolved.status_code, 200)

                settings = client.get("/api/settings").json()
                self.assertFalse(settings["llm"]["configured"])
                self.assertNotIn("api_key", str(settings))
                self.assertEqual(client.post("/api/settings/test").status_code, 409)

                evaluation = client.post("/api/evaluation/run", params={"profile": "optimized"})
                self.assertEqual(evaluation.status_code, 200)
                self.assertGreaterEqual(evaluation.json()["question_count"], 16)
                self.assertIn("source_locator_coverage", evaluation.json()["metrics"])
                self.assertEqual(client.get("/api/evaluation/runs").json()["total"], 1)

                self.assertEqual(client.delete(f"/api/documents/{documents['items'][0]['id']}").status_code, 400)
                deleted = client.delete(
                    f"/api/documents/{documents['items'][0]['id']}",
                    params={"confirm": "true"},
                )
                self.assertEqual(deleted.status_code, 200)
                self.assertTrue(deleted.json()["recoverable_from"].startswith("data/trash/"))
                self.assertEqual(client.get("/api/documents").json()["total"], 0)

    def test_interrupted_processing_is_recovered_as_retryable_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, NO_KEYS, clear=False):
            root = Path(directory)
            config = load_config(root)
            database = Database(config.db_path)
            try:
                database.upsert_paper("interrupted-id", "Interrupted", [], None, "", "", "EXTRACTING")
            finally:
                database.close()

            with TestClient(create_app(root)) as client:
                document = client.get("/api/documents/interrupted-id").json()
                self.assertEqual(document["status"], "FAILED")
                self.assertIn("PROCESS_INTERRUPTED", document["error_message"])


if __name__ == "__main__":
    unittest.main()
