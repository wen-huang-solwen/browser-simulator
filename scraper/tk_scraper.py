"""Core scraping logic for TikTok videos using yt-dlp.

yt-dlp handles TikTok's anti-bot protections and extracts video metadata
including view counts, likes, comments, shares, and captions.
"""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone

from config import TIKTOK_BASE

logger = logging.getLogger(__name__)


async def scrape_tk_videos(
    context,  # unused, kept for API compatibility
    username: str,
    max_videos: int,
    with_details: bool = False,
    debug: bool = False,
    pw=None,  # unused, kept for API compatibility
) -> list[dict]:
    """Scrape TikTok videos from a user profile using yt-dlp."""
    username_clean = username.lstrip("@")
    profile_url = f"{TIKTOK_BASE}/@{username_clean}"
    logger.info("Fetching TikTok videos for @%s via yt-dlp", username_clean)

    try:
        result = await asyncio.to_thread(
            _run_ytdlp, profile_url, max_videos
        )
    except Exception as e:
        logger.error("yt-dlp failed: %s", e)
        return []

    entries = result.get("entries") or []
    logger.info("yt-dlp returned %d videos", len(entries))

    results = []
    for i, entry in enumerate(entries[:max_videos]):
        vid = entry.get("id", "")
        if not vid:
            continue

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


def _run_ytdlp(profile_url: str, max_videos: int) -> dict:
    """Run yt-dlp to fetch playlist metadata."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "-J",
        "--playlist-end", str(max_videos),
        profile_url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp exit code {result.returncode}: {result.stderr[:500]}")
    return json.loads(result.stdout)
