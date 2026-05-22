"""Entry point for the web interface.

Configuration is read from environment variables so the same script works for
local development and the supervised production service:

  WEB_HOST    bind address          (default 0.0.0.0)
  WEB_PORT    bind port             (default 8000)
  WEB_RELOAD  autoreload on changes (default off — dev only)
"""

import logging
import os

import uvicorn


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8000"))
    # reload spawns a file-watcher subprocess and is only meant for development.
    # In production it must stay OFF so the process is a single stable server
    # that systemd can supervise and restart cleanly.
    reload = _env_bool("WEB_RELOAD", False)

    # workers MUST stay at 1: the scrape queue, background worker, and shared
    # Playwright browser all live in-process. Multiple workers would each spawn
    # their own copy and break the single-job-at-a-time model.
    uvicorn.run(
        "web.app:app",
        host=host,
        port=port,
        reload=reload,
        workers=1,
        timeout_graceful_shutdown=30,
    )
