"""Conservative entity identity: explicit aliases, never similarity matching."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional

from .manager import normalize_name


ENTITY_TYPES = {"method", "dataset", "topic"}
DEFAULT_ALIASES = [
    {
        "type": "method", "name": "Dempster–Shafer theory",
        "aliases": ["Dempster-Shafer theory", "Dempster–Shafer theory", "Dempster Shafer theory",
                    "D-S证据理论", "D-S 证据理论", "Dempster-Shafer 证据理论", "Dempster–Shafer 证据理论"],
    },
    {"type": "dataset", "name": "LeCaRD", "aliases": ["LeCaRD"]},
    {"type": "dataset", "name": "ELAM", "aliases": ["ELAM"]},
]


def entity_id(kind: str, name: str) -> str:
    return kind + ":" + hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()[:16]


class EntityRegistry:
    """Only an explicitly declared alias can select another display name.

    Caller-supplied canonical IDs are not accepted from model extractions. The
    registry derives identity from the registered name and keeps entity types
    separate, so a dataset and a method with the same name cannot be merged.
    """

    def __init__(self, aliases: Optional[Iterable[Dict[str, Any]]] = None):
        self._lookup: Dict[tuple, Dict[str, Any]] = {}
        self._seen: Dict[str, Dict[str, Any]] = {}
        for record in [*DEFAULT_ALIASES, *(list(aliases) if aliases is not None else [])]:
            kind, name = str(record.get("type") or ""), str(record.get("name") or "").strip()
            if kind not in ENTITY_TYPES or not name:
                raise ValueError("实体别名必须指定 method、dataset 或 topic 类型及非空名称。")
            values = list(dict.fromkeys([name, *[str(value).strip() for value in record.get("aliases", []) if str(value).strip()]]))
            entity = {"type": kind, "name": name, "canonical_id": entity_id(kind, name), "aliases": values}
            for value in values:
                key = (kind, normalize_name(value))
                old = self._lookup.get(key)
                if old and old["canonical_id"] != entity["canonical_id"]:
                    raise ValueError("同类型实体的显式别名发生冲突：" + value)
                self._lookup[key] = entity

    def resolve(self, kind: str, name: str) -> Dict[str, Any]:
        name = str(name).strip()
        if kind not in ENTITY_TYPES or not name:
            raise ValueError("不支持的实体类型或空名称。")
        record = self._lookup.get((kind, normalize_name(name)))
        if record is None:
            record = {"type": kind, "name": name, "canonical_id": entity_id(kind, name), "aliases": [name]}
            self._lookup[(kind, normalize_name(name))] = record
        self._seen[record["canonical_id"]] = record
        return {**record, "aliases": list(record["aliases"])}

    def records(self) -> List[Dict[str, Any]]:
        return [self._seen[key] for key in sorted(self._seen)]


def canonical_entity(kind: str, name: str) -> Dict[str, Any]:
    return EntityRegistry().resolve(kind, name)
