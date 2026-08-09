"""Launcher for the FastAPI app.

Binds to loopback by default. This serves unredacted financial history, so it
must not be exposed on a routable interface without authentication in front.
"""
from __future__ import annotations

import os
import logging
import threading
import webbrowser
from pathlib import Path


def serve(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8770,
    open_browser: bool = True,
    inbox: Path | None = None,
    reload: bool = False,
) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # The app reads these at import time, so they must be set before it loads.
    # Keep the API and reload worker pinned to one database even if uvicorn or a
    # caller changes its working directory. A relative path can otherwise make
    # a successful save disappear on the next process start.
    db_path = Path(db_path).expanduser().resolve()
    os.environ["SPARSER_DB"] = str(db_path)
    if inbox:
        os.environ["SPARSER_INBOX"] = str(inbox)

    from .api import WEB_DIST

    url = f"http://{host}:{port}/"
    print(f"sparser on {url}   api docs: {url}docs   db: {db_path}")
    if not WEB_DIST.exists():
        print("  ! UI not built — run:  cd webapp && npm install && npm run build")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "sparser.api:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
        access_log=False,
        reload_excludes=(
            ["*.db", "*.db-wal", "*.db-shm", "inbox/*", "sparser/web/dist/*", "webapp/dist/*"]
            if reload else None
        ),
    )
