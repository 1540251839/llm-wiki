from researchwiki.models import EntityMention, StructuredPaper
from researchwiki.compilation import CompilationService


def test_wiki_page_types_have_distinct_content(tmp_path):
    from researchwiki.app import initialize
    from researchwiki.config import load_config
    import hashlib
    import json
    from researchwiki.index.database import Database

    config = load_config(tmp_path)
    initialize(config)
    text = "Named Approach operates on DemoSet for legal retrieval. It processes 100 queries."
    digest = hashlib.sha256(text.encode()).hexdigest()
    raw = tmp_path / "raw" / "papers" / (digest + ".txt")
    raw.write_text(text)
    paper = StructuredPaper(
        title="Named Study", authors=["Researcher"], year=2025,
        abstract="An abstract from the source.", keywords=["retrieval", "evidence"],
        experiment_info="DemoSet contains 100 queries.",
        source_hash=digest, source_path=str(raw),
        methods=[EntityMention("Named Approach", "A retrieval algorithm.", "Legal retrieval", "Applies evidence weighting.")],
        datasets=[EntityMention("DemoSet", "Retrieval benchmark.", "Legal retrieval", "100 queries.")],
        topics=["Legal retrieval"], findings=["Improved score by 10%."],
    )
    (tmp_path / "data" / "extractions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "extractions" / (digest + ".json")).write_text(json.dumps(paper.to_dict()))
    db = Database(config.db_path)
    try:
        db.register_source(digest, str(raw), str(raw), "text/plain", raw.stat().st_size)
        db.upsert_paper(digest, paper.title, paper.authors, paper.year, str(raw), str(raw), "EXTRACTED")
    finally:
        db.close()
    svc = CompilationService(config)
    assert svc.run()["status"] == "succeeded"
    manifest = svc.manifest()
    pages = {x["type"]: (tmp_path / x["path"]).read_text() for x in manifest["pages"]}
    assert set(pages) == {"paper", "method", "dataset", "topic"}
    assert "## 关键词" in pages["paper"]
    assert "retrieval" in pages["paper"] and "## 实验信息" in pages["paper"]
    assert "## 方法定义" in pages["method"] and "## 基本原理" in pages["method"]
    assert "Applies evidence weighting." in pages["method"]
    assert "## 统计信息" in pages["dataset"] and "100 queries." in pages["dataset"]
    assert "## 研究方向分类" in pages["topic"] and "## 相关论文" in pages["topic"]
    for kind in ("method", "dataset", "topic"):
        assert "Improved score by 10%." not in pages[kind]
