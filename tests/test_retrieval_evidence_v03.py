import unittest
from unittest.mock import Mock, patch

from researchwiki.qa.engine import answer_with_metadata
from researchwiki.retrieval.search import reciprocal_rank_fusion, search


def candidate(page_id, title, content):
    return {"page_id": page_id, "title": title, "type": "topic", "path": "wiki/" + title + ".md", "content": content}


class HybridRetrievalEvidenceTest(unittest.TestCase):
    def test_semantic_candidate_survives_fusion_and_answer_admission(self):
        lexical = [candidate(index, "regularization " + str(index), "Keyword regularization.") for index in range(1, 9)]
        semantic = candidate(99, "Evidence learning", "The technique is designed to tolerate corrupted annotations.")
        database = Mock()
        database.search.return_value = []
        database.get_page_sources.return_value = []
        with patch("researchwiki.retrieval.search.retrieve_evidence", return_value=lexical), patch("researchwiki.retrieval.search.vector_search", return_value=[semantic]):
            results = search(database, "regularization", top_k=8, graph_expansion=False)
        self.assertIn(99, [item["page_id"] for item in results])
        source = next(item for item in results if item["page_id"] == 99)
        self.assertEqual(source["retrieval_signals"], ["vector"])
        answer = answer_with_metadata("regularization", [source])
        self.assertEqual(answer["mode"], "local_evidence")
        self.assertEqual(answer["sources"][0]["page_id"], 99)
        self.assertIn("corrupted annotations", answer["answer"])

    def test_signal_duplicates_do_not_manufacture_extra_rrf_votes(self):
        first = candidate(1, "First", "A finding.")
        second = candidate(2, "Second", "Another finding.")
        fused = reciprocal_rank_fusion([("fts", [first, first, second], 1.0), ("vector", [second], 1.0)])
        self.assertEqual([item["page_id"] for item in fused], [2, 1])
        self.assertEqual(fused[0]["signal_ranks"], {"fts": 2, "vector": 1})
        self.assertAlmostEqual(fused[1]["hybrid_score"], 1 / 61)

    def test_qa_preserves_fused_order_instead_of_lexical_resorting(self):
        semantic = {**candidate(1, "Annotations", "Corrupted annotations can affect learning."), "retrieval_signals": ["vector"]}
        lexical = {**candidate(2, "Noise noise noise", "Noise is the keyword."), "retrieval_signals": ["fts"]}
        answer = answer_with_metadata("noise", [semantic, lexical])
        self.assertEqual([source["page_id"] for source in answer["sources"]], [1, 2])

    def test_empty_query_never_expands_graph_or_calls_embedding(self):
        database = Mock()
        with patch("researchwiki.retrieval.search.vector_search") as vectors:
            self.assertEqual(search(database, "  "), [])
            vectors.assert_not_called()
        database.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
