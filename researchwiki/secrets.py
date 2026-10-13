"""Local-only secret persistence for the standalone research workbench.

Only used for explicitly submitted DeepSeek keys. Never return or log a secret.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def secret_path(root: Path) -> Path:
    return root / "data" / "private" / "deepseek_api_key"


def read_deepseek_key(root: Path) -> Optional[str]:
    path = secret_path(root)
    if path.is_symlink():
        return None
    try:
        # Do not trust a secret file readable by other local users on POSIX.
        if os.name == "posix" and (path.stat().st_mode & 0o077):
            return None
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value if value else None


def save_deepseek_key(root: Path, value: str) -> None:
    directory = secret_path(root).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".deepseek-", dir=str(directory))
    temp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            temp.chmod(0o600)
        os.replace(temp, secret_path(root))
    finally:
        temp.unlink(missing_ok=True)


def delete_deepseek_key(root: Path) -> None:
    path = secret_path(root)
    if path.is_symlink():
        raise ValueError("Refusing to remove a symbolic-link secret file")
    path.unlink(missing_ok=True)
