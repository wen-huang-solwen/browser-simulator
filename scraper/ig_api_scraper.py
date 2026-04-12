"""Instagram reels scraper via instagrapi (private mobile API).

Replaces the Playwright-based grid scroll + per-reel page visits with a
single paginated API call. Reuses the existing Playwright session file by
extracting the sessionid cookie from its storage_state.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from config import AUTH_DIR, INSTAGRAM_BASE, SESSION_FILE, USER_AGENT

# Cache file for username -> user_id mappings to avoid repeated API lookups.
_USER_ID_CACHE_FILE = os.path.join(AUTH_DIR, "ig_user_id_cache.json")

logger = logging.getLogger(__name__)

# Public-facing web App ID that Instagram's own JS uses for api/v1/users/web_profile_info/.
# Without this header the endpoint returns 400.
IG_WEB_APP_ID = "936619743392459"


def _extract_ig_cookies(session_path: str) -> dict[str, str]:
    """Pull all instagram.com cookies out of a Playwright storage_state file."""
    if not os.path.isfile(session_path):
        raise RuntimeError(
            "Instagram session file not found. "
            "Please upload one via the dashboard."
        )
    with open(session_path, encoding="utf-8") as f:
        state = json.load(f)

    cookies: dict[str, str] = {}
    for cookie in state.get("cookies", []):
        if "instagram.com" not in (cookie.get("domain") or ""):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value:
            cookies[name] = value

    if "sessionid" not in cookies:
        raise RuntimeError(
            "Instagram session is missing the 'sessionid' cookie. "
            "Please re-upload a fresh session file."
        )
    return cookies


def _media_to_row(media, now_iso: str) -> dict:
    """Map an instagrapi Media object to our scraper dict shape."""
    code = getattr(media, "code", "") or ""
    taken_at = getattr(media, "taken_at", None)
    timestamp = None
    if taken_at is not None:
        if hasattr(taken_at, "isoformat"):
            timestamp = taken_at.isoformat()
        else:
            timestamp = str(taken_at)

    # Reels use play_count for view counts; fall back to view_count if present.
    views = getattr(media, "play_count", None)
    if views is None:
        views = getattr(media, "view_count", None)

    return {
        "url": f"{INSTAGRAM_BASE}/reel/{code}/" if code else "",
        "shortcode": code,
        "views": views,
        "likes": getattr(media, "like_count", None),
        "comments": getattr(media, "comment_count", None),
        "caption": getattr(media, "caption_text", "") or "",
        "timestamp": timestamp,
        "scraped_at": now_iso,
    }


def _patch_instagrapi_extractors() -> None:
    """Guard against instagrapi 2.3.0's extract_broadcast_channel KeyError when
    the API response omits 'pinned_channels_info'. Idempotent.
    """
    from instagrapi import extractors
    if getattr(extractors.extract_broadcast_channel, "_patched", False):
        return
    orig = extractors.extract_broadcast_channel

    def safe(data):
        try:
            return orig(data)
        except (KeyError, TypeError):
            return None

    safe._patched = True
    extractors.extract_broadcast_channel = safe


def _load_user_id_cache() -> dict[str, str]:
    """Load the username -> user_id cache from disk."""
    if not os.path.isfile(_USER_ID_CACHE_FILE):
        return {}
    try:
        with open(_USER_ID_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_user_id_cache(cache: dict[str, str]) -> None:
    """Persist the username -> user_id cache to disk."""
    os.makedirs(os.path.dirname(_USER_ID_CACHE_FILE), exist_ok=True)
    with open(_USER_ID_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _validate_session(cookies: dict[str, str]) -> None:
    """Quick check that the session is still accepted by Instagram.

    Hits an authenticated endpoint with redirects disabled. If Instagram
    redirects to /accounts/login/, the session has been invalidated
    server-side. This is important because web_profile_info returns 429
    even for expired sessions, which would otherwise cause misleading
    "rate limited" errors.
    """
    resp = requests.get(
        "https://www.instagram.com/api/v1/accounts/edit/web_form_data/",
        cookies=cookies,
        headers={
            "User-Agent": USER_AGENT,
            "X-IG-App-ID": IG_WEB_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{INSTAGRAM_BASE}/",
        },
        timeout=15,
        allow_redirects=False,
    )
    location = resp.headers.get("Location", "")
    if resp.status_code in (401, 403) or "accounts/login" in location:
        raise RuntimeError(
            "Instagram session is invalid or expired. "
            "Please upload a new session file via the dashboard "
            "or run `python main.py <username> --login`."
        )
    # 200 or 429 here means the session is still recognised.


def _user_id_from_search(username: str, cookies: dict[str, str]) -> str | None:
    """Look up a user's numeric ID via IG's top search endpoint.

    This is more resilient than web_profile_info which gets IP-rate-limited
    aggressively. Returns None if the search doesn't find an exact match.
    """
    resp = requests.get(
        "https://www.instagram.com/api/v1/web/search/topsearch/",
        params={"query": username, "context": "user", "count": 5},
        cookies=cookies,
        headers={
            "User-Agent": USER_AGENT,
            "X-IG-App-ID": IG_WEB_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{INSTAGRAM_BASE}/",
        },
        timeout=15,
        allow_redirects=False,
    )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        for entry in data.get("users", []):
            user = entry.get("user", {})
            if user.get("username", "").lower() == username.lower():
                pk = user.get("pk") or user.get("pk_id")
                if pk:
                    return str(pk)
    except (KeyError, ValueError):
        pass
    return None


def _user_id_from_username(username: str, cookies: dict[str, str]) -> str:
    """Look up a user's numeric ID via multiple IG endpoints with fallbacks.

    Order: local cache -> web_profile_info -> topsearch.
    Validates the session on first 429 so expired sessions fail fast.
    """
    # Check cache first to avoid hitting the API unnecessarily.
    cache = _load_user_id_cache()
    if username in cache:
        logger.info("Using cached user ID %s for @%s", cache[username], username)
        return cache[username]

    # --- Attempt 1: web_profile_info (most reliable when not rate-limited) ---
    resp = requests.get(
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        params={"username": username},
        cookies=cookies,
        headers={
            "User-Agent": USER_AGENT,
            "X-IG-App-ID": IG_WEB_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{INSTAGRAM_BASE}/{username}/",
        },
        timeout=15,
    )

    if resp.status_code == 200:
        try:
            user = resp.json()["data"]["user"]
        except (KeyError, ValueError) as e:
            raise RuntimeError(f"Unexpected response when looking up @{username}") from e
        if not user:
            raise ValueError(f"Account '{username}' not found or not accessible")
        uid = str(user["id"])
        cache[username] = uid
        _save_user_id_cache(cache)
        return uid

    if resp.status_code == 404:
        raise ValueError(f"Account '{username}' not found or not accessible")
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Instagram session is invalid or expired. "
            "Please upload a new session file via the dashboard "
            "or run `python main.py <username> --login`."
        )

    # --- Attempt 2: topsearch fallback (works when web_profile_info is 429) ---
    if resp.status_code == 429:
        _validate_session(cookies)
        logger.info("web_profile_info rate-limited, trying search fallback for @%s", username)
        uid = _user_id_from_search(username, cookies)
        if uid:
            logger.info("Found user ID %s via search for @%s", uid, username)
            cache[username] = uid
            _save_user_id_cache(cache)
            return uid

        # Search didn't find an exact match — retry web_profile_info with backoff
        backoff = 5
        for attempt in range(3):
            wait = backoff * (2 ** attempt)
            logger.warning(
                "Rate limited (429) looking up @%s, retrying in %ds (attempt %d/3)",
                username, wait, attempt + 1,
            )
            time.sleep(wait)
            resp = requests.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/",
                params={"username": username},
                cookies=cookies,
                headers={
                    "User-Agent": USER_AGENT,
                    "X-IG-App-ID": IG_WEB_APP_ID,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{INSTAGRAM_BASE}/{username}/",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                try:
                    user = resp.json()["data"]["user"]
                except (KeyError, ValueError) as e:
                    raise RuntimeError(f"Unexpected response when looking up @{username}") from e
                if not user:
                    raise ValueError(f"Account '{username}' not found or not accessible")
                uid = str(user["id"])
                cache[username] = uid
                _save_user_id_cache(cache)
                return uid
            if resp.status_code != 429:
                break

    raise RuntimeError(
        f"Could not look up @{username}. Instagram is rate-limiting this server. "
        f"Please wait a few minutes and try again."
    )


def _scrape_reels_sync(username: str, max_reels: int, cookies: dict[str, str]) -> list[dict]:
    """Blocking instagrapi calls. Invoked from a worker thread."""
    _patch_instagrapi_extractors()

    from instagrapi import Client
    from instagrapi.exceptions import ClientError

    # Bypass Client.login_by_sessionid — in instagrapi 2.3.0 it verifies the
    # session via user_info_v1 and then a GraphQL fallback, both of which have
    # bugs / broken IG endpoints. Wire up auth directly using the full cookie
    # set from the Playwright session (sessionid alone is insufficient for the
    # private API — csrftoken, mid, ig_did are all checked).
    sessionid = cookies["sessionid"]
    m = re.match(r"^(\d+)", sessionid)
    if not m:
        raise RuntimeError(
            "Could not parse user_id from sessionid. Please re-upload a fresh session file."
        )
    self_user_id = m.group(1)

    cl = Client()
    cl.settings["cookies"] = dict(cookies)
    cl.settings["authorization_data"] = {
        "ds_user_id": self_user_id,
        "sessionid": sessionid,
        "should_use_header_over_cookies": True,
    }
    cl.init()
    cl.authorization_data = {
        "ds_user_id": self_user_id,
        "sessionid": sessionid,
        "should_use_header_over_cookies": True,
    }
    cl.cookie_dict["ds_user_id"] = self_user_id

    # Username -> user_id via the web endpoint (instagrapi's lookups are broken).
    user_id = _user_id_from_username(username, cookies)

    logger.info("Fetching reels for @%s via API (max=%d)", username, max_reels)

    collected: list = []
    end_cursor = ""
    page = 0
    while len(collected) < max_reels:
        page += 1
        try:
            medias_page, end_cursor = cl.user_clips_paginated_v1(
                user_id, amount=max_reels, end_cursor=end_cursor
            )
        except ClientError as e:
            if "429" in str(e):
                wait = 5 * (2 ** min(page - 1, 4))
                logger.warning(
                    "Rate limited (429) on page %d, retrying in %ds", page, wait
                )
                time.sleep(wait)
                page -= 1  # retry the same page
                continue
            logger.error("API error on page %d: %s", page, e)
            break

        if not medias_page:
            break
        collected.extend(medias_page)
        # Mimic the "Scroll N: found M total reels" log pattern so the
        # dashboard's progress handler picks it up.
        logger.info(
            "Scroll %d: found %d total reels (%d new)",
            page, len(collected), len(medias_page),
        )
        if not end_cursor:
            break

    collected = collected[:max_reels]
    now = datetime.now(timezone.utc).isoformat()
    results = [_media_to_row(m, now) for m in collected]
    logger.info("Collected %d reels via API", len(results))
    return results


async def scrape_account_reels_api(username: str, max_reels: int) -> list[dict]:
    """Scrape an Instagram account's reels via instagrapi's mobile API."""
    cookies = _extract_ig_cookies(SESSION_FILE)
    return await asyncio.to_thread(_scrape_reels_sync, username, max_reels, cookies)
