"""Logging owned by the application process, including uvicorn reload workers."""
from __future__ import annotations

import logging
import sys


def configure_progress_logging() -> None:
    """Show useful ingest progress without HTTP polling and file-watcher noise."""
    package = logging.getLogger("sparser")
    package.setLevel(logging.INFO)
    package.propagate = False

    # Reload imports the API in a fresh process, so configure a package-local
    # handler instead of relying on basicConfig in the launcher process.
    if not any(getattr(h, "_sparser_progress", False) for h in package.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._sparser_progress = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)s  %(message)s", datefmt="%H:%M:%S"
        ))
        package.addHandler(handler)

    # These are implementation chatter, not user-visible progress. Uvicorn's
    # access log is separately disabled in server.py.
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)
