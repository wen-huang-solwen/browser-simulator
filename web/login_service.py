"""Session file upload and status helpers."""

import json
import logging
import os

from config import AUTH_DIR, SESSION_FILE, FB_SESSION_FILE, TK_SESSION_FILE
from auth.session_manager import session_exists

logger = logging.getLogger(__name__)

_SESSION_PATHS = {
    "instagram": SESSION_FILE,
    "facebook": FB_SESSION_FILE,
    "tiktok": TK_SESSION_FILE,
}


def get_session_status() -> dict:
    return {
        "instagram": session_exists("instagram"),
        "facebook": session_exists("facebook"),
        "tiktok": session_exists("tiktok"),
    }


def save_uploaded_session(platform: str, content: bytes) -> None:
    # Validate it's valid JSON with expected structure
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Invalid session file: expected a JSON object")
    if "cookies" not in data:
        raise ValueError("Invalid session file: missing 'cookies' key")

    os.makedirs(AUTH_DIR, exist_ok=True)
    path = _SESSION_PATHS.get(platform)
    if not path:
        raise ValueError(f"Unknown platform: {platform}")

    with open(path, "w") as f:
        json.dump(data, f)
    logger.info("Uploaded session saved to %s", path)
