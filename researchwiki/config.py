from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .secrets import read_deepseek_key


DEFAULT_CONFIG: Dict[str, Any] = {
    "llm": {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "timeout_seconds": 180,
        "max_retries": 1,
    },
    "wiki": {"root": "./wiki", "language": "zh-CN"},
    "database": {"path": "./data/researchwiki.db"},
    "retrieval": {
        "top_k": 8,
        "max_context_chars": 48000,
        "response_reserve_chars": 6000,
        "max_page_chars": 9000,
        "graph_expansion": True,
        "vector": {
            "enabled": False,
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://api.openai.com/v1",
            "timeout_seconds": 60,
        },
    },
    "ingestion": {
        "quality_mode": "basic",
        "max_attempts": 3,
    },
    "parser": {"backend": "pymupdf", "grobid_url": ""},
}


@dataclass
class Config:
    root: Path
    llm: Dict[str, Any]
    wiki: Dict[str, Any]
    database: Dict[str, Any]
    retrieval: Dict[str, Any]
    ingestion: Dict[str, Any]
    parser: Dict[str, Any]

    @property
    def wiki_root(self) -> Path:
        return self._resolve(self.wiki.get("root", "./wiki"))

    @property
    def wiki_language(self) -> str:
        """Default response language. Original source fields remain unchanged."""
        language = str(self.wiki.get("language") or "zh-CN").strip().lower()
        return "zh-CN" if language.startswith("zh") else "en"


    @property
    def db_path(self) -> Path:
        return self._resolve(self.database.get("path", "./data/researchwiki.db"))

    @property
    def top_k(self) -> int:
        try:
            return int(self.retrieval.get("top_k", 8))
        except (TypeError, ValueError):
            return 8

    @property
    def quality_mode(self) -> str:
        value = str(self.ingestion.get("quality_mode", "basic")).strip().lower()
        return value if value in {"basic", "standard", "high"} else "basic"

    @property
    def max_ingest_attempts(self) -> int:
        try:
            return max(1, min(10, int(self.ingestion.get("max_attempts", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def vector_config(self) -> Dict[str, Any]:
        value = self.retrieval.get("vector") or {}
        return dict(value) if isinstance(value, dict) else {}

    def _resolve(self, value: str) -> Path:
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else self.root / path


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _simple_yaml(text: str) -> Dict[str, Any]:
    """Small YAML subset parser used only when PyYAML is unavailable."""
    data: Dict[str, Any] = {}
    section: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and line.endswith(":"):
            section = {}
            data[line[:-1].strip()] = section
        elif section is not None and ":" in line:
            key, value = line.strip().split(":", 1)
            section[key.strip()] = _scalar(value)
    return data


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def _load_env_file(root_path: Path) -> Dict[str, str]:
    """Read KEY=VALUE pairs from <root>/.env (gitignored local secrets)."""
    env_path = root_path / ".env"
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(root: Union[str, Path] = ".") -> Config:
    root_path = Path(root).expanduser().resolve()
    _load_dotenv(root_path / ".env")
    config_path = root_path / "config.yaml"
    data: Dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        except ImportError:
            data = _simple_yaml(config_path.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_CONFIG)
    for key, value in data.items():
        if isinstance(value, dict):
            merged[key] = dict(DEFAULT_CONFIG.get(key, {}), **value)
        else:
            merged[key] = value
    if not merged["llm"].get("api_key"):
        file_env = _load_env_file(root_path)
        provider = str(merged["llm"].get("provider") or "").lower()
        provider_keys = {
            "deepseek": ("DEEPSEEK_API_KEY",),
            "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
            "openai": ("OPENAI_API_KEY",),
        }
        names = provider_keys.get(provider, (provider.upper() + "_API_KEY",))
        for name in names:
            value = os.environ.get(name) or file_env.get(name)
            if value:
                merged["llm"] = dict(merged["llm"], api_key=value)
                break
    # Explicitly saved local API key takes precedence over inherited shell/.env
    # credentials so the web settings page remains consistent after a restart.
    saved_key = read_deepseek_key(root_path)
    if saved_key:
        was_deepseek = str(merged["llm"].get("provider") or "").lower() == "deepseek"
        merged["llm"] = dict(
            merged["llm"], api_key=saved_key, provider="deepseek",
            base_url="https://api.deepseek.com",
            model=merged["llm"].get("model") if was_deepseek else "deepseek-chat",
        )
    return Config(
        root=root_path,
        llm=merged["llm"],
        wiki=merged["wiki"],
        database=merged["database"],
        retrieval=merged["retrieval"],
        ingestion=merged["ingestion"],
        parser=merged["parser"],
    )
