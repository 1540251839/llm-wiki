"""Chinese-first display, original-language preservation and legacy-cache compatibility."""
import hashlib
import json

from fastapi.testclient import TestClient

from researchwiki.app import initialize, rebuild
from researchwiki.compilation import CompilationService
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.models import EntityMention, StructuredPaper
from researchwiki.web.app import create_app
from researchwiki.wiki.manager import parse_frontmatter


def test_default_language_and_legacy_cache(tmp_path):
    config = load_config(tmp_path)
    assert config.wiki_language == "zh-CN"
    old = StructuredPaper.from_dict({
        "title": "Old Paper", "abstract": "Original abstract.",
        "methods": [{"name": "Evidential Learning"}],
        "topics": ["Evidential learning"],
    })
    assert old.abstract == "Original abstract."
    assert old.abstract_zh == ""
    assert old.methods[0].name_zh == ""
    assert old.topic_labels_zh == {}


def test_bilingual_wiki_keeps_stable_original_names_and_searches_chinese(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    raw_text = (
        "An Evidential Study\n\nAbstract: This study investigates evidence-based "
        "uncertainty estimation for legal case retrieval.\n\n"
        "Keywords: evidential learning, legal retrieval\n\n"
        "This work evaluates a named method using the LeCaRD dataset."
    )
    digest = hashlib.sha256(raw_text.encode()).hexdigest()
    source_path = tmp_path / "raw" / "papers" / (digest + ".txt")
    source_path.write_text(raw_text, encoding="utf-8")
    paper = StructuredPaper(
        title="An Evidential Study",
        authors=["Author A"], year=2025,
        abstract="This study investigates evidence-based uncertainty estimation for legal case retrieval.",
        abstract_zh="本文研究面向法律类案检索的证据不确定性估计。",
        keywords=["evidential learning", "legal retrieval"],
        methods=[EntityMention(name="Evidence Method", name_zh="证据方法", description="利用证据建模不确定性。")],
        datasets=[EntityMention(name="LeCaRD", description="法律类案检索基准数据集。")],
        topics=["Evidential learning"],
        topic_labels_zh={"Evidential learning": "证据学习"},
        source_hash=digest, source_path=str(source_path),
    )
    (tmp_path / "data" / "extractions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "extractions" / (digest + ".json")).write_text(
        json.dumps(paper.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    db = Database(config.db_path)
    try:
        db.register_source(digest, str(source_path), str(source_path), "text/plain", source_path.stat().st_size)
        db.upsert_paper(digest, paper.title, paper.authors, paper.year,
                        str(source_path), str(source_path), "EXTRACTED")
    finally:
        db.close()

    compiler = CompilationService(config)
    assert compiler.run()["status"] == "succeeded"
    pages = compiler.manifest()["pages"]
    paper_meta = next(p for p in pages if p["type"] == "paper")
    paper_text = (tmp_path / paper_meta["path"]).read_text(encoding="utf-8")
    assert paper_text.index("## 中文摘要") < paper_text.index("## 原文摘要（Abstract）")
    assert paper.abstract_zh in paper_text
    assert paper.abstract in paper_text
    assert "evidential learning" in paper_text
    assert raw_text == source_path.read_text(encoding="utf-8")

    topic = next(p for p in pages if p["type"] == "topic")
    assert topic["title"] == "Evidential learning"
    assert topic["display_title"] == "证据学习（Evidential learning）"
    topic_text = (tmp_path / topic["path"]).read_text(encoding="utf-8")
    assert parse_frontmatter(topic_text)["title"] == "Evidential learning"
    assert "# 证据学习（Evidential learning）" in topic_text
    assert "## 来源证据" in topic_text

    rebuild(config)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/wiki/pages", params={"q": "证据学习", "type": "topic"})
        assert response.status_code == 200
        match = response.json()["items"]
        assert len(match) == 1
        assert match[0]["title"] == "Evidential learning"
        assert match[0]["display_title"] == topic["display_title"]
        detail = client.get(f"/api/wiki/pages/{match[0]['id']}").json()
        assert detail["display_title"] == topic["display_title"]


def test_basic_cache_does_not_invent_a_chinese_translation(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    original = "Offline Study\nAbstract: An original English abstract without a translation.\n"
    digest = hashlib.sha256(original.encode()).hexdigest()
    source = tmp_path / "raw" / "papers" / (digest + ".txt")
    source.write_text(original, encoding="utf-8")
    paper = StructuredPaper(title="Offline Study", abstract="An original English abstract without a translation.",
                            source_hash=digest)
    (tmp_path / "data" / "extractions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "extractions" / (digest + ".json")).write_text(
        json.dumps(paper.to_dict()), encoding="utf-8"
    )
    db = Database(config.db_path)
    try:
        db.register_source(digest, str(source), str(source), "text/plain", source.stat().st_size)
        db.upsert_paper(digest, paper.title, [], None, str(source), str(source), "EXTRACTED")
    finally:
        db.close()
    compiler = CompilationService(config)
    assert compiler.run()["status"] == "succeeded"
    meta = next(p for p in compiler.manifest()["pages"] if p["type"] == "paper")
    content = (tmp_path / meta["path"]).read_text(encoding="utf-8")
    assert "暂无中文摘要" in content
    assert paper.abstract in content
