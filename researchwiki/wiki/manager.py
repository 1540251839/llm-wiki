from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import yaml

from ..models import EntityMention, StructuredPaper


NOTE_START = "<!-- user-notes:start -->"
NOTE_END = "<!-- user-notes:end -->"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_name(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    value = re.sub(r"[\s_-]+", "-", value)
    return value[:120] or "untitled"


def _yaml_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(_yaml_value(value) for value in dict.fromkeys(values) if value) + "]"


class WikiManager:
    TYPES = ("papers", "methods", "datasets", "topics", "synthesis")

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)

    def page_path(self, page_type: str, title: str) -> Path:
        return self.root / (page_type + "s" if not page_type.endswith("s") else page_type) / (slugify(title) + ".md")

    def ensure_dirs(self) -> None:
        for directory in self.TYPES:
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def all_pages(self) -> Iterable[Path]:
        if not self.root.exists():
            return []
        # Compiler navigation/history are not knowledge pages or retrieval evidence.
        return (
            path for path in self.root.rglob("*.md")
            if not (
                path.parent == self.root
                and path.name in {"index.md", "log.md"}
                and parse_frontmatter(path.read_text(encoding="utf-8")).get("compiler_auxiliary")
            )
        )

    def find_page(self, page_type: str, title: str) -> Optional[Path]:
        wanted = normalize_name(title)
        directory = self.root / (page_type + "s" if not page_type.endswith("s") else page_type)
        if not directory.exists():
            return None
        for path in directory.glob("*.md"):
            if normalize_name(path.stem) == wanted:
                return path
            front = parse_frontmatter(path.read_text(encoding="utf-8"))
            candidate = str(front.get("title") or front.get("name") or "")
            if candidate and normalize_name(candidate) == wanted:
                return path
        return None

    def _user_notes(self, old: str) -> str:
        if NOTE_START in old and NOTE_END in old:
            return old[old.index(NOTE_START):old.index(NOTE_END) + len(NOTE_END)]
        return NOTE_START + "\n\n" + NOTE_END

    def _generated_part(self, old: str) -> str:
        if NOTE_START in old:
            old = old.split(NOTE_START, 1)[0]
        marker = "\n## 我的笔记"
        if marker in old:
            old = old.split(marker, 1)[0]
        return old.rstrip()

    def _compose(self, path: Path, generated: str) -> str:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        notes = self._user_notes(old)
        if NOTE_START in generated:
            generated = generated.split(NOTE_START, 1)[0].rstrip()
        return generated.rstrip() + "\n\n## 我的笔记\n\n" + notes + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _commit(self, planned: Dict[Path, str]) -> None:
        originals: Dict[Path, Optional[str]] = {
            path: path.read_text(encoding="utf-8") if path.exists() else None
            for path in planned
        }
        committed: List[Path] = []
        try:
            for path, content in planned.items():
                if originals[path] == content:
                    continue
                self._atomic_write(path, content)
                committed.append(path)
        except Exception:
            for path in reversed(committed):
                original = originals[path]
                if original is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    self._atomic_write(path, original)
            raise

    def _write(self, path: Path, generated: str) -> bool:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        content = self._compose(path, generated)
        if old == content:
            return False
        self._atomic_write(path, content)
        return True

    def write_paper(self, paper: StructuredPaper, raw_path: str) -> Path:
        path = self.find_page("paper", paper.title) or self.page_path("paper", paper.title)
        created = now_iso()
        methods = "\n".join("- [[" + x.name + "]]" for x in paper.methods) or "- None identified"
        datasets = "\n".join("- [[" + x.name + "]]" for x in paper.datasets) or "- None identified"
        topics = "\n".join("- [[" + x + "]]" for x in paper.topics) or "- None identified"
        metrics = "\n".join("- " + x for x in paper.metrics) or "- None identified"
        contributions = "\n".join("- " + x for x in paper.contributions) or "- None identified"
        findings = "\n".join("- " + x for x in paper.findings) or "- None identified"
        limitations = "\n".join("- " + x for x in paper.limitations) or "- None identified"
        author_text = ", ".join(paper.authors) or "Unknown"
        generated = f"""---
type: paper
title: {_yaml_value(paper.title)}
authors: {_yaml_value(author_text)}
year: {paper.year if paper.year is not None else "null"}
source: {_yaml_value(raw_path)}
source_hash: {_yaml_value(paper.source_hash)}
sources: {_yaml_list([paper.source_hash])}
updated_at: {_yaml_value(created)}
---

# {paper.title}

## 基本信息

作者：{author_text}

年份：{paper.year or "Unknown"}

来源：`{raw_path}`

## 摘要

{paper.abstract or "未识别到摘要。"}

## 研究问题

{paper.research_problem or "未识别到研究问题。"}

## 核心方法

{paper.methodology or "未识别到方法概述。"}

## 主要贡献

{contributions}

## 方法

{methods}

## 数据集

{datasets}

## 评价指标

{metrics}

## 主要实验结论

{findings}

## 局限性

{limitations}

## 相关研究主题

{topics}

## 来源

原始文件：`{raw_path}`
"""
        self._write(path, generated)
        return path

    @staticmethod
    def _with_source(content: str, source_hash: str) -> str:
        if not source_hash:
            return content
        front = parse_frontmatter(content)
        current = front.get("sources") or []
        if isinstance(current, str):
            sources = [current]
        elif isinstance(current, list):
            sources = [str(value) for value in current]
        else:
            sources = []
        if source_hash not in sources:
            sources.append(source_hash)
        return replace_frontmatter(content, {"sources": list(dict.fromkeys(sources))})

    def _entity_generated(
        self,
        kind: str,
        name: str,
        paper: StructuredPaper,
        description: str = "",
        role: str = "",
        existing: str = "",
    ) -> str:
        page_type = kind[:-1] if kind.endswith("s") else kind
        paper_link = "[[" + paper.title + "]]"
        if kind == "methods":
            header = f"""---
type: method
name: {_yaml_value(name)}
sources: {_yaml_list([paper.source_hash])}
updated_at: {_yaml_value(now_iso())}
---

# {name}

## 方法简介

{description or "待从更多论文中补充。"}

## 在不同论文中的使用

"""
            entry = "### " + paper_link + "\n\n用途：" + (role or "论文中使用该方法。") + "\n\n"
        elif kind == "datasets":
            header = f"""---
type: dataset
name: {_yaml_value(name)}
sources: {_yaml_list([paper.source_hash])}
updated_at: {_yaml_value(now_iso())}
---

# {name}

## 基本信息

{description or "待从更多论文中补充。"}

## 使用该数据集的论文

"""
            entry = f"- {paper_link}"
        else:
            header = f"""---
type: topic
title: {_yaml_value(name)}
sources: {_yaml_list([paper.source_hash])}
updated_at: {_yaml_value(now_iso())}
---

# {name}

## 定义与核心问题

{description or "该主题由相关论文持续补充。"}

## 代表论文

"""
            entry = f"- {paper_link}"
        body = self._generated_part(existing)
        if not body.strip():
            body = header
        else:
            body = self._with_source(body, paper.source_hash)
        if paper_link not in body:
            body = body.rstrip() + "\n" + entry + "\n"
        return body

    def _entity_page(self, kind: str, name: str, paper: StructuredPaper, description: str = "", role: str = "") -> Path:
        page_type = kind[:-1] if kind.endswith("s") else kind
        path = self.find_page(page_type, name) or self.page_path(page_type, name)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        body = self._entity_generated(kind, name, paper, description, role, old)
        self._write(path, body)
        return path

    def update_entities(self, paper: StructuredPaper) -> List[Path]:
        paths: List[Path] = []
        for entity in paper.methods:
            paths.append(self._entity_page("methods", entity.name, paper, entity.description, entity.role))
        for entity in paper.datasets:
            paths.append(self._entity_page("datasets", entity.name, paper, entity.description, entity.role))
        for topic in paper.topics:
            paths.append(self._entity_page("topics", topic, paper))
        return paths

    def _ingest_direct(self, paper: StructuredPaper, raw_path: str) -> Tuple[Path, List[Path]]:
        self.ensure_dirs()
        paper_path = self.write_paper(paper, raw_path)
        entity_paths = self.update_entities(paper)
        return paper_path, entity_paths

    def ingest(self, paper: StructuredPaper, raw_path: str) -> Tuple[Path, List[Path]]:
        """Legacy formatter, not a verifier or production factual ingestion path.

        Production uses the compilation service and independently verified Claims.

        Individual file replacement is atomic. If any replacement fails, already
        replaced files are restored from their pre-commit contents.
        """
        self.root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".researchwiki-stage-", dir=str(self.root.parent)) as directory:
            stage_root = Path(directory) / "wiki"
            if self.root.exists():
                shutil.copytree(self.root, stage_root, dirs_exist_ok=True)
            stage = WikiManager(stage_root)
            staged_paper, staged_entities = stage._ingest_direct(paper, raw_path)
            staged_paths = [staged_paper, *staged_entities]
            planned: Dict[Path, str] = {}
            output_paths: List[Path] = []
            for staged_path in staged_paths:
                relative = staged_path.relative_to(stage_root)
                output = self.root / relative
                planned[output] = staged_path.read_text(encoding="utf-8")
                output_paths.append(output)
            self._commit(planned)
        return output_paths[0], output_paths[1:]

    def remove_source(self, source_id: str, paper_title: str, trash_root: Path) -> Dict[str, List[str]]:
        """Remove one source's generated contribution while preserving user notes.

        Files that no longer have a source are moved into the workspace trash so
        the operation remains recoverable.
        """
        modified: List[str] = []
        trashed: List[str] = []
        paper_link = "[[" + paper_title + "]]"
        for path in list(self.all_pages()):
            content = path.read_text(encoding="utf-8")
            front = parse_frontmatter(content)
            value = front.get("sources") or []
            sources = [value] if isinstance(value, str) else [str(item) for item in value] if isinstance(value, list) else []
            page_title = str(front.get("title") or front.get("name") or path.stem.replace("-", " "))
            is_paper_page = str(front.get("type") or "") == "paper" and normalize_name(page_title) == normalize_name(paper_title)
            if source_id not in sources and not is_paper_page and paper_link not in content:
                continue
            if is_paper_page:
                target = trash_root / path.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                trashed.append(str(path))
                continue
            remaining = [item for item in sources if item != source_id]
            updated = re.sub(
                r"^###\s+\[\[" + re.escape(paper_title) + r"\]\]\s*$[\s\S]*?(?=^###\s+|^##\s+|\Z)",
                "",
                content,
                flags=re.M,
            )
            updated = "\n".join(line for line in updated.splitlines() if paper_link not in line) + "\n"
            updated = replace_frontmatter(updated, {"sources": remaining})
            notes = self._user_notes(updated)
            note_body = notes.replace(NOTE_START, "").replace(NOTE_END, "").strip()
            has_other_links = bool(wiki_links(self._generated_part(updated)))
            if not remaining and not note_body and not has_other_links:
                target = trash_root / path.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                trashed.append(str(path))
            else:
                self._atomic_write(path, updated)
                modified.append(str(path))
        return {"modified": modified, "trashed": trashed}


_FRONTMATTER = re.compile(r"^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)")


def parse_frontmatter(content: str) -> Dict[str, Any]:
    match = _FRONTMATTER.match(content)
    if not match:
        return {}
    try:
        value = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def replace_frontmatter(content: str, updates: Dict[str, Any]) -> str:
    match = _FRONTMATTER.match(content)
    front = parse_frontmatter(content)
    front.update(updates)
    body = content[match.end():] if match else content
    payload = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()
    return "---\n" + payload + "\n---\n\n" + body.lstrip("\r\n")


def wiki_links(content: str) -> List[str]:
    return list(dict.fromkeys(match.strip() for match in re.findall(r"\[\[([^\]]+)\]\]", content)))
