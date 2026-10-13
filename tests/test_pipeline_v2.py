import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchwiki.app import delete_source, do_search, ingest, initialize
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.graph import extract_graph
from researchwiki.qa.context import assemble_context
from researchwiki.retrieval.vector import sync_vector_index, vector_search
from researchwiki.wiki.manager import parse_frontmatter


class PipelineLLM:
    enabled = True

    def __init__(self):
        self.analysis_calls = 0

    def generate(self, _system, _user, temperature=0.1):
        self.analysis_calls += 1
        return "# Analysis\n\nShared Method is the central method; title and claims are explicit."

    def structured(self, _system, user):
        second = "SECOND_UNIQUE_MARKER" in user
        return {
            "title": "Second Traceable Study" if second else "First Traceable Study",
            "authors": ["Researcher"],
            "year": 2026,
            "abstract": "Traceable evidence for a local research wiki.",
            "methods": [{"name": "Shared Method", "role": "Links claims to evidence."}],
            "datasets": [{"name": "Shared Dataset"}],
            "topics": ["Traceability"],
            "findings": ["Provenance improves auditability."],
            "limitations": [],
            "claims": [{"statement": "Shared Method links traceable evidence to Shared Dataset.",
                        "claim_type": "method", "entities": [{"type": "method", "name": "Shared Method"},
                                                                  {"type": "dataset", "name": "Shared Dataset"}],
                        "evidence": {"quote": "Shared Method links traceable evidence to Shared Dataset."}}],
        }


class ResearchWikiPipelineV2Test(unittest.TestCase):
    def test_provenance_graph_context_and_shared_entity_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("FIRST_UNIQUE_MARKER\nShared Method links traceable evidence to Shared Dataset.", encoding="utf-8")
            second.write_text("SECOND_UNIQUE_MARKER\nShared Method links traceable evidence to Shared Dataset.", encoding="utf-8")

            first_result = ingest(config, first, llm_client=PipelineLLM())
            second_result = ingest(config, second, llm_client=PipelineLLM())
            # IDs are content hashes; read them from structured frontmatter to avoid
            # reimplementing the production hash routine in the test.
            first_front = parse_frontmatter(Path(first_result["paper_path"]).read_text(encoding="utf-8"))
            second_front = parse_frontmatter(Path(second_result["paper_path"]).read_text(encoding="utf-8"))
            first_id = str(first_front["source_hash"])
            second_id = str(second_front["source_hash"])

            database = Database(config.db_path)
            try:
                method = next(page for page in database.list_wiki_pages(page_type="method") if page["title"] == "Shared Method")
                source_ids = {value["source_id"] for value in database.get_page_sources(int(method["id"]))}
                self.assertEqual(source_ids, {first_id, second_id})
                evidence = database.get_page_sources(int(method["id"]))
                self.assertTrue(all(value["page_number"] == 1 for value in evidence))
                self.assertTrue(all(value["locator"].startswith("page:1#chars=") for value in evidence))
                self.assertTrue(all(value["evidence_text"] for value in evidence))
                related = database.related_pages(int(method["id"]))
                self.assertTrue(any(value["title"] == "First Traceable Study" for value in related))
                self.assertGreaterEqual(len(database.list_reviews()), 2)
                graph = extract_graph(database)
                first_digest = graph["digest"]
                self.assertGreater(graph["stats"]["nodes"], 0)
                self.assertGreater(graph["stats"]["edges"], 0)
                self.assertIn("PAGE", graph["node_types"])
                self.assertIn("SOURCE", graph["node_types"])
                self.assertIn("HAS_SOURCE", graph["edge_semantics"])
                self.assertEqual(extract_graph(database)["digest"], first_digest)
                node_ids = {value["id"] for value in database.list_graph_nodes()}
                self.assertTrue(all(edge["source"] in node_ids and edge["target"] in node_ids for edge in database.list_graph_edges()))
            finally:
                database.close()

            results = do_search(config, "traceable evidence")
            self.assertTrue(results)
            self.assertTrue(any(item.get("sources") for item in results))
            context, included, stats = assemble_context(
                "How is evidence traced?",
                results,
                {"max_context_chars": 8000, "response_reserve_chars": 2000, "max_page_chars": 1500},
            )
            self.assertTrue(context)
            self.assertTrue(included)
            self.assertLessEqual(stats["used_chars"], stats["page_budget_chars"])

            deletion = delete_source(config, first_id)
            self.assertTrue(Path(deletion["recoverable_from"]).exists())
            self.assertTrue((Path(deletion["recoverable_from"]) / "manifest.json").exists())
            self.assertFalse(Path(first_result["paper_path"]).exists())
            method_path = root / "wiki" / "methods" / "Shared-Method.md"
            self.assertTrue(method_path.exists())
            method_content = method_path.read_text(encoding="utf-8")
            self.assertNotIn("[[First Traceable Study]]", method_content)
            self.assertIn("[[Second Traceable Study]]", method_content)
            method_front = parse_frontmatter(method_content)
            self.assertEqual(method_front["sources"], [second_id])

    def test_persistent_job_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            initialize(config)
            source = root / "queued.txt"
            source.write_text("queued", encoding="utf-8")
            database = Database(config.db_path)
            try:
                database.upsert_paper("source-id", "Queued", [], None, str(source), "", "UPLOADED")
                database.register_source("source-id", str(source), "", "text/plain", source.stat().st_size)
                job = database.enqueue_ingest_job("source-id", str(source), quality_mode="high", max_attempts=2)
                claimed = database.claim_next_ingest_job()
                self.assertEqual(claimed["id"], job["id"])
                self.assertEqual(claimed["status"], "RUNNING")
                self.assertEqual(database.fail_interrupted_jobs(), 1)
                failed = database.get_ingest_job(int(job["id"]))
                self.assertEqual(failed["status"], "FAILED")
                retried = database.retry_ingest_job(int(job["id"]), "standard")
                self.assertEqual(retried["status"], "PENDING")
                self.assertEqual(retried["quality_mode"], "standard")
            finally:
                database.close()

    def test_high_quality_analysis_and_optional_vector_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            source = root / "paper.txt"
            source.write_text("FIRST_UNIQUE_MARKER traceable evidence", encoding="utf-8")
            fake = PipelineLLM()
            result = ingest(config, source, llm_client=fake, quality_mode="high")
            self.assertEqual(result["quality_mode"], "high")
            self.assertEqual(fake.analysis_calls, 1)
            source_id = str(parse_frontmatter(Path(result["paper_path"]).read_text(encoding="utf-8"))["source_hash"])
            self.assertTrue((root / "data" / "analyses" / (source_id + ".md")).exists())

            vector_config = {
                "enabled": True,
                "provider": "openai",
                "model": "test-embedding",
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
            }

            def fake_embed(_client, texts):
                return [[1.0, 0.0] if "trace" in text.casefold() else [0.0, 1.0] for text in texts]

            database = Database(config.db_path)
            try:
                with patch("researchwiki.retrieval.vector.EmbeddingClient.embed", fake_embed):
                    stats = sync_vector_index(database, vector_config)
                    matches = vector_search(database, "trace query", vector_config, 5)
                self.assertTrue(stats["enabled"])
                self.assertGreater(stats["indexed"], 0)
                self.assertTrue(matches)
                self.assertEqual(matches[0]["retrieval"], "vector")
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
