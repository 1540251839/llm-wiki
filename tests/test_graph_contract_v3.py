import hashlib
from pathlib import Path

from researchwiki.graph import extract_graph
from researchwiki.index.database import Database
from researchwiki.models import EntityMention, Page, StructuredPaper
from researchwiki.wiki.manager import WikiManager


def make_corpus(root: Path, row_offset: int = 0):
    raw_text = "Stable Study uses Shared Method to answer Traceability questions."
    source_id = hashlib.sha256(raw_text.encode()).hexdigest()
    raw = root / "raw" / "papers" / (source_id + ".txt")
    raw.parent.mkdir(parents=True)
    raw.write_text(raw_text)
    wiki = WikiManager(root / "wiki")
    paper = StructuredPaper(title="Stable Study", source_hash=source_id,
                            methods=[EntityMention("Shared Method")], topics=["Traceability"])
    wiki.ingest(paper, raw.relative_to(root).as_posix())
    db = Database(root / "data" / "researchwiki.db")
    db.register_source(source_id, str(raw), str(raw), "text/plain", len(raw_text))
    db.upsert_paper(source_id, paper.title, [], None, str(raw), str(raw), "INDEXED")
    if row_offset:
        db.conn.execute("INSERT INTO wiki_pages(id,title,type,path) VALUES(?,?,?,?)",
                        (row_offset, "Old removed page", "paper", str(root / "wiki" / "removed.md")))
        db.conn.commit()
    db.rebuild(wiki)
    db.enrich_source_evidence(source_id, [Page(1, raw_text)])
    return db, wiki, source_id


def test_graph_identity_survives_workspace_move_and_database_rebuild(tmp_path):
    original, wiki, source_id = make_corpus(tmp_path / "original")
    moved, moved_wiki, _ = make_corpus(tmp_path / "moved", row_offset=800)
    try:
        before = extract_graph(original)
        assert extract_graph(original)["digest"] == before["digest"]
        after = extract_graph(moved, wiki_root=moved_wiki.root)
        assert before["digest"] == after["digest"]
        assert {node["id"] for node in before["nodes"]} == {node["id"] for node in after["nodes"]}
        original_pages = [node for node in before["nodes"] if node.get("page_id")]
        moved_pages = [node for node in after["nodes"] if node.get("page_id")]
        assert {node["page_id"] for node in original_pages}.isdisjoint({node["page_id"] for node in moved_pages})
        assert before["node_types"]["TOPIC"] == 1
        assert before["node_types"]["PAGE"] == 2
        assert before["node_types"]["SOURCE"] == 1
        node_ids = {node["id"] for node in before["nodes"]}
        assert all(edge["source"] in node_ids and edge["target"] in node_ids and edge["source"] != edge["target"] for edge in before["edges"])
        assert {edge["semantic"] for edge in before["edges"]} == {"HAS_TYPE", "HAS_SOURCE", "LINKS_TO"}
        for edge in before["edges"]:
            if edge["semantic"] == "HAS_SOURCE":
                assert edge["doc_id"] == source_id
                assert edge["page_number"] == 1
                assert edge["locator"].startswith("page:1#chars=")
                assert edge["char_end"] > edge["char_start"]
                assert edge["evidence"]
        persisted = [edge for edge in original.list_graph_edges() if edge["semantic"] == "HAS_SOURCE"]
        assert all(edge["locator"] and edge["char_end"] > edge["char_start"] for edge in persisted)
        # Renaming the page changes its stable path identity, as explicitly defined
        # by this graph contract, while a database recreation alone does not.
        before_ids = {node["id"] for node in before["nodes"] if node.get("page_id")}
        path = wiki.root / "methods" / "Shared-Method.md"
        path.rename(path.with_name("Shared-Method-renamed.md"))
        original.rebuild(wiki)
        original.enrich_source_evidence(source_id, [Page(1, "Stable Study uses Shared Method to answer Traceability questions.")])
        renamed = extract_graph(original)
        after_ids = {node["id"] for node in renamed["nodes"] if node.get("page_id")}
        assert len(before_ids - after_ids) == 1
    finally:
        original.close()
        moved.close()


def test_graph_skips_legacy_dangling_and_self_links(tmp_path):
    database, wiki, _ = make_corpus(tmp_path)
    try:
        page_id = int(database.list_wiki_pages()[0]["id"])
        database.conn.execute("INSERT INTO links(source_id,target_id,relation) VALUES(?,?,?)", (page_id, page_id, "related"))
        database.conn.execute("INSERT INTO links(source_id,target_id,relation) VALUES(?,?,?)", (page_id, 999999, "related"))
        database.conn.commit()
        graph = extract_graph(database)
        nodes = {node["id"] for node in graph["nodes"]}
        assert all(edge["source"] in nodes and edge["target"] in nodes and edge["source"] != edge["target"] for edge in graph["edges"])
    finally:
        database.close()
