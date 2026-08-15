"""Dependency-free loader for the project's ignored local ``.env`` file."""
from __future__ import annotations

import os
from pathlib import Path


def load_local_env(path: str | Path = ".env") -> None:
    source = Path(path)
    if not source.exists() and str(path) == ".env":
        source = Path(__file__).resolve().parent.parent / ".env"
    if not source.exists():
        return
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)
