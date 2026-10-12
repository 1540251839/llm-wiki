import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from researchwiki.app import initialize
from researchwiki.compilation import CompilationService
from researchwiki.config import load_config
from researchwiki.index.database import Database
from researchwiki.models import EntityMention, StructuredPaper
from researchwiki.wiki.manager import NOTE_END, NOTE_START, WikiManager, parse_frontmatter


def add_source(root, config, title, finding):
    text = f"{title}\n\nShared Method is evaluated with Shared Dataset.\n\n{finding}\n\nSmall samples limit generalization."
    source_id = hashlib.sha256(text.encode()).hexdigest()
    raw = root / "raw" / "papers" / (source_id + ".txt")
    raw.write_text(text)
    paper = StructuredPaper(
        title=title, source_hash=source_id, source_path=str(raw),
        abstract="A traceable experiment.", research_problem="How does Shared Method behave?",
        methods=[EntityMention("Shared Method", "A method used in " + title, "Main experiment")],
        datasets=[EntityMention("Shared Dataset", "A common test set")], topics=["Traceability"],
        findings=[finding], limitations=["Small samples limit generalization."],
    )
    cache = root / "data" / "extractions" / (source_id + ".json")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(paper.to_dict()))
    db = Database(config.db_path)
    try:
        db.register_source(source_id, str(raw), str(raw), "text/plain", raw.stat().st_size)
        db.upsert_paper(source_id, title, [], None, str(raw), str(raw), "EXTRACTED")
    finally:
        db.close()
    return paper, raw


def test_compiler_combines_sources_replays_and_preserves_notes(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    first, first_raw = add_source(tmp_path, config, "Study Alpha", "Accuracy is 70 percent.")
    second, second_raw = add_source(tmp_path, config, "Study Beta", "Accuracy is 85 percent.")
    # Begin with the previous product's generated files, including human notes.
    wiki = WikiManager(config.wiki_root)
    wiki.ingest(first, first_raw.relative_to(tmp_path).as_posix())
    wiki.ingest(second, second_raw.relative_to(tmp_path).as_posix())
    method = config.wiki_root / "methods" / "Shared-Method.md"
    method.write_text(method.read_text().replace(NOTE_START + "\n\n", NOTE_START + "\n我的待验证假设。\n"))
    raw_before = {path: path.read_bytes() for path in (first_raw, second_raw)}
    service = CompilationService(config)
    preview = service.inventory()
    assert preview["status"] == "ready"
    assert preview["ready_sources"] == 2
    assert preview["page_count"] == 5
    assert not (config.wiki_root / "manifest.json").exists()
    assert preview["changes"]["created"] == []

    first_run = service.run()
    assert first_run["status"] == "succeeded", first_run
    assert first_run["raw_unchanged"] is True
    content = method.read_text()
    assert "[[Study Alpha]]" in content and "[[Study Beta]]" in content
    assert "Accuracy is 70 percent." in content and "Accuracy is 85 percent." in content
    assert "我的待验证假设。" in content
    manifest_before = (config.wiki_root / "manifest.json").read_bytes()
    second_run = service.run()
    assert second_run["status"] == "succeeded", second_run
    assert second_run["input_digest"] == first_run["input_digest"]
    assert second_run["content_digest"] == first_run["content_digest"]
    assert second_run["changes"]["updated"] == []
    assert len(second_run["changes"]["unchanged"]) == 5
    assert (config.wiki_root / "manifest.json").read_bytes() == manifest_before
    assert all(path.read_bytes() == raw_before[path] for path in raw_before)
    assert len(service.history()) == 2
    assert len(list(wiki.all_pages())) == 5
    for page in service.manifest()["pages"]:
        for span in page["evidence"]:
            source = tmp_path / span["source_path"]
            assert source.read_text()[span["char_start"]:span["char_end"]] == span["evidence_text"]
    # Editing only notes does not change the generated knowledge digest.
    method.write_text(method.read_text().replace("我的待验证假设。", "更新后的人工笔记。"))
    third_run = service.run()
    assert third_run["content_digest"] == first_run["content_digest"]
    assert "更新后的人工笔记。" in method.read_text()


def test_compiler_preserves_manual_collisions_and_blocks_raw_tampering(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    paper, raw = add_source(tmp_path, config, "Study Alpha", "Accuracy is 70 percent.")
    manual = config.wiki_root / "methods" / "Shared-Method.md"
    manual.write_text("# Shared Method\n\n手工维护的独立内容。\n")
    service = CompilationService(config)
    report = service.run()
    assert report["status"] == "succeeded"
    assert manual.read_text() == "# Shared Method\n\n手工维护的独立内容。\n"
    generated = next(config.wiki_root.glob("methods/Shared-Method--*.md"))
    assert parse_frontmatter(generated.read_text())["generated_by"]
    assert any(d["code"] == "preserved_manual_page" for d in report["diagnostics"])
    # A manually edited managed fallback also must never be silently overwritten.
    changed = generated.read_text().replace("## 稳定问题", "人工修订\n\n## 稳定问题")
    generated.write_text(changed)
    failed = service.run()
    assert failed["status"] == "failed"
    assert generated.read_text() == changed
    assert any(d["code"] == "path_conflict" for d in failed["diagnostics"])
    raw.write_text(raw.read_text() + "\nChanged after registration")
    before = {path: path.read_bytes() for path in config.wiki_root.rglob("*") if path.is_file()}
    failed = service.run()
    assert failed["status"] == "failed"
    assert any(d["code"] == "source_integrity" for d in failed["diagnostics"])
    assert all(path.read_bytes() == content for path, content in before.items())


def test_compiler_rolls_back_partial_commit(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    add_source(tmp_path, config, "Study Alpha", "Accuracy is 70 percent.")
    service = CompilationService(config)
    original_write = WikiManager._atomic_write

    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("fixture disk failure")
        return original_write(path, content)

    with patch.object(WikiManager, "_atomic_write", side_effect=fail_manifest):
        result = service.run()
    assert result["status"] == "failed"
    assert "fixture disk failure" in result["error"]
    assert list(config.wiki_root.rglob("*.md")) == []
    assert not (config.wiki_root / "manifest.json").exists()
    assert service.history()[0]["status"] == "failed"


def test_recompile_updates_only_affected_pages_and_reports_stale(tmp_path):
    config = load_config(tmp_path)
    initialize(config)
    paper, raw = add_source(tmp_path, config, "Study Alpha", "Accuracy is 70 percent.")
    service = CompilationService(config)
    baseline = service.run()
    before = service.manifest()
    alpha_path = next(page["path"] for page in before["pages"] if page["type"] == "paper")
    add_source(tmp_path, config, "Study Beta", "Accuracy is 85 percent.")
    added = service.run()
    assert added["status"] == "succeeded"
    assert alpha_path in added["changes"]["unchanged"]
    assert len(added["changes"]["created"]) == 1
    assert len(added["changes"]["updated"]) == 3
    assert added["content_digest"] != baseline["content_digest"]
    db = Database(config.db_path)
    try:
        db.mark_source_deleted(paper.source_hash)
    finally:
        db.close()
    stale = service.run()
    assert stale["status"] == "succeeded"
    assert alpha_path in stale["changes"]["stale"]
    assert (tmp_path / alpha_path).exists()
    assert any(item["code"] == "stale_page" for item in stale["diagnostics"])
