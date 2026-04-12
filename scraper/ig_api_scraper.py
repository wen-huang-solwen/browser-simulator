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
from datetime import datetime, timezone

import requests

from config import INSTAGRAM_BASE, SESSION_FILE, USER_AGENT

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


def _user_id_from_username(username: str, cookies: dict[str, str]) -> str:
    """Look up a user's numeric ID via IG's web_profile_info endpoint.

    We don't use instagrapi.user_id_from_username because its GraphQL path 400s
    and its private /usernameinfo/ fallback 403s on web-captured sessions.
    """
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
    if resp.status_code == 404:
        raise ValueError(f"Account '{username}' not found or not accessible")
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Instagram session is invalid or expired. "
            "Please upload a new session file."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up @{username}: HTTP {resp.status_code}")

    try:
        user = resp.json()["data"]["user"]
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Unexpected response when looking up @{username}") from e
    if not user:
        raise ValueError(f"Account '{username}' not found or not accessible")
    return str(user["id"])


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
