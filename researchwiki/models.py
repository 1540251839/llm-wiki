from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Page:
    number: int
    text: str


@dataclass
class DocumentBlock:
    """A parser-owned physical-page span, never a model-supplied locator."""
    block_id: str
    page: int
    section: str
    block_type: str
    text: str
    bbox: Optional[List[float]] = None
    char_start: int = 0
    char_end: int = 0
    parser_version: str = ""


@dataclass
class ParsedDocument:
    text: str
    pages: List[Page] = field(default_factory=list)
    blocks: List[DocumentBlock] = field(default_factory=list)
    quality: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityMention:
    name: str
    description: str = ""
    role: str = ""
    principle: str = ""
    statistics: str = ""
    name_zh: str = ""
    usage_status: str = "mentioned"  # mentioned | training | validation | testing | benchmark


@dataclass
class StructuredPaper:
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    abstract: Optional[str] = None
    abstract_zh: str = ""
    keywords: List[str] = field(default_factory=list)
    experiment_info: str = ""
    extraction_mode: str = "deep"
    research_problem: str = ""
    methodology: str = ""
    methods: List[EntityMention] = field(default_factory=list)
    datasets: List[EntityMention] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    contributions: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    topic_labels_zh: Dict[str, str] = field(default_factory=dict)
    field_evidence: List[Dict[str, Any]] = field(default_factory=list)
    source_path: str = ""
    source_hash: str = ""
    claims: List[Dict[str, Any]] = field(default_factory=list)
    extraction_meta: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StructuredPaper":
        def entities(key: str) -> List[EntityMention]:
            values = data.get(key) or []
            result: List[EntityMention] = []
            for value in values:
                if isinstance(value, str):
                    result.append(EntityMention(name=value))
                elif isinstance(value, dict) and value.get("name"):
                    result.append(EntityMention(
                        name=str(value["name"]),
                        description=str(value.get("description") or ""),
                        role=str(value.get("role") or ""),
                        principle=str(value.get("principle") or ""),
                        statistics=str(value.get("statistics") or ""),
                        name_zh=str(value.get("name_zh") or ""),
                        usage_status=str(value.get("usage_status") or "mentioned"),
                    ))
            return result

        year = data.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        return cls(
            title=str(data.get("title") or "Untitled Paper"),
            authors=[str(x) for x in (data.get("authors") or [])],
            year=year,
            abstract=data.get("abstract"),
            abstract_zh=str(data.get("abstract_zh") or ""),
            keywords=[str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()],
            experiment_info=str(data.get("experiment_info") or ""),
            extraction_mode=str(data.get("extraction_mode") or "deep"),
            research_problem=str(data.get("research_problem") or ""),
            methodology=str(data.get("methodology") or ""),
            methods=entities("methods"),
            datasets=entities("datasets"),
            metrics=[str(x) for x in (data.get("metrics") or [])],
            contributions=[str(x) for x in (data.get("contributions") or [])],
            findings=[str(x) for x in (data.get("findings") or [])],
            limitations=[str(x) for x in (data.get("limitations") or [])],
            topics=[str(x) for x in (data.get("topics") or [])],
            topic_labels_zh={
                str(name).strip(): str(label).strip()
                for name, label in data["topic_labels_zh"].items()
                if str(name).strip() and str(label).strip()
            } if isinstance(data.get("topic_labels_zh"), dict) else {},
            field_evidence=[dict(x) for x in (data.get("field_evidence") or []) if isinstance(x, dict)],
            source_path=str(data.get("source_path") or ""),
            source_hash=str(data.get("source_hash") or ""),
            claims=[dict(value) for value in (data.get("claims") or []) if isinstance(value, dict)],
            extraction_meta=dict(data.get("extraction_meta") or {}) if isinstance(data.get("extraction_meta"), dict) else {},
            quality=dict(data.get("quality") or {}) if isinstance(data.get("quality"), dict) else {},
        )

