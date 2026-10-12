"""Deterministic compilation of cached paper extractions into a traceable Wiki.

This service never calls a model or modifies a source file.  The manifest is the
machine contract; run reports and the maintenance log carry volatile timestamps.
Existing files are adopted only when their generated section can be reproduced
by the old Wiki writer.  Personal notes never belong to the compiler.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .config import Config
from .index.database import Database
from .models import ParsedDocument, StructuredPaper
from .parsing.pdf import parse_file
from .wiki.manager import (
    NOTE_END, NOTE_START, WikiManager, normalize_name, parse_frontmatter, slugify,
    wiki_links,
)


COMPILER = "researchwiki.compiler.v1"
SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class _Source:
    source_id: str
    paper: StructuredPaper
    raw: Path
    raw_path: str
    document: ParsedDocument
    extraction_hash: str


class CompilationService:
    def __init__(self, config: Config, database: Optional[Database] = None):
        self.config = config
        self.database = database
        self.wiki = WikiManager(config.wiki_root)
        self.state_dir = config.root / "data" / "compilation"
        self.manifest_path = config.wiki_root / "manifest.json"

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.config.root.resolve()).as_posix()
        except ValueError:
            return path.name

    def manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        reports = []
        for path in sorted((self.state_dir / "runs").glob("*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    reports.append(value)
            except (OSError, ValueError):
                continue
            if len(reports) >= max(1, min(limit, 100)):
                break
        return reports

    def _records(self) -> List[Dict[str, Any]]:
        if self.database is not None:
            return self.database.list_papers(page_size=100000)["items"]
        db = Database(self.config.db_path)
        try:
            return db.list_papers(page_size=100000)["items"]
        finally:
            db.close()

    def _sources(self) -> Tuple[List[_Source], List[Dict[str, Any]], List[Dict[str, Any]]]:
        sources, inventory, diagnostics = [], [], []
        for record in sorted(self._records(), key=lambda item: str(item["id"])):
            source_id = str(record["id"])
            item = {"source_id": source_id, "title": str(record["title"]), "ready": False}
            inventory.append(item)
            extraction = self.config.root / "data" / "extractions" / (source_id + ".json")
            raw = Path(str(record.get("raw_path") or ""))
            if not raw.is_absolute():
                raw = self.config.root / raw
            item["raw_path"] = self._relative(raw)
            try:
                if not extraction.is_file():
                    item["reason"] = "尚无结构化抽取缓存，请先完成资料导入。"
                    diagnostics.append({"severity": "warning", "code": "missing_extraction", "source_id": source_id, "message": item["reason"]})
                    continue
                if not raw.is_file():
                    raise ValueError("原始资料副本不存在，不能验证来源。")
                if _file_hash(raw) != source_id:
                    raise ValueError("原始资料哈希与登记身份不一致，已停止编译。")
                payload = json.loads(extraction.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not str(payload.get("title") or "").strip():
                    raise ValueError("结构化抽取缓存缺少有效标题。")
                if str(payload.get("source_hash") or "") != source_id:
                    raise ValueError("结构化抽取缓存与来源哈希不匹配。")
                paper = StructuredPaper.from_dict(payload)
                document = parse_file(raw)
                if not any(page.text.strip() for page in document.pages):
                    raise ValueError("原始资料没有可定位的文本，请先处理 OCR。")
                # Absolute input paths are runtime details, not content identity.
                stable_payload = dict(payload)
                stable_payload.pop("source_path", None)
                sources.append(_Source(source_id, paper, raw, item["raw_path"], document, _digest(stable_payload)))
                item.update(title=paper.title, ready=True, evidence_pages=len(document.pages), reason="缓存与原件哈希验证通过")
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                item["reason"] = str(exc)
                diagnostics.append({"severity": "error", "code": "source_integrity", "source_id": source_id, "message": str(exc)})
        return sources, inventory, diagnostics

    @staticmethod
    def _evidence(source: _Source, terms: List[str]) -> Dict[str, Any]:
        """Return an exact excerpt; a fallback is never presented as a claim match."""
        selected = None
        for term in terms:
            if len(term.strip()) < 3:
                continue
            pattern = re.compile(r"\s+".join(re.escape(part) for part in term.split()), re.I)
            for page in source.document.pages:
                match = pattern.search(page.text)
                if match:
                    selected = (page, max(0, match.start() - 100), "exact_text")
                    break
            if selected:
                break
        if selected is None:
            selected = (next(page for page in source.document.pages if page.text.strip()), 0, "source_excerpt_fallback")
        page, start, confidence = selected
        end = min(len(page.text), start + 680)
        while start < end and page.text[start].isspace():
            start += 1
        while end > start and page.text[end - 1].isspace():
            end -= 1
        paragraph = len(re.findall(r"\n\s*\n", page.text[:start])) + 1
        return {
            "source_id": source.source_id, "source_path": source.raw_path,
            "page_number": page.number, "paragraph": paragraph,
            "char_start": start, "char_end": end,
            "locator": f"page:{page.number}#chars={start}-{end}",
            "evidence_text": page.text[start:end], "confidence": confidence,
        }

    @staticmethod
    def _specs(sources: List[_Source]) -> List[Dict[str, Any]]:
        specs: Dict[str, Dict[str, Any]] = {}
        for source in sources:
            paper = source.paper
            paper_id = "paper:" + source.source_id
            specs[paper_id] = {"id": paper_id, "type": "paper", "title": paper.title, "contributors": [(source, "", "")]}
            for kind, values in (("method", paper.methods), ("dataset", paper.datasets), ("topic", paper.topics)):
                for entity in values:
                    title = entity if isinstance(entity, str) else entity.name
                    if not title.strip():
                        continue
                    page_id = kind + ":" + hashlib.sha256(normalize_name(title).encode("utf-8")).hexdigest()[:16]
                    spec = specs.setdefault(page_id, {"id": page_id, "type": kind, "title": title, "contributors": []})
                    if any(item[0].source_id == source.source_id for item in spec["contributors"]):
                        continue
                    spec["contributors"].append((source, "" if isinstance(entity, str) else entity.description, "" if isinstance(entity, str) else entity.role))
        return sorted(specs.values(), key=lambda item: item["id"])

    def _legacy_matches(self, path: Path, spec: Dict[str, Any]) -> bool:
        """Recognize legacy generated text exactly, allowing only volatile metadata."""
        old = path.read_text(encoding="utf-8")
        if NOTE_START not in old or NOTE_END not in old:
            return False
        if spec["type"] == "paper":
            declared_hash = parse_frontmatter(old).get("source_hash")
            if declared_hash and declared_hash != spec["contributors"][0][0].source_id:
                return False

        def canonical(text: str) -> Tuple[Dict[str, Any], str]:
            text = self.wiki._generated_part(text)
            front = parse_frontmatter(text)
            for key in ("updated_at", "sources", "source_hash"):
                front.pop(key, None)
            body = re.sub(r"\A---\s*\n[\s\S]*?\n---\s*\n", "", text).strip()
            return front, body

        if spec["type"] == "paper":
            source = spec["contributors"][0][0]
            with tempfile.TemporaryDirectory(prefix="researchwiki-legacy-check-") as directory:
                generated_path = WikiManager(Path(directory)).write_paper(source.paper, source.raw_path)
                generated = generated_path.read_text(encoding="utf-8")
        else:
            generated = ""
            links = wiki_links(old)
            def order(entry: Tuple[_Source, str, str]) -> Tuple[int, str]:
                title = entry[0].paper.title
                return (links.index(title) if title in links else len(links), entry[0].source_id)
            for source, description, role in sorted(spec["contributors"], key=order):
                generated = self.wiki._entity_generated(spec["type"] + "s", spec["title"], source.paper, description, role, generated)
        return canonical(old) == canonical(generated)

    def _choose_paths(self, specs: List[Dict[str, Any]], previous: Dict[str, Any], diagnostics: List[Dict[str, Any]]) -> None:
        previous_by_id = {item.get("id"): item for item in previous.get("pages", []) if isinstance(item, dict)}
        reserved: set = set()
        for spec in specs:
            old = previous_by_id.get(spec["id"], {})
            path = self.wiki.page_path(spec["type"], spec["title"])
            if old.get("path"):
                candidate = self.config.root / str(old["path"])
                # A corrupt/tampered manifest cannot cause arbitrary file writes.
                if candidate.resolve().is_relative_to(self.config.wiki_root.resolve()) and candidate.suffix == ".md":
                    path = candidate
            elif (existing := self.wiki.find_page(spec["type"], spec["title"])):
                path = existing
            if path.exists():
                current = path.read_text(encoding="utf-8")
                front = parse_frontmatter(current)
                trusted = front.get("generated_by") == COMPILER and front.get("id") == spec["id"]
                if trusted and old.get("generated_sha256"):
                    trusted = _digest(self.wiki._generated_part(current)) == old["generated_sha256"]
                if not trusted and not self._legacy_matches(path, spec):
                    diagnostics.append({"severity": "warning", "code": "preserved_manual_page", "path": self._relative(path), "message": "该页面含有未由当前编译器确认的内容，保留原文件并使用独立生成路径。"})
                    path = path.with_name(slugify(spec["title"]) + "--" + spec["id"].split(":")[-1][:12] + ".md")
            if path in reserved or (path.exists() and parse_frontmatter(path.read_text(encoding="utf-8")).get("id") not in (None, spec["id"])):
                path = path.with_name(slugify(spec["title"]) + "--" + spec["id"].split(":")[-1][:12] + ".md")
            # The stable fallback may itself have become a human-maintained file.
            if path.exists() and path.name.endswith("--" + spec["id"].split(":")[-1][:12] + ".md"):
                current = path.read_text(encoding="utf-8")
                front = parse_frontmatter(current)
                changed_generated = (
                    old.get("path") == self._relative(path)
                    and old.get("generated_sha256")
                    and _digest(self.wiki._generated_part(current)) != old["generated_sha256"]
                )
                if front.get("generated_by") != COMPILER or front.get("id") != spec["id"] or changed_generated:
                    diagnostics.append({"severity": "error", "code": "path_conflict", "path": self._relative(path), "message": "稳定生成路径已被人工文件占用，请先人工整理该冲突。"})
            spec["path"] = path
            reserved.add(path)

    def _render(self, spec: Dict[str, Any], title_by_id: Dict[str, str]) -> Tuple[str, List[Dict[str, Any]]]:
        contributors = spec["contributors"]
        evidence = []
        for source, description, _role in contributors:
            terms = [spec["title"], description]
            evidence.append(self._evidence(source, terms))
        source_ids = [source.source_id for source, _, _ in contributors]
        front = {"id": spec["id"], "type": spec["type"], "title": spec["title"], "sources": source_ids,
                 "generated_by": COMPILER, "schema_version": SCHEMA_VERSION}
        if spec["type"] == "paper":
            source = contributors[0][0]
            paper = source.paper
            front.update(source=source.raw_path, source_hash=source.source_id, authors=paper.authors, year=paper.year)
            lines = [f"# {paper.title}", "", "## 基本信息", "", f"作者：{', '.join(paper.authors) or '未提供'}", f"年份：{paper.year or '未提供'}", "", "## 摘要", "", (paper.abstract.strip() + " [1]") if paper.abstract else "原文未提取到摘要。", "", "## 关键词", ""]
            lines.extend(["- " + keyword for keyword in paper.keywords] or ["- 原文未提取到作者关键词。"])
            for heading, kind, values in (("数据集", "dataset", paper.datasets), ("相关方法", "method", paper.methods), ("相关研究主题", "topic", paper.topics)):
                names = [value if isinstance(value, str) else value.name for value in values]
                names = [title_by_id.get(kind + ":" + hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()[:16], name) for name in names if name.strip()]
                lines.extend(["", "## " + heading, "", *(["- [[" + name + "]]" for name in dict.fromkeys(names)] or ["- 未识别。"])])
            lines.extend(["", "## 实验信息", "", paper.experiment_info.strip() + " [1]" if paper.experiment_info.strip() else "原文未提取到明确实验设置。"])
            if paper.metrics:
                lines.extend(["", "### 评价指标", "", *["- " + x + " [1]" for x in paper.metrics]])
            lines.extend(["", "## 原文来源", "", "原始文件：`" + source.raw_path + "`", "SHA-256：`" + source.source_id + "`", ""])
        else:
            kind = spec["type"]
            lines = ["# " + spec["title"], ""]
            if kind == "topic":
                lines.extend(["## 概念简介", "", "由相关论文汇集的研究主题；尚未核实独立定义。", "", "## 研究方向分类", "", "尚未形成有证据支持的子方向分类。", "", "## 相关论文", ""])
            elif kind == "method":
                definitions = [(i, d) for i, (_, d, _) in enumerate(contributors, 1) if d.strip()]
                principles = [(i, p) for i, (source, _, _) in enumerate(contributors, 1) for method in source.paper.methods if normalize_name(method.name) == normalize_name(spec["title"]) for p in [method.principle] if p.strip()]
                lines.extend(["## 方法定义", "", *([f"- {d} [{i}]" for i,d in definitions] or ["来源未提供明确的方法定义。"]), "", "## 基本原理", "", *([f"- {p} [{i}]" for i,p in principles] or ["来源未提供可核验的原理说明。"]), "", "## 所属论文与应用场景", ""])
            else:
                descriptions = [(i, d) for i, (_, d, _) in enumerate(contributors, 1) if d.strip()]
                statistics = [(i, p) for i, (source, _, _) in enumerate(contributors, 1) for dataset in source.paper.datasets if normalize_name(dataset.name) == normalize_name(spec["title"]) for p in [dataset.statistics] if p.strip()]
                lines.extend(["## 数据集简介", "", *([f"- {d} [{i}]" for i,d in descriptions] or ["来源未提供数据集简介。"]), "", "## 任务", "", *([f"- {role} [{i}]" for i,(_, _, role) in enumerate(contributors, 1) if role.strip()] or ["来源未提供明确的任务说明。"]), "", "## 统计信息", "", *([f"- {p} [{i}]" for i,p in statistics] or ["来源未提供可核验的统计信息。"]), "", "## 使用该数据集的论文", ""])
            for index, (source, description, role) in enumerate(contributors, 1):
                lines.append("- [[" + source.paper.title + "]]")
                if kind != "topic" and role.strip():
                    lines.append("  - 用途：" + role + f" [{index}]")
            lines.extend(["", "## 证据说明", "", "此页仅汇总各来源中相关的知识与出处，不重复整篇论文摘要或完整实验结论。来源摘录不等于对每条论断独立核验。", ""])
        lines.extend(["## 来源证据", ""])
        for index, ((source, _, _), span) in enumerate(zip(contributors, evidence), 1):
            label = "原文命中" if span["confidence"] == "exact_text" else "来源摘录（未定位到主题原词，不代表逐条论断已核验）"
            lines.extend([f"### [{index}] {source.paper.title}", "", f"来源：`{source.raw_path}` · 第 {span['page_number']} 页 · 第 {span['paragraph']} 段 · `{span['locator']}` · {label}", "", *[("> " + line).rstrip() for line in span["evidence_text"].splitlines()], ""])
        generated = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n" + "\n".join(lines).rstrip()
        return generated, evidence

    def _prepare(self) -> Tuple[Dict[str, Any], Dict[Path, str], Dict[str, Any], List[_Source]]:
        sources, inventory, diagnostics = self._sources()
        previous = self.manifest()
        if self.manifest_path.exists() and previous.get("compiler") != COMPILER:
            diagnostics.append({"severity": "error", "code": "manifest_conflict", "message": "已有 manifest.json 不是当前编译器的清单，不能覆盖。", "path": self._relative(self.manifest_path)})
        specs = self._specs(sources)
        self._choose_paths(specs, previous, diagnostics)
        title_by_id = {spec["id"]: spec["title"] for spec in specs}
        titles: Dict[str, List[str]] = defaultdict(list)
        for spec in specs:
            titles[normalize_name(spec["title"])].append(spec["id"])
        for title, ids in titles.items():
            if len(ids) > 1:
                diagnostics.append({"severity": "warning", "code": "ambiguous_title", "message": "不同知识对象同名，标题内链可能存在歧义：" + title, "ids": ids})
        changes: Dict[str, List[str]] = {key: [] for key in ("created", "updated", "unchanged", "preserved", "stale")}
        planned, pages = {}, []
        for spec in specs:
            path = spec["path"]
            relative = self._relative(path)
            generated, evidence = self._render(spec, title_by_id)
            content = self.wiki._compose(path, generated)
            old = path.read_text(encoding="utf-8") if path.exists() else None
            changes["created" if old is None else "unchanged" if old == content else "updated"].append(relative)
            planned[path] = content
            pages.append({"id": spec["id"], "title": spec["title"], "type": spec["type"], "path": relative,
                          "sources": [item[0].source_id for item in spec["contributors"]], "links": wiki_links(generated),
                          "generated_sha256": _digest(self.wiki._generated_part(content)), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "evidence": evidence})
            descriptions = {description.strip() for _, description, _ in spec["contributors"] if description.strip()}
            if len(descriptions) > 1:
                diagnostics.append({"severity": "info", "code": "multiple_source_descriptions", "message": "各来源对同一对象有不同描述，已分别保留以供比较；未自动判定为冲突。", "path": relative})
            for span in evidence:
                if span["confidence"] != "exact_text":
                    diagnostics.append({"severity": "warning", "code": "fallback_evidence", "message": "未命中主题原词，显示来源摘录；需要人工复核具体论断。", "path": relative, "source_id": span["source_id"]})
        planned_paths = {page["path"] for page in pages}
        for old in previous.get("pages", []):
            if old.get("path") not in planned_paths:
                changes["stale"].append(str(old.get("path")))
                diagnostics.append({"severity": "warning", "code": "stale_page", "message": "该页面已没有当前编译输入，保留现有文件和人工笔记，请在 Review 中复核。", "path": old.get("path")})
        for path in self.wiki.all_pages():
            if path in planned:
                continue
            relative = self._relative(path)
            changes["preserved"].append(relative)
            front = parse_frontmatter(path.read_text(encoding="utf-8"))
            titles[normalize_name(str(front.get("title") or front.get("name") or path.stem))].append(relative)
        for page in pages:
            for link in page["links"]:
                if normalize_name(link) not in titles:
                    diagnostics.append({"severity": "warning", "code": "broken_link", "message": "内链缺少目标页面：" + link, "path": page["path"]})
        stable_sources = [{"id": source.source_id, "sha256": source.source_id, "title": source.paper.title, "path": source.raw_path, "extraction_sha256": source.extraction_hash} for source in sources]
        input_digest = _digest({"compiler": COMPILER, "sources": stable_sources})
        content_digest = _digest([{key: page[key] for key in ("id", "path", "generated_sha256")} for page in pages])
        manifest = {"schema_version": SCHEMA_VERSION, "compiler": COMPILER, "input_digest": input_digest,
                    "content_digest": content_digest, "sources": stable_sources, "pages": pages}
        for name in ("index.md", "log.md"):
            target = self.config.wiki_root / name
            if target.exists() and not parse_frontmatter(target.read_text(encoding="utf-8")).get("compiler_auxiliary"):
                diagnostics.append({"severity": "error", "code": "auxiliary_conflict", "message": f"已有 {name} 是人工文档，不能覆盖。", "path": self._relative(target)})
        report = {"status": "blocked" if not sources or any(item["severity"] == "error" for item in diagnostics) else "ready",
                  "source_count": len(inventory), "ready_sources": len(sources), "skipped_sources": len(inventory) - len(sources),
                  "page_count": len(pages), "page_types": dict(Counter(page["type"] for page in pages)), "sources": inventory,
                  "changes": changes, "diagnostics": diagnostics, "input_digest": input_digest, "content_digest": content_digest}
        return report, planned, manifest, sources

    def inventory(self) -> Dict[str, Any]:
        return self._prepare()[0]

    def _index(self, manifest: Dict[str, Any]) -> str:
        lines = ["---", "compiler_auxiliary: true", "generated_by: " + COMPILER, "---", "", "# 论文知识导航", "", "按稳定研究对象浏览；每页均保留来源证据与个人笔记。", ""]
        for kind, title in (("paper", "论文"), ("method", "方法"), ("dataset", "数据集"), ("topic", "研究主题")):
            lines.extend(["## " + title, ""])
            for page in manifest["pages"]:
                if page["type"] == kind:
                    relative = (self.config.root / page["path"]).relative_to(self.config.wiki_root).as_posix()
                    lines.append(f"- [{page['title']}]({relative}) · {len(page['sources'])} 个来源")
            lines.append("")
        return "\n".join(lines)

    def run(self) -> Dict[str, Any]:
        started = _utc()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        report_path = self.state_dir / "runs" / (run_id + ".json")
        report: Dict[str, Any] = {"id": run_id, "started_at": started, "status": "failed", "raw_unchanged": False}
        try:
            inventory, planned, manifest, sources = self._prepare()
            report.update(inventory, id=run_id, started_at=started)
            if inventory["status"] != "ready":
                raise ValueError("没有可编译的资料，或检测到完整性/文件归属冲突；请查看诊断。")
            if any(_file_hash(source.raw) != source.source_id for source in sources):
                raise ValueError("准备期间原始资料发生变化，未提交任何 Wiki 文件。")
            index_path = self.config.wiki_root / "index.md"
            log_path = self.config.wiki_root / "log.md"
            report.update(status="succeeded", finished_at=_utc(), raw_unchanged=True,
                          manifest_path=self._relative(self.manifest_path), index_path=self._relative(index_path), log_path=self._relative(log_path))
            old_log = log_path.read_text(encoding="utf-8") if log_path.exists() else "---\ncompiler_auxiliary: true\ngenerated_by: " + COMPILER + "\n---\n\n# 知识维护记录\n"
            log_entry = f"\n## {report['finished_at']} · {run_id}\n\n- 来源：{len(sources)}；页面：{len(manifest['pages'])}；新增：{len(inventory['changes']['created'])}；更新：{len(inventory['changes']['updated'])}；未变：{len(inventory['changes']['unchanged'])}。\n- 输入：`{manifest['input_digest']}`\n- 内容：`{manifest['content_digest']}`\n- 原始文件哈希保持不变；编译未调用语言模型。\n"
            planned.update({self.manifest_path: _json(manifest), index_path: self._index(manifest), log_path: old_log.rstrip() + "\n" + log_entry, report_path: _json(report)})
            # WikiManager restores previously replaced files on any write error.
            self.wiki._commit(planned)
            return report
        except Exception as exc:
            report.update(status="failed", finished_at=_utc(), error=str(exc))
            self.wiki._atomic_write(report_path, _json(report))
            return report


def compile_wiki(config: Config) -> Dict[str, Any]:
    return CompilationService(config).run()


def compilation_status(config: Config) -> Dict[str, Any]:
    service = CompilationService(config)
    return {"inventory": service.inventory(), "manifest": service.manifest(), "history": service.history()}
