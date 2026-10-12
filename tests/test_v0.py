import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from researchwiki.app import do_ask, do_search, ingest, initialize, rebuild
from researchwiki.config import load_config
from researchwiki.llm.client import LLMClient


class FakeLLM:
    enabled = True

    def structured(self, _system, user):
        if "A Second Study" in user:
            return {
                "title": "A Second Study",
                "methods": [{"name": "Evidential Learning"}],
                "topics": ["Label Noise"],
            }
        return {
            "title": "A Study of Label Noise",
            "authors": ["Alice", "Bob"],
            "year": 2024,
            "abstract": "This paper studies label noise in legal retrieval.",
            "methods": [{"name": "Evidential Learning"}],
            "datasets": [{"name": "LeCaRD"}],
            "topics": ["Label Noise", "Legal Case Retrieval"],
            "contributions": ["A robust method"],
            "findings": ["Improves retrieval quality"],
        }


class ResearchWikiV0Test(unittest.TestCase):
    def test_kimi_k26_uses_supported_temperature(self):
        client = LLMClient({
            "provider": "moonshot",
            "model": "kimi-k2.6",
            "api_key": "test-key",
        })
        self.assertEqual(client._effective_temperature(0), 1.0)
        self.assertEqual(client._effective_temperature(0.2), 1.0)

    def test_null_api_key_is_not_treated_as_configured(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}, clear=False):
            client = LLMClient({"provider": "deepseek", "api_key": None})
            self.assertFalse(client.enabled)

    def test_ingest_search_and_protected_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            initialize(config)
            source = root / "paper.txt"
            source.write_text(
                "A Study of Label Noise\n"
                "Authors: Alice; Bob\n"
                "Year: 2024\n"
                "Abstract\nThis paper studies label noise in legal retrieval.\n"
                "Methods: Evidential Learning\n"
                "Datasets: LeCaRD\n"
                "Topics: Label Noise; Legal Case Retrieval\n"
                "Contributions\n- A robust method\n"
                "Results\n- Improves retrieval quality\n",
                encoding="utf-8",
            )
            result = ingest(config, source, llm_client=FakeLLM())
            self.assertEqual(result["status"], "indexed")
            paper_path = Path(result["paper_path"])
            paper_path.write_text(paper_path.read_text(encoding="utf-8").replace(
                "<!-- user-notes:end -->", "My private note\n\n<!-- user-notes:end -->"), encoding="utf-8")
            ingest(config, source, resume=True, llm_client=FakeLLM())
            content = paper_path.read_text(encoding="utf-8")
            self.assertIn("My private note", content)
            self.assertTrue(do_search(config, "label noise"))
            chinese_results = do_search(config, "这篇论文解决了什么问题？使用了哪些方法和数据集？")
            self.assertTrue(chinese_results)
            self.assertTrue(any("A Study of Label Noise" == item["title"] for item in chinese_results))
            cross_language_results = do_search(config, "如何处理噪声")
            self.assertTrue(any("A Study of Label Noise" == item["title"] for item in cross_language_results))
            self.assertIn("Sources", do_ask(config, "What methods handle label noise?"))
            self.assertGreaterEqual(rebuild(config)["pages"], 4)

            second = root / "second.txt"
            second.write_text(
                "A Second Study\n"
                "Abstract\nA follow-up paper studies the same topic.\n"
                "Methods: Evidential Learning\n"
                "Topics: Label Noise\n",
                encoding="utf-8",
            )
            ingest(config, second, llm_client=FakeLLM())
            method_page = root / "wiki" / "methods" / "Evidential-Learning.md"
            method_content = method_page.read_text(encoding="utf-8")
            self.assertIn("[[A Study of Label Noise]]", method_content)
            self.assertIn("[[A Second Study]]", method_content)

    def test_ingest_stops_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            source = root / "paper.txt"
            source.write_text("A Paper\nAbstract\nContent", encoding="utf-8")
            with patch.dict("os.environ", {
                "DEEPSEEK_API_KEY": "",
                "MOONSHOT_API_KEY": "",
                "KIMI_API_KEY": "",
                "OPENAI_API_KEY": "",
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "Deep extraction requires"):
                    ingest(config, source, quality_mode="standard")
            import sqlite3
            connection = sqlite3.connect(root / "data" / "researchwiki.db")
            self.assertEqual(connection.execute("select status from papers").fetchone()[0], "FAILED")

    def test_same_extracted_title_does_not_overwrite_another_paper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(root)
            first = root / "first.txt"
            second = root / "different-source.txt"
            first.write_text("First source about label noise.", encoding="utf-8")
            second.write_text("Different bytes, but a mistaken identical extracted title.", encoding="utf-8")

            result = ingest(config, first, llm_client=FakeLLM())
            paper_path = Path(result["paper_path"])
            original = paper_path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already indexed with the extracted title"):
                ingest(config, second, llm_client=FakeLLM())
            self.assertEqual(paper_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
