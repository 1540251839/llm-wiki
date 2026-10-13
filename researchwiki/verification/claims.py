from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..models import ParsedDocument, StructuredPaper


SCHEMA_VERSION = 1
VERIFIER_VERSION = "source-claims-v2"
STATUSES = {"supported", "contradicted", "insufficient", "pending_review"}


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalized(text: str) -> Tuple[str, List[int]]:
    """Whitespace/line-wrap normalization with an exact source-offset map."""
    chars, offsets = [], []
    for match in re.finditer(r"-\s*\n\s*|\s+|[^\s]", text):
        token = match.group()
        if token.startswith("-") and "\n" in token:
            continue
        replacement = " " if token.isspace() else token
        if replacement == " " and (not chars or chars[-1] == " "):
            continue
        # casefold can expand a single character (e.g. ß); keep offset mapping.
        for char in replacement.casefold():
            chars.append(char)
            offsets.append(match.start())
    if chars and chars[-1] == " ":
        chars.pop(); offsets.pop()
    return "".join(chars), offsets


def locate_quote(document: ParsedDocument, quote: str) -> Optional[Dict[str, Any]]:
    needle = _normalized(str(quote))[0].strip()
    if not needle or len(needle) > 10000:
        return None
    for page in document.pages:
        normalized, positions = _normalized(page.text)
        start = normalized.find(needle)
        if start < 0:
            continue
        first, last = positions[start], positions[start + len(needle) - 1] + 1
        return {"quote": page.text[first:last], "evidence_text": page.text[first:last],
                "page_number": page.number, "page": page.number,
                "char_start": first, "char_end": last,
                "paragraph": len(re.findall(r"\n\s*\n", page.text[:first])) + 1,
                "locator": f"page:{page.number}#chars={first}-{last}",
                "match": "normalized_exact", "located": True}
    return None


def _has_proposition(statement: str, paper_title: str = "") -> bool:
    """Conservatively distinguish a statement from a located keyword/heading.

    This is a syntax gate, not a semantic support judge. Unrecognized sentence
    forms go to review rather than being approved merely because a string occurs
    in the source. A short explicit quantitative relation can still be a fact.
    """
    text = _normalized(statement)[0].strip()
    if not text or text.rstrip(".。:：") == _normalized(paper_title)[0].strip().rstrip(".。:："):
        return False
    if text.endswith(("?", "？")):
        return False
    # A finite English predicate needs both a subject before it and an object/
    # complement after it. Infinitive/gerund phrases such as Training robustness
    # and Learning against noisy labels deliberately do not meet this rule.
    predicates = (
        r"(?:is|are|was|were|has|have|had|can|could|may|might|will|would|must|should|"
        r"reports?|shows?|demonstrates?|finds?|found|reaches?|achieves?|achieved|"
        r"improves?|improved|outperforms?|outperformed|reduces?|reduced|"
        r"increases?|increased|decreases?|decreased|uses?|used|studies|study|"
        r"evaluates?|evaluated|introduces?|introduced|proposes?|proposed|"
        r"supports?|supported|preserves?|preserved|contains?|contained|"
        r"requires?|required|makes?|made|limits?|limited|yields?|yielded|"
        r"performs?|performed|compares?|compared|learns?|learned|links?|linked|"
        r"combines?|combined|connects?|connected|tests?|tested|addresses?|addressed)"
    )
    for match in re.finditer(r"(?<![\w])" + predicates + r"(?=\s|\d)", text, re.I):
        subject, complement = text[:match.start()].strip(), text[match.end():].strip(" .。;；")
        if re.search(r"[a-z\u4e00-\u9fff]", subject, re.I) and re.search(r"[a-z0-9\u4e00-\u9fff]", complement, re.I):
            return True
    # Avoid noun-like Chinese role labels (自动评估/文本生成/重排序/知识库).
    # Verbs require an explicit subject and a nonempty complement; generic
    # nominalizable words such as 训练、评估、生成 are not predicates here.
    chinese = r"(?:是|为|采用|使用|达到|达到了|提升|提高|降低|减少|表明|报告|获得|包含|支持|依赖|提出|解决|优于|构建了|验证了)"
    for match in re.finditer(chinese, text):
        subject, complement = text[:match.start()].strip(), text[match.end():].strip(" .。;；")
        if len(re.sub(r"[^\w\u4e00-\u9fff]", "", subject)) >= 2 and re.search(r"[a-z0-9\u4e00-\u9fff]", complement, re.I):
            return True
    # Metric/value statements are useful even without an English copula.
    return bool(re.match(r"^[a-z\u4e00-\u9fff][^\n=<>:：]{1,80}\s*(?:=|:|：|≥|≤|>|<)\s*[+-]?\d", text, re.I))


