import os
import stat
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from researchwiki.config import load_config
from researchwiki.secrets import secret_path
from researchwiki.web import create_app

CLEAR_KEYS = {"DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "", "MOONSHOT_API_KEY": ""}


def test_deepseek_key_is_local_private_and_never_returned(tmp_path):
    with patch.dict(os.environ, CLEAR_KEYS):
        with TestClient(create_app(tmp_path)) as client:
            headers = {"X-RW-Config-Action": "1"}
            value = "sk-test-private-0123456"
            assert not client.get("/api/settings").json()["credentials"]["configured"]
            assert client.post("/api/settings/deepseek-key", json={"api_key": value}).status_code == 403
            assert client.post("/api/settings/deepseek-key",
                               json={"api_key": value},
                               headers={**headers, "Origin": "https://not-local.example"}).status_code == 403
            response = client.post("/api/settings/deepseek-key", json={"api_key": value}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["configured"]
            assert value not in response.text
            assert value not in client.get("/api/settings").text
            assert value not in client.get("/api/health").text
            saved = secret_path(tmp_path)
            assert saved.read_text() == value
            if os.name == "posix":
                assert stat.S_IMODE(saved.stat().st_mode) == 0o600
            assert load_config(tmp_path).llm["api_key"] == value
            removed = client.delete("/api/settings/deepseek-key", headers=headers)
            assert removed.status_code == 200
            assert not saved.exists()
            assert not client.get("/api/settings").json()["credentials"]["stored_locally"]


def test_untrusted_origin_cannot_write_key(tmp_path):
    with patch.dict(os.environ, CLEAR_KEYS):
        with TestClient(create_app(tmp_path)) as client:
            headers = {"X-RW-Config-Action": "1", "Origin": "http://evil.example"}
            assert client.post("/api/settings/deepseek-key",
                               headers=headers,
                               json={"api_key": "sk-leak-prevention-key"}).status_code == 403
            assert not secret_path(tmp_path).exists()
