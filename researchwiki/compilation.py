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
from .extraction.synthesis import load_topic_synthesis
from .extraction.quality import QualityGateError, assess_paper
from .index.database import Database
from .models import ParsedDocument, StructuredPaper
from .parsing.pdf import parse_file
from .wiki.entities import ENTITY_TYPES, EntityRegistry
from .wiki.manager import (
    NOTE_END, NOTE_START, WikiManager, normalize_name, parse_frontmatter, slugify,
    wiki_links,
)


COMPILER = "researchwiki.compiler.v2"
LEGACY_COMPILER = "researchwiki.compiler.v1"
SCHEMA_VERSION = 2


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
    claims: Dict[str, Any]


class CompilationService:
    def __init__(self, config: Config, database: Optional[Database] = None):
        self.config = config
        self.database = database
        self.wiki = WikiManager(config.wiki_root)
        self.state_dir = config.root / "data" / "compilation"
        self.manifest_path = config.wiki_root / "manifest.json"
        self.entities = EntityRegistry()

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
                quality = assess_paper(paper)
                if not quality["allowed"] and paper.extraction_mode != "basic":
                    item.update(reason="结构化缓存未通过质量门禁，已隔离其历史生成页面。", isolated=True, quality=quality)
                    diagnostics.append({"severity": "warning", "code": "source_quality", "source_id": source_id,
                                        "message": item["reason"], "issues": quality.get("issues", [])})
                    continue
                item["quality"] = quality
                for issue in quality.get("issues", []):
                    if issue.get("severity") == "warning":
                        diagnostics.append({"severity": "warning", "code": "source_quality_warning", "source_id": source_id,
                                            "message": issue.get("message", "结构化缓存尚不完整。"), "quality_code": issue.get("code")})
                document = parse_file(raw)
                if not any(page.text.strip() for page in document.pages) and paper.extraction_mode != "basic":
                    raise ValueError("原始资料没有可定位的文本，请先处理 OCR。")
                # Absolute input paths are runtime details, not content identity.
                stable_payload = dict(payload)
                stable_payload.pop("source_path", None)
                claims_path = self.config.root / "data" / "claims" / (source_id + ".json")
                existing = None
                if claims_path.is_file():
                    try:
                        existing = json.loads(claims_path.read_text(encoding="utf-8"))
                        if not isinstance(existing, dict) or not isinstance(existing.get("claims", []), list):
                            raise ValueError("无效 Claim bundle")
                    except (OSError, ValueError):
                        diagnostics.append({"severity": "warning", "code": "claims_cache_invalid", "source_id": source_id,
                                            "message": "Claim 缓存无法读取，已从抽取记录重新核验。"})
                # Cached status is not trust: verify again against the immutable
                # source, with human decisions bound by the verifier to digests.
                from .verification import verify_paper
                bundle = verify_paper(paper, document, source_id, existing=existing)
                if paper.extraction_mode == "basic":
                    bundle["claims"] = []
                source = _Source(source_id, paper, raw, item["raw_path"], document, _digest(stable_payload), bundle)
                supported = self._verified_claims(source, diagnostics)
                item.update(title=paper.title, ready=True, evidence_pages=len(document.pages),
                            supported_claims=len(supported), pending_claims=len(bundle.get("claims", [])) - len(supported),
                            reason="缓存与原件哈希验证通过；具体事实仅使用已核验 Claim。")
                sources.append(source)
            except QualityGateError as exc:
                item.update(reason=str(exc), isolated=True, quality=exc.quality)
                diagnostics.append({"severity": "warning", "code": "source_quality", "source_id": source_id,
                                    "message": str(exc), "issues": exc.issues})
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
                    selected = (page, max(0, match.start() - 100), "keyword_hit")
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
    def _verified_claims(source: _Source, diagnostics: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Require both the verifier's decision and the exact current raw span."""
        if source.paper.extraction_mode == "basic":
            return []
        result = []
        for claim in source.claims.get("claims", []):
            if not isinstance(claim, dict):
                continue
            verification = claim.get("verification") or {}
            evidence = claim.get("evidence") or {}
            quote = str(evidence.get("quote") or "")
            valid = (verification.get("status") == "supported" and bool(str(claim.get("claim_id") or "").strip())
                     and bool(str(claim.get("statement") or "").strip()))
            try:
                page = next(page for page in source.document.pages if page.number == int(evidence.get("page_number")))
                start, end = int(evidence.get("char_start")), int(evidence.get("char_end"))
                valid = valid and evidence.get("source_id") == source.source_id and bool(quote.strip())
                valid = valid and 0 <= start < end <= len(page.text) and page.text[start:end] == quote
            except (StopIteration, TypeError, ValueError):
                valid = False
            if valid:
                result.append(claim)
            elif diagnostics is not None:
                diagnostics.append({"severity": "warning", "code": "claim_quarantined", "source_id": source.source_id,
                                    "claim_id": claim.get("claim_id"), "verification_status": verification.get("status", "pending_review"),
                                    "message": str(verification.get("reason") or "该 Claim 未获原文精确证据支持，已从正文与实体页面隔离。")})
        return sorted(result, key=lambda claim: str(claim.get("claim_id") or ""))

    def _specs(self, sources: List[_Source]) -> List[Dict[str, Any]]:
        specs: Dict[str, Dict[str, Any]] = {}
        for source in sources:
            paper = source.paper
            claims = self._verified_claims(source)
            paper_id = "paper:" + source.source_id
            specs[paper_id] = {"id": paper_id, "type": "paper", "title": paper.title,
                               "contributors": [(source, "", "")], "claims": [(source, claim) for claim in claims]}
            # Entity indexes are navigation, not factual approval. Their model
            # descriptions live only in explicitly unverified reading sections.
            if paper.extraction_mode != "basic":
                for kind, values in (("method", paper.methods), ("dataset", paper.datasets), ("topic", paper.topics)):
                    for original in values:
                        name = original if isinstance(original, str) else original.name
                        if not str(name).strip():
                            continue
                        entity = self.entities.resolve(kind, name)
                        spec = specs.setdefault(entity["canonical_id"], {"id": entity["canonical_id"], "type": kind,
                            "title": entity["name"], "aliases": entity["aliases"], "contributors": [], "claims": []})
                        if not any(entry[0].source_id == source.source_id for entry in spec["contributors"]):
                            spec["contributors"].append((source, getattr(original, "description", ""), getattr(original, "role", "")))
            for claim in claims:
                for mention in claim.get("entities") or []:
                    if not isinstance(mention, dict):
                        continue
                    kind, name = str(mention.get("type") or ""), str(mention.get("name") or "").strip()
                    if kind not in ENTITY_TYPES or not name:
                        continue
                    entity = self.entities.resolve(kind, name)
                    page_id = entity["canonical_id"]
                    spec = specs.setdefault(page_id, {"id": page_id, "type": kind, "title": entity["name"],
                                                      "aliases": entity["aliases"], "contributors": [], "claims": []})
                    if not any(item[0].source_id == source.source_id and item[1].get("claim_id") == claim.get("claim_id") for item in spec["claims"]):
                        spec["claims"].append((source, claim))
                    if not any(item[0].source_id == source.source_id for item in spec["contributors"]):
                        values = {"method": paper.methods, "dataset": paper.datasets, "topic": paper.topics}[kind]
                        original = next((value for value in values if EntityRegistry().resolve(kind, value if isinstance(value, str) else value.name)["canonical_id"] == page_id), None)
                        spec["contributors"].append((source, getattr(original, "description", ""), getattr(original, "role", "")))
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
                trusted = front.get("generated_by") in {COMPILER, LEGACY_COMPILER} and front.get("id") == spec["id"]
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
                if front.get("generated_by") not in {COMPILER, LEGACY_COMPILER} or front.get("id") != spec["id"] or changed_generated:
                    diagnostics.append({"severity": "error", "code": "path_conflict", "path": self._relative(path), "message": "稳定生成路径已被人工文件占用，请先人工整理该冲突。"})
            spec["path"] = path
            reserved.add(path)

    def _display_title(self, spec: Dict[str, Any]) -> str:
        name = spec["title"]
        if self.config.wiki_language != "zh-CN" or spec["type"] == "paper":
            return name
        labels = []
        for source, _, _ in spec["contributors"]:
            paper = source.paper
            if spec["type"] == "topic":
                labels.extend(str(label).strip() for original, label in paper.topic_labels_zh.items()
                              if self.entities.resolve("topic", original)["canonical_id"] == spec["id"])
            else:
                values = paper.methods if spec["type"] == "method" else paper.datasets
                labels.extend(item.name_zh.strip() for item in values
                              if self.entities.resolve(spec["type"], item.name)["canonical_id"] == spec["id"])
        labels = [label for label in labels if re.search(r"[\u3400-\u9fff]", label)]
        if not labels:
            return name
        counts = Counter(labels)
        preferred = sorted(counts, key=lambda label: (-counts[label], label))[0]
        return name if normalize_name(preferred) == normalize_name(name) else f"{preferred}（{name}）"

    def _render(self, spec: Dict[str, Any], title_by_id: Dict[str, str]) -> Tuple[str, List[Dict[str, Any]]]:
        contributors = spec["contributors"]
        evidence = []
        claims = spec["claims"]
        for source, claim in claims:
            span = claim["evidence"]
            start, end = int(span["char_start"]), int(span["char_end"])
            evidence.append({"claim_id": claim["claim_id"], "source_id": source.source_id,
                             "source_path": source.raw_path, "page_number": int(span["page_number"]),
                             "paragraph": span.get("paragraph"), "char_start": start, "char_end": end,
                             "locator": f"page:{int(span['page_number'])}#chars={start}-{end}",
                             "evidence_text": span["quote"], "quote": span["quote"],
                             "confidence": "verified_quote", "verification_status": "supported"})
        source_ids = [source.source_id for source, _, _ in contributors]
        front = {"id": spec["id"], "type": spec["type"], "title": spec["title"], "display_title": self._display_title(spec), "sources": source_ids,
                 "generated_by": COMPILER, "schema_version": SCHEMA_VERSION,
                 "claim_ids": [claim["claim_id"] for _, claim in claims],
                 "verification_status": "supported" if claims else "no_supported_claims"}
        if spec.get("aliases"):
            front.update(canonical_id=spec["id"], aliases=spec["aliases"])
        # Source text must remain text, not compiler section delimiters/links.
        def literal(value: Any) -> str:
            return str(value).replace("<!--", "&lt;!--").replace("[[", "&#91;&#91;").replace("]]", "&#93;&#93;")

        if spec["type"] == "paper":
            source = contributors[0][0]
            paper = source.paper
            front.update(source=source.raw_path, source_hash=source.source_id, authors=paper.authors, year=paper.year)
            lines = [f"# {literal(paper.title)}", "", "## 基本信息", "",
                     "以下为抽取记录中的书目信息，不作为研究结论的验证依据。", "",
                     f"作者：{literal(', '.join(paper.authors)) or '未提供'}", f"年份：{paper.year or '未提供'}", "",
                     "## 已核验事实", ""]
            if not claims:
                lines.extend(["尚无得到原文支持的 Claim；摘要、方法概述和实验结论未进入知识正文。", ""])
            headings = {"abstract": "摘要", "research_problem": "研究问题", "methodology": "核心方法",
                        "method": "方法", "dataset": "数据集", "topic": "研究主题", "contribution": "主要贡献",
                        "contributions": "主要贡献", "metric": "评价指标", "metrics": "评价指标",
                        "finding": "实验结论", "findings": "实验结论", "result": "实验结论",
                        "limitation": "局限性", "limitations": "局限性"}
            for index, (_source, claim) in enumerate(claims, 1):
                lines.extend([f"### {headings.get(str(claim.get('claim_type') or ''), '事实')} · {literal(claim['claim_id'])}", "",
                              literal(claim["statement"]) + f" [{index}]", ""])
            pending_count = len(source.claims.get("claims", [])) - len(claims)
            lines.extend(["## 待核验记录", "", f"{pending_count} 条 Claim 尚未获得支持，保存在审核记录中；未将其作为事实显示。", ""])
            lines.extend(["<!-- pending-reading:start -->", "## 阅读导览（待核验解读）", "",
                          "本区为模型解读或离线原文展示，不作为已支持事实、图谱实验关系或问答知识证据。", ""])
            if paper.extraction_mode == "basic":
                lines.extend(["当前为基础论文资料卡；尚未进行方法和结果的深度解读。", ""])
            if self.config.wiki_language == "zh-CN":
                lines.extend(["## 中文摘要", "", literal(paper.abstract_zh) if paper.abstract_zh else
                              "暂无中文摘要。基础模式不调用翻译模型；可查看原文。", ""])
            lines.extend(["## 原文摘要（Abstract）", "", literal(paper.abstract) or "原文未提取到摘要。", "",
                          "## 关键词（原文）", "", *["- " + literal(word) for word in paper.keywords], ""])
            if paper.extraction_mode != "basic":
                for heading, text in (("研究什么", paper.research_problem), ("怎么做", paper.methodology),
                                      ("实验信息", paper.experiment_info)):
                    lines.extend(["## " + heading, "", literal(text) or "尚未提取。", ""])
                for heading, values in (("模型提取的结果", paper.findings), ("模型提取的局限性", paper.limitations),
                                        ("模型提取的评价指标", paper.metrics)):
                    lines.extend(["## " + heading, "", *["- " + literal(value) for value in values], ""])
            lines.extend(["<!-- pending-reading:end -->", ""])
            related: Dict[str, List[str]] = defaultdict(list)
            for _, claim in claims:
                for mention in claim.get("entities") or []:
                    if not isinstance(mention, dict) or mention.get("type") not in ENTITY_TYPES or not str(mention.get("name") or "").strip():
                        continue
                    entity = self.entities.resolve(mention["type"], mention["name"])
                    related[entity["type"]].append(title_by_id[entity["canonical_id"]])
            for kind, heading in (("method", "方法"), ("dataset", "数据集"), ("topic", "相关研究主题")):
                lines.extend(["## " + heading, "", *(["- [[" + name + "]]" for name in dict.fromkeys(related[kind])] or ["- 暂无已核验关联。"]), ""])
        else:
            heading = {"method": "方法在不同研究中的用途", "dataset": "数据集在不同研究中的用途", "topic": "研究问题与跨来源比较"}[spec["type"]]
            lines = ["# " + literal(self._display_title(spec)), "", "## 稳定问题", "", f"{literal(spec['title'])} 在这些论文中如何定义、使用，其证据和适用边界是什么？", "", "## " + heading, ""]
            for index, (source, claim) in enumerate(claims, 1):
                lines.extend(["### [[" + source.paper.title + "]]", "",
                              literal(claim["statement"]) + f" [{index}]", ""])
            lines.extend(["## 证据边界", "", "仅列出已核验 Claim 对该实体的明确关联。未将论文的其它结论或局限性自动归入此页；共享术语不代表实验条件或结论一致。", ""])
            lines.extend(["<!-- pending-reading:start -->", "## 待核验解读", "",
                          "以下描述仍需原文级人工审核，不进入事实问答。", ""])
            template = {"method": ("方法定义", "基本原理", "所属论文与应用场景"),
                        "dataset": ("数据集简介", "任务", "统计信息", "使用该数据集的论文"),
                        "topic": ("概念简介", "研究方向分类", "相关论文")}[spec["type"]]
            for heading in template:
                lines.extend(["## " + heading, ""])
                for source, description, role in contributors:
                    values = source.paper.methods if spec["type"] == "method" else source.paper.datasets
                    entity = next((item for item in values if self.entities.resolve(spec["type"], item.name)["canonical_id"] == spec["id"]), None) if spec["type"] != "topic" else None
                    text = (getattr(entity, "principle", "") if heading == "基本原理" else
                            getattr(entity, "statistics", "") if heading == "统计信息" else
                            role if heading in {"任务", "所属论文与应用场景"} else
                            source.paper.research_problem if heading == "研究方向分类" else description)
                    lines.append("- [[" + source.paper.title + "]]：" + (literal(text) if text else "仅作来源索引；尚无已核验统一说明。"))
                lines.append("")
            if spec["type"] == "topic":
                summary = load_topic_synthesis(self.config.root, spec["title"], [(source.source_id, source.paper) for source, _, _ in contributors])
                if summary:
                    lines.extend(["## 深度跨论文综合（待人工复核）", "", "模型综合未升级为 supported 事实。", ""])
                    if int(summary.get("batch_count") or 1) > 1:
                        lines.extend([f"本次按上下文预算分为 {int(summary['batch_count'])} 批；仅保证批内比较，不宣称完成所有来源之间的两两比较。", ""])
                    for key in ("claims", "open_questions"):
                        for point in summary.get(key, []):
                            lines.append("- " + literal(point.get("text", "")) + " · 来源：" + ", ".join(str(value) for value in point.get("source_ids", [])))
                    lines.append("")
            lines.extend(["<!-- pending-reading:end -->", ""])
        lines.extend(["## 来源证据", ""])
        for index, ((source, claim), span) in enumerate(zip(claims, evidence), 1):
            lines.extend([f"### [{index}] {literal(source.paper.title)}", "",
                          f"Claim：`{claim['claim_id']}` · 来源：`{source.raw_path}` · 第 {span['page_number']} 页 · `{span['locator']}` · 已核验原文片段", "",
                          *[("> " + literal(line)).rstrip() for line in span["evidence_text"].splitlines()], ""])
        generated = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n" + "\n".join(lines).rstrip()
        return generated, evidence

    def _prepare(self) -> Tuple[Dict[str, Any], Dict[Path, str], Dict[str, Any], List[_Source]]:
        # A service may be reused after inputs change. Seen identities only belong
        # to this preparation, not earlier previews.
        self.entities = EntityRegistry()
        sources, inventory, diagnostics = self._sources()
        previous = self.manifest()
        if self.manifest_path.exists() and previous.get("compiler") not in {COMPILER, LEGACY_COMPILER}:
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
            pages.append({"id": spec["id"], "title": spec["title"], "display_title": self._display_title(spec), "type": spec["type"], "path": relative,
                          "sources": [item[0].source_id for item in spec["contributors"]], "links": wiki_links(generated),
                          "generated_sha256": _digest(self.wiki._generated_part(content)), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "evidence": evidence,
                          "claim_ids": [claim["claim_id"] for _, claim in spec["claims"]],
                          "claims": [{"source_id": source.source_id, "claim_id": claim["claim_id"],
                                      "statement": claim["statement"], "claim_type": claim.get("claim_type"),
                                      "entities": claim.get("entities", []), "attributes": claim.get("attributes", {}),
                                      "evidence": span, "verification": claim.get("verification", {})}
                                     for (source, claim), span in zip(spec["claims"], evidence)]})
        planned_paths = {page["path"] for page in pages}
        for old in previous.get("pages", []):
            if old.get("path") not in planned_paths:
                changes["stale"].append(str(old.get("path")))
                diagnostics.append({"severity": "warning", "code": "stale_page", "message": "该页面已没有当前编译输入，保留现有文件和人工笔记，请在 Review 中复核。", "path": old.get("path")})
        excluded_paths = set(str(path) for path in previous.get("excluded_paths", []))
        known_sources = {str(item["source_id"]) for item in inventory}
        isolated_sources = {str(item["source_id"]) for item in inventory if item.get("isolated")}
        for path in self.wiki.all_pages():
            if path in planned:
                excluded_paths.discard(self._relative(path))
                continue
            relative = self._relative(path)
            changes["preserved"].append(relative)
            content = path.read_text(encoding="utf-8")
            front = parse_frontmatter(content)
            titles[normalize_name(str(front.get("title") or front.get("name") or path.stem))].append(relative)
            page_sources = front.get("sources") or []
            page_sources = {str(page_sources)} if isinstance(page_sources, str) else {str(value) for value in page_sources} if isinstance(page_sources, list) else set()
            if front.get("source_hash"):
                page_sources.add(str(front["source_hash"]))
            managed = str(front.get("generated_by") or "").startswith("researchwiki.compiler.")
            legacy = NOTE_START in content and NOTE_END in content and bool(page_sources & known_sources)
            if managed or legacy or page_sources & isolated_sources:
                excluded_paths.add(relative)
                diagnostics.append({"severity": "warning", "code": "historical_page_quarantined", "path": relative,
                                    "message": "保留历史页面和个人笔记；该页没有当前编译确认的事实，不进入检索、图谱和问答。"})
        for page in pages:
            for link in page["links"]:
                if normalize_name(link) not in titles:
                    diagnostics.append({"severity": "warning", "code": "broken_link", "message": "内链缺少目标页面：" + link, "path": page["path"]})
        stable_sources = [{"id": source.source_id, "sha256": source.source_id, "title": source.paper.title,
                           "path": source.raw_path, "extraction_sha256": source.extraction_hash,
                           "claims_sha256": _digest(source.claims)} for source in sources]
        input_digest = _digest({"compiler": COMPILER, "sources": stable_sources})
        content_digest = _digest([{key: page[key] for key in ("id", "path", "generated_sha256")} for page in pages])
        excluded_wiki_paths = []
        for relative in sorted(excluded_paths):
            try:
                excluded_wiki_paths.append((self.config.root / relative).resolve().relative_to(self.config.wiki_root.resolve()).as_posix())
            except ValueError:
                # Historical/tampered manifest entries outside the Wiki root
                # never become an exclusion of arbitrary workspace files.
                continue
        manifest = {"schema_version": SCHEMA_VERSION, "compiler": COMPILER, "input_digest": input_digest,
                    "content_digest": content_digest, "sources": stable_sources, "pages": pages,
                    "entities": self.entities.records(), "excluded_paths": sorted(excluded_paths),
                    "excluded_wiki_paths": excluded_wiki_paths}
        for name in ("index.md", "log.md"):
            target = self.config.wiki_root / name
            if target.exists() and not parse_frontmatter(target.read_text(encoding="utf-8")).get("compiler_auxiliary"):
                diagnostics.append({"severity": "error", "code": "auxiliary_conflict", "message": f"已有 {name} 是人工文档，不能覆盖。", "path": self._relative(target)})
        can_reconcile_empty = previous.get("compiler") in {COMPILER, LEGACY_COMPILER}
        report = {"status": "blocked" if (not sources and not isolated_sources and not can_reconcile_empty) or any(item["severity"] == "error" for item in diagnostics) else "ready",
                  "source_count": len(inventory), "ready_sources": len(sources), "skipped_sources": len(inventory) - len(sources),
                  "page_count": len(pages), "page_types": dict(Counter(page["type"] for page in pages)), "sources": inventory,
                  "changes": changes, "diagnostics": diagnostics, "input_digest": input_digest, "content_digest": content_digest,
                  "supported_claims": sum(item.get("supported_claims", 0) for item in inventory),
                  "pending_claims": sum(item.get("pending_claims", 0) for item in inventory),
                  "excluded_paths": sorted(excluded_paths),
                  "paper_paths": {page["sources"][0]: page["path"] for page in pages if page["type"] == "paper"}}
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
                    lines.append(f"- [{page.get('display_title') or page['title']}]({relative}) · {len(page['sources'])} 个来源")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def run(self, source_id: Optional[str] = None) -> Dict[str, Any]:
        """Compile the corpus once for both ingestion and explicit rebuilds.

        ``source_id`` selects the returned paper path, not a partial aggregation:
        entity pages always use the same full verified corpus as a rebuild.
        """
        started = _utc()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        report_path = self.state_dir / "runs" / (run_id + ".json")
        report: Dict[str, Any] = {"id": run_id, "started_at": started, "status": "failed", "raw_unchanged": False}
        try:
            inventory, planned, manifest, sources = self._prepare()
            report.update(inventory, id=run_id, started_at=started)
            if inventory["status"] != "ready":
                raise ValueError("没有可编译的资料，或检测到完整性/文件归属冲突；请查看诊断。")
            if source_id is not None and source_id not in inventory["paper_paths"]:
                raise ValueError("当前资料未通过编译质量门禁，请查看诊断。")
            if any(_file_hash(source.raw) != source.source_id for source in sources):
                raise ValueError("准备期间原始资料发生变化，未提交任何 Wiki 文件。")
            index_path = self.config.wiki_root / "index.md"
            log_path = self.config.wiki_root / "log.md"
            report.update(status="succeeded", finished_at=_utc(), raw_unchanged=True,
                          manifest_path=self._relative(self.manifest_path), index_path=self._relative(index_path), log_path=self._relative(log_path))
            if source_id is not None:
                report["paper_path"] = inventory["paper_paths"][source_id]
            old_log = log_path.read_text(encoding="utf-8") if log_path.exists() else "---\ncompiler_auxiliary: true\ngenerated_by: " + COMPILER + "\n---\n\n# 知识维护记录\n"
            log_entry = f"\n## {report['finished_at']} · {run_id}\n\n- 来源：{len(sources)}；页面：{len(manifest['pages'])}；新增：{len(inventory['changes']['created'])}；更新：{len(inventory['changes']['updated'])}；未变：{len(inventory['changes']['unchanged'])}。\n- 输入：`{manifest['input_digest']}`\n- 内容：`{manifest['content_digest']}`\n- 原始文件哈希保持不变；编译未调用语言模型。\n"
            # Reverified Claim caches share the Wiki rollback boundary: preview
            # does not mutate evidence status.
            planned.update({self.config.root / "data" / "claims" / (source.source_id + ".json"): _json(source.claims) for source in sources})
            planned.update({self.manifest_path: _json(manifest), index_path: self._index(manifest), log_path: old_log.rstrip() + "\n" + log_entry, report_path: _json(report)})
            # WikiManager restores previously replaced files on any write error.
            self.wiki._commit(planned)
            return report
        except Exception as exc:
            report.update(status="failed", finished_at=_utc(), error=str(exc))
            self.wiki._atomic_write(report_path, _json(report))
            return report


def compile_wiki(config: Config, source_id: Optional[str] = None, database: Optional[Database] = None) -> Dict[str, Any]:
    return CompilationService(config, database=database).run(source_id=source_id)


def compilation_status(config: Config) -> Dict[str, Any]:
    service = CompilationService(config)
    return {"inventory": service.inventory(), "manifest": service.manifest(), "history": service.history()}