def _legacy_claims(paper: StructuredPaper) -> List[Dict[str, Any]]:
    records = []
    def evidence(field: str) -> Dict[str, Any]:
        row = next((row for row in paper.field_evidence if row.get("field") == field and row.get("quote")), None)
        # A matched quotation is only a requested source span, never approval.
        return {"quote": row["quote"]} if row else {}
    for field, kind in (("findings", "finding"), ("contributions", "contribution"), ("limitations", "limitation")):
        records.extend({"statement": text, "claim_type": kind, "origin": "legacy_field",
                        "field": field + "." + str(index), "evidence": evidence(field + "." + str(index))}
                       for index, text in enumerate(getattr(paper, field)) if str(text).strip())
    for field, kind in (("abstract", "abstract"), ("abstract_zh", "abstract"), ("research_problem", "research_problem"),
                        ("methodology", "methodology"), ("experiment_info", "experiment_result")):
        text = getattr(paper, field)
        if text:
            records.append({"statement": str(text), "claim_type": kind, "origin": "legacy_field",
                            "field": field, "evidence": evidence(field)})
    for entity_type, entities in (("method", paper.methods), ("dataset", paper.datasets)):
        for entity in entities:
            for text in dict.fromkeys(value for value in (entity.description, entity.role, entity.principle, entity.statistics) if value):
                records.append({"statement": str(text), "claim_type": entity_type + "_usage", "origin": "legacy_entity",
                                "field": entity_type + "s:" + entity.name, "evidence": evidence(entity_type + "s:" + entity.name),
                                "entities": [{"type": entity_type, "name": entity.name}]})
    return records


def _associations(paper: StructuredPaper, candidate: Dict[str, Any], quote: str = "") -> List[Dict[str, str]]:
    from ..wiki.entities import canonical_entity
    statement = _normalized(str(candidate.get("statement") or ""))[0]
    context = statement + " " + _normalized(quote)[0]
    def mentioned(kind: str, name: str) -> bool:
        entity = canonical_entity(kind, name)
        for value in [name, entity["name"], *entity.get("aliases", [])]:
            term = _normalized(value)[0]
            if term and (re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", context)
                         if re.search(r"[a-z]", term) else term in context):
                return True
        return False
    result = [{"type": "paper", "name": paper.title}]
    for value in candidate.get("entities") or []:
        if isinstance(value, str):
            value = {"type": "topic", "name": value}
        if (isinstance(value, dict) and value.get("name") and value.get("type") in {"method", "dataset", "topic"}
                and mentioned(value["type"], value["name"])):
            result.append({"type": str(value["type"]), "name": str(value["name"]).strip()})
    # Legacy links are conservative: a global finding belongs to an entity only
    # when that entity is actually named in the statement. Never copy all findings.
    for kind, names in (("method", [entity.name for entity in paper.methods]),
                        ("dataset", [entity.name for entity in paper.datasets]), ("topic", paper.topics)):
        for name in names:
            term = _normalized(name)[0]
            matched = bool(term and (re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", statement)
                                     if re.search(r"[a-z]", term) else term in statement))
            if matched:
                result.append({"type": kind, "name": name})
    return list({(item["type"], item["name"].casefold()): item for item in result}.values())


