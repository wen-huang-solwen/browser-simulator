"""Core scraping logic for TikTok videos using yt-dlp.

yt-dlp handles TikTok's anti-bot protections and extracts video metadata
including view counts, likes, comments, shares, and captions.
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from config import TK_PROXY, TK_SESSION_FILE, TIKTOK_BASE

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds between retries


async def scrape_tk_videos(
    context,  # unused, kept for API compatibility
    username: str,
    max_videos: int,
    with_details: bool = False,
    debug: bool = False,
    pw=None,  # unused, kept for API compatibility
) -> list[dict]:
    """Scrape TikTok videos from a user profile using yt-dlp.

    Runs the playlist scrape up to MAX_RETRIES times to work around
    TikTok's non-deterministic geo-filtering, merging results across
    attempts.
    """
    username_clean = username.lstrip("@")
    profile_url = f"{TIKTOK_BASE}/@{username_clean}"
    logger.info("Fetching TikTok videos for @%s via yt-dlp", username_clean)

    all_entries: dict[str, dict] = {}  # vid -> entry

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await asyncio.to_thread(
                _run_ytdlp, profile_url, max_videos
            )
        except Exception as e:
            logger.error("yt-dlp attempt %d failed: %s", attempt, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            continue

        entries = result.get("entries") or []
        new_count = 0
        for entry in entries:
            vid = entry.get("id", "")
            if vid and vid not in all_entries:
                all_entries[vid] = entry
                new_count += 1

        logger.info(
            "yt-dlp attempt %d: returned %d videos (%d new, %d total unique)",
            attempt, len(entries), new_count, len(all_entries),
        )

        if len(all_entries) >= max_videos or new_count == 0:
            break
        # Wait before retry so TikTok may return different results
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)

    if not all_entries:
        logger.warning("No videos found for @%s", username_clean)
        return []

    # Build result list from merged entries
    sorted_entries = sorted(
        all_entries.values(),
        key=lambda e: e.get("timestamp") or 0,
        reverse=True,
    )

    results = []
    for i, entry in enumerate(sorted_entries[:max_videos]):
        vid = entry["id"]
        ts = entry.get("timestamp")
        timestamp = None
        if ts:
            timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        results.append({
            "url": f"{TIKTOK_BASE}/@{username_clean}/video/{vid}",
            "shortcode": vid,
            "views": entry.get("view_count"),
            "likes": entry.get("like_count"),
            "comments": entry.get("comment_count"),
            "shares": entry.get("repost_count"),
            "caption": (entry.get("description") or entry.get("title") or "")[:500],
            "timestamp": timestamp,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

        if (i + 1) % 30 == 0:
            logger.info("Scroll %d: found %d total videos", (i + 1) // 30, len(results))

    # Log final progress for the web UI
    if results:
        logger.info(
            "Scroll %d: found %d total videos",
            max(1, len(results) // 30),
            len(results),
        )

    logger.info("Collected %d videos with stats", len(results))
    return results


def _get_cookie_jar_path() -> str | None:
    """Convert TikTok session cookies to a Netscape cookie jar file.

    Returns the path to the temporary cookie jar, or None if no session exists.
    """
    if not os.path.exists(TK_SESSION_FILE):
        return None
    try:
        with open(TK_SESSION_FILE) as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        if not cookies:
            return None

        cookie_path = os.path.join(tempfile.gettempdir(), "tk_cookies.txt")
        with open(cookie_path, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                domain = c.get("domain", ".tiktok.com")
                if not domain.startswith("."):
                    domain = "." + domain
                path = c.get("path", "/")
                secure = "TRUE" if c.get("secure", False) else "FALSE"
                expires = str(int(c.get("expires", 0))) if c.get("expires") else "0"
                name = c.get("name", "")
                value = c.get("value", "")
                f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
        return cookie_path
    except Exception as e:
        logger.warning("Failed to load TikTok cookies: %s", e)
        return None


def _run_ytdlp(profile_url: str, max_videos: int) -> dict:
    """Run yt-dlp to fetch playlist metadata."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "-J",
        "--playlist-end", str(max_videos),
    ]
    cookie_jar = _get_cookie_jar_path()
    if cookie_jar:
        cmd.extend(["--cookies", cookie_jar])
        logger.info("Using TikTok session cookies for yt-dlp")

    if TK_PROXY:
        cmd.extend(["--proxy", TK_PROXY])
        logger.info("Using proxy for yt-dlp: %s", TK_PROXY.split("@")[-1] if "@" in TK_PROXY else TK_PROXY)

    cmd.append(profile_url)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp exit code {result.returncode}: {result.stderr[:500]}")
    return json.loads(result.stdout)
