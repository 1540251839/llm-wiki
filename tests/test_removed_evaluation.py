"""Retired fixed question bank must not leak through UI, CLI, API or new schemas."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from researchwiki.cli import _parser
from researchwiki.index.database import Database
from researchwiki.web import create_app


def test_fixed_question_bank_cli_removed():
    with pytest.raises(SystemExit):
        _parser().parse_args(["eval"])
    options = _parser().parse_args(["ask", "What methods are in the library?"])
    assert options.command == "ask"


def test_no_evaluation_database_schema_for_new_workspace(tmp_path):
    database = Database(tmp_path / "local.sqlite")
    try:
        rows = database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = {row[0] for row in rows}
        assert "evaluation_runs" not in tables
        assert "evaluation_cases" not in tables
        assert "papers" in tables
    finally:
        database.close()


def test_legacy_evaluation_tables_remain_unmodified(tmp_path):
    path = tmp_path / "older.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE evaluation_runs (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO evaluation_runs (note) VALUES ('history')")
    conn.commit()
    conn.close()
    database = Database(path)
    try:
        record = database.conn.execute("SELECT note FROM evaluation_runs").fetchone()
        assert record["note"] == "history"
    finally:
        database.close()


def test_fixed_question_bank_is_absent_from_web_and_routes(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        homepage = client.get("/").text
        script = client.get("/assets/app.js").text
        assert 'id="view-evaluation"' not in homepage
        assert "固定题库与评估" not in homepage
        assert "loadEvaluation" not in script
        assert "/api/evaluation" not in script
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert all(row["key"] != "evaluation" for row in overview.json()["dataflow"])
        for route in ("/api/evaluation/questions",
                      "/api/evaluation/runs",
                      "/api/evaluation/runs/1"):
            assert client.get(route).status_code == 404
        for route in ("/api/evaluation/run",
                      "/api/evaluation/compare",
                      "/api/evaluation/runs/1/replay"):
            assert client.post(route).status_code == 404
        # The core research features remain accessible.
        assert client.get("/api/wiki/pages").status_code == 200
        assert client.get("/api/search", params={"q": "topic"}).status_code == 200
