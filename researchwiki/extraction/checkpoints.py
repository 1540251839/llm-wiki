"""Private, version-bound local checkpoints for completed extraction requests.

Only a validated complete result or a deterministic split plan is persisted.
The envelope checksum detects accidental partial writes/corruption; it is not a
security signature. Do not accept checkpoint files from untrusted parties.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Optional, Tuple


CHECKPOINT_VERSION = "request-checkpoints-1"


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_response_meta(value: Any) -> Dict[str, Any]:
    """Never forward arbitrary provider response bodies, prompts or credentials."""
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {}
    finish = value.get("finish_reason")
    if isinstance(finish, str):
        result["finish_reason"] = finish[:80]
    for name in ("elapsed_seconds", "total_elapsed_ms", "request_attempts", "requests", "attempts", "json_attempts", "json_retries"):
        item = value.get(name)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[name] = item
    for key in ("usage", "usage_totals"):
        usage = value.get(key)
        if isinstance(usage, dict):
            result[key] = {name: item for name, item in usage.items()
                           if name in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
                           and isinstance(item, int) and not isinstance(item, bool)}
    return result


class ChunkCheckpoints:
    def __init__(self, directory: Optional[Path]):
        self.directory = Path(directory) if directory is not None else None

    def key(self, identity: Dict[str, Any]) -> str:
        return digest({"checkpoint_version": CHECKPOINT_VERSION, "identity": identity})

    def path(self, stage: str, key: str) -> Path:
        if self.directory is None:
            raise ValueError("Checkpoint directory is not configured")
        if stage not in {"analysis", "extraction"} or len(key) != 64 or any(value not in "0123456789abcdef" for value in key):
            raise ValueError("Invalid checkpoint identity")
        return self.directory / (stage + "_" + key + ".json")

    def read(self, stage: str, identity: Dict[str, Any],
             validator: Callable[[Any], bool]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return (entry, state): hit/miss/invalid. Invalid data is never reused."""
        if self.directory is None:
            return None, "miss"
        key = self.key(identity)
        path = self.path(stage, key)
        if not path.exists():
            return None, "miss"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            entry = envelope.get("entry")
            if (envelope.get("version") != CHECKPOINT_VERSION or envelope.get("key") != key
                    or envelope.get("identity") != identity or not isinstance(entry, dict)
                    or envelope.get("entry_digest") != digest(entry) or not validator(entry)):
                return None, "invalid"
            return entry, "hit"
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            return None, "invalid"

    def write(self, stage: str, identity: Dict[str, Any], entry: Dict[str, Any]) -> None:
        if self.directory is None:
            return
        key = self.key(identity)
        path = self.path(stage, key)
        envelope = {"version": CHECKPOINT_VERSION, "key": key, "identity": identity,
                    "entry": entry, "entry_digest": digest(entry)}
        # Serialize before opening a temporary file. Invalid/non-finite output
        # cannot become a success checkpoint or damage an existing checkpoint.
        content = json.dumps(envelope, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=str(self.directory))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


def summarize_checkpoints(directory: Path) -> Dict[str, Any]:
    """Count locally valid files, not current-run progress across namespaces."""
    counts = {"analysis": 0, "extraction": 0, "split_plans": 0, "invalid": 0}
    path = Path(directory)
    if path.exists():
        for item in path.glob("*.json"):
            try:
                envelope = json.loads(item.read_text(encoding="utf-8"))
                entry = envelope.get("entry")
                stage = envelope["identity"]["stage"]
                if (envelope.get("version") != CHECKPOINT_VERSION or stage not in {"analysis", "extraction"}
                        or not isinstance(entry, dict) or envelope.get("entry_digest") != digest(entry)
                        or envelope.get("key") != digest({"checkpoint_version": CHECKPOINT_VERSION, "identity": envelope["identity"]})):
                    counts["invalid"] += 1
                elif entry.get("type") == "split":
                    counts["split_plans"] += 1
                elif entry.get("type") == "result":
                    counts[stage] += 1
                else:
                    counts["invalid"] += 1
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
                counts["invalid"] += 1
    return {"completed": {"analysis": counts["analysis"], "extraction": counts["extraction"]},
            "split_plans": counts["split_plans"], "invalid": counts["invalid"],
            "scope": "all_local_configurations_not_current_run"}
