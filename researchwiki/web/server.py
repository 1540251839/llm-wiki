from __future__ import annotations

import os

from .app import create_app


def create_server_app():
    """Uvicorn factory used by ``rw web --reload``."""
    return create_app(os.environ.get("RESEARCHWIKI_ROOT", "."))
