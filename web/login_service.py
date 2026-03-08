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


def _get_cookie_expiry(platform: str) -> str | None:
    """Read the session file and return the expiry ISO string of the key auth cookie."""
    path = _SESSION_PATHS.get(platform)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cookie_name = {"instagram": "ds_user_id", "facebook": "c_user", "tiktok": "sessionid"}.get(platform)
        if not cookie_name:
            return None
        for cookie in data.get("cookies", []):
            if cookie.get("name") == cookie_name:
                expires = cookie.get("expires")
                if expires and isinstance(expires, (int, float)) and expires > 0:
                    from datetime import datetime, timezone
                    return datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
        return None
    except Exception:
        return None


def get_session_status() -> dict:
    return {
        "instagram": session_exists("instagram"),
        "facebook": session_exists("facebook"),
        "tiktok": session_exists("tiktok"),
        "expires": {
            "instagram": _get_cookie_expiry("instagram"),
            "facebook": _get_cookie_expiry("facebook"),
            "tiktok": _get_cookie_expiry("tiktok"),
        },
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