def verify_paper(paper: StructuredPaper, document: ParsedDocument, source_id: str,
                 existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    serial = paper.to_dict()
    serial.pop("source_path", None)
    # Timings/cache counters describe execution, not a new factual extraction.
    # A checkpoint resume must not revoke unchanged human decisions solely
    # because this attempt was faster or reused more completed blocks.
    metadata = dict(serial.get("extraction_meta") or {})
    for key in ("elapsed_ms", "completed_chunk_count", "cached_chunk_count"):
        metadata.pop(key, None)
    serial["extraction_meta"] = metadata
    extraction_digest = content_digest(serial)
    same_extraction = bool(existing and existing.get("source_id") == source_id
                           and existing.get("extraction_digest") == extraction_digest)
    generation_revision = paper.extraction_meta.get("generation_revision")
    supplement_compatible = bool(existing and paper.extraction_meta.get("last_supplement")
        and existing.get("source_id") == source_id and existing.get("verifier_version") == VERIFIER_VERSION
        and existing.get("generation_revision") == generation_revision)
    old = {item.get("claim_id"): item for item in (existing or {}).get("claims", [])}
    # Offline metadata and raw abstracts are discovery objects, not factual
    # interpretations. Never infer claims/approval during basic import.
    candidates = [] if paper.extraction_mode == "basic" else list(getattr(paper, "claims", []) or []) + _legacy_claims(paper)
    claims = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        statement = str(candidate.get("statement") or "").strip()
        if not statement:
            continue
        kind = str(candidate.get("claim_type") or "finding")
        claim_id = "claim:" + content_digest([source_id, kind, _normalized(statement)[0]])[:24]
        if claim_id in claims:
            continue
        # A correction is bound to the original extraction version, not merely
        # its display id. Re-extraction cannot inherit a stale human approval.
        correction = old.get(claim_id, {}).get("correction") or {}
        previous_record = old.get(claim_id, {})
        same_attributes = content_digest(previous_record.get("attributes") or {}) == content_digest(candidate.get("attributes") or {})
        if (same_extraction or (supplement_compatible and same_attributes)) and correction.get("original_statement_hash") == content_digest(statement):
            statement = str(correction.get("statement") or statement).strip()
            candidate = {**candidate, "statement": statement, "evidence": {"quote": correction.get("quote", "")}}
        else:
            correction = {}
        supplied = candidate.get("evidence") or {}
        if isinstance(supplied, list):
            supplied = supplied[0] if supplied else {}
        if not isinstance(supplied, dict):
            supplied = {}
        requested_quote = str(supplied.get("quote") or candidate.get("quote") or "")
        located = locate_quote(document, requested_quote or statement)
        # LLM-supplied page/character offsets are deliberately discarded.
        evidence = {"source_id": source_id, **(located or {
            "quote": requested_quote, "evidence_text": requested_quote,
            "page_number": None, "page": None, "char_start": None, "char_end": None,
            "locator": "", "paragraph": None, "located": False, "match": "not_found"})}
        literal_statement = _normalized(statement)[0]
        literal_quote = _normalized(evidence["quote"])[0]
        extractive = bool(located and literal_statement in literal_quote)
        proposition = _has_proposition(statement, paper.title)
        numbers = re.findall(r"(?<![\w])\d+(?:\.\d+)?\s*%?", statement)
        numeric_ok = bool(located) and all(re.sub(r"\s+", "", n) in re.sub(r"\s+", "", evidence["quote"]) for n in numbers)
        status = "supported" if extractive and numeric_ok and proposition else ("pending_review" if located else "insufficient")
        reason = "原文中的逐字陈述；仅验证原文包含该陈述，不证明论文结论为真。" if status == "supported" else (
            ("仅关键词、裸词组或标题命中，缺少完整事实陈述；需补充原文支持的主谓陈述并审核。" if not proposition else
             "已定位引文，但概括与引文的语义支持关系需要人工审核。") if located else "无法在原文定位完整引文；禁止作为已验证事实编译。")
        evidence_hash = content_digest({key: evidence.get(key) for key in ("source_id", "quote", "page_number", "char_start", "char_end")})
        statement_hash = content_digest(statement)
        verification = {"status": status, "method": "deterministic_extractive" if status == "supported" else "requires_review",
                        "reason": reason, "quote_located": bool(located), "numeric_consistent": numeric_ok if numbers else None,
                        "proposition": proposition, "proposition_gate": "conservative_syntax_v1",
                        "statement_hash": statement_hash, "evidence_hash": evidence_hash, "verifier_version": VERIFIER_VERSION}
        previous = old.get(claim_id, {}).get("verification") or {}
        associations = _associations(paper, candidate, evidence["quote"] if located else "")
        stable_claim = bool(supplement_compatible and same_attributes
                            and content_digest(previous_record.get("entities") or []) == content_digest(associations)
                            and previous.get("verifier_version") == VERIFIER_VERSION)
        if ((same_extraction or stable_claim) and previous.get("method") == "human_review" and previous.get("statement_hash") == statement_hash
                and previous.get("evidence_hash") == evidence_hash and previous.get("status") in STATUSES
                and (previous.get("status") != "supported" or bool(located))):
            verification = copy.deepcopy(previous)
        claims[claim_id] = {"claim_id": claim_id, "source_id": source_id, "subject": str(candidate.get("subject") or paper.title),
                            "claim_type": kind, "statement": statement, "attributes": candidate.get("attributes") or {},
                            "entities": associations,
                            "association_verification": "explicit_source_mention", "evidence": evidence, "verification": verification,
                            "origin": str(candidate.get("origin") or "model_extraction")}
        if correction:
            claims[claim_id]["correction"] = copy.deepcopy(correction)
    return {"schema_version": SCHEMA_VERSION, "verifier_version": VERIFIER_VERSION,
            "source_id": source_id, "source_hash": source_id, "extraction_digest": extraction_digest,
            "generation_revision": generation_revision,
            "claims": list(claims.values()), "summary": dict(Counter(item["verification"]["status"] for item in claims.values())),
            "audit": copy.deepcopy((existing or {}).get("audit", []))}


class ClaimsService:
    def __init__(self, config: Config):
        self.config = config

    def path(self, source_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", source_id):
            raise ValueError("Invalid source hash")
        return self.config.root / "data" / "claims" / (source_id + ".json")

    def load(self, source_id: str) -> Dict[str, Any]:
        path = self.path(source_id)
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("source_id") != source_id:
            raise ValueError("Invalid claim bundle")
        if not isinstance(value.get("claims"), list) or any(not isinstance(item, dict) for item in value["claims"]):
            raise ValueError("Invalid claims array")
        return value

    @staticmethod
    def write(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".claims-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def prepare(self, source_id: str, paper: StructuredPaper, document: ParsedDocument, persist: bool = True) -> Dict[str, Any]:
        bundle = verify_paper(paper, document, source_id, existing=self.load(source_id))
        if persist:
            self.write(self.path(source_id), bundle)
        return bundle

    def review(self, source_id: str, claim_id: str, status: str, reason: str,
               statement: Optional[str] = None, quote: Optional[str] = None) -> Dict[str, Any]:
        if status not in STATUSES or not reason.strip():
            raise ValueError("审核必须提供有效状态和理由。")
        from ..index.database import Database
        from ..parsing.pdf import parse_file
        database = Database(self.config.db_path)
        try:
            paper_record = database.get_paper(source_id)
        finally:
            database.close()
        if not paper_record or paper_record.get("status") == "DELETED":
            raise FileNotFoundError("Source not found")
        raw = Path(str(paper_record.get("raw_path") or ""))
        if hashlib.sha256(raw.read_bytes()).hexdigest() != source_id:
            raise ValueError("原始文件哈希不符，不能确认事实。")
        paper = StructuredPaper.from_dict(json.loads((self.config.root / "data" / "extractions" / (source_id + ".json")).read_text(encoding="utf-8")))
        bundle = self.prepare(source_id, paper, parse_file(raw), persist=False)
        claim = next((item for item in bundle["claims"] if item["claim_id"] == claim_id), None)
        if claim is None:
            raise FileNotFoundError("Claim not found")
        before_claim = copy.deepcopy(claim)
        prior = copy.deepcopy(claim.get("verification"))
        # Corrections are applied to structured extraction, then independently
        # rebound to the source. Neither existing evidence offsets nor overrides
        # can grant support for a quote that does not exist.
        if statement is not None or quote is not None:
            if statement is not None and not statement.strip():
                raise ValueError("事实陈述不能为空。")
            fixed = {**claim, "statement": statement if statement is not None else claim["statement"],
                     "evidence": {"quote": quote if quote is not None else claim["evidence"]["quote"]}}
            corrected = verify_paper(StructuredPaper.from_dict({**paper.to_dict(), "claims": [fixed],
                "findings": [], "contributions": [], "limitations": [], "abstract": None,
                "methodology": "", "research_problem": "", "methods": [], "datasets": []}), parse_file(raw), source_id)["claims"][0]
            # Keep identity stable for this reviewed fact; remember the correction
            # in its extraction input for future deterministic recompilation.
            corrected["claim_id"] = claim_id
            claim.update(corrected)
        if status == "supported" and not claim["evidence"].get("located"):
            raise ValueError("证据不在原文中，无法确认 supported。")
        verification = claim["verification"]
        verification.update(status=status, method="human_review", reason=reason.strip(),
                            reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            reviewer="local_user")
        bundle.setdefault("audit", []).append({"claim_id": claim_id, "before": prior, "after": copy.deepcopy(verification),
                                              "before_claim": before_claim, "after_claim": copy.deepcopy(claim)})
        bundle["summary"] = dict(Counter(item["verification"]["status"] for item in bundle["claims"]))
        # No corrected factual prose is written into immutable raw files.
        if statement is not None or quote is not None:
            claim["correction"] = {"statement": claim["statement"], "quote": claim["evidence"]["quote"],
                                   "original_statement_hash": content_digest(next(item["statement"] for item in verify_paper(paper, parse_file(raw), source_id)["claims"] if item["claim_id"] == claim_id))}
        self.write(self.path(source_id), bundle)
        return claim
