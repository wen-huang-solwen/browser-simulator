"""Core scraping logic for YouTube videos using yt-dlp.

No browser or API key needed. Uses yt-dlp CLI with full extraction
to get views, likes, comments, upload date, and title.
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _check_ytdlp() -> str:
    """Return path to yt-dlp or raise if not installed."""
    path = shutil.which("yt-dlp")
    if not path:
        raise RuntimeError(
            "yt-dlp is not installed. Install it with: pip install yt-dlp"
        )
    return path


def _build_channel_url(username: str) -> str:
    """Construct YouTube channel URL from username."""
    if username.startswith("http"):
        return username
    return f"https://www.youtube.com/@{username}"


def _map_video(entry: dict) -> dict:
    """Map yt-dlp JSON entry to standard video dict."""
    video_id = entry.get("id", "")
    url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
    # flat-playlist entries have relative urls
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={video_id}"

    return {
        "url": url,
        "shortcode": video_id,
        "views": entry.get("view_count"),
        "likes": entry.get("like_count"),
        "comments": entry.get("comment_count"),
        "shares": None,
        "caption": entry.get("title", ""),
        "timestamp": None,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


async def scrape_yt_videos(
    username: str,
    max_videos: int = 50,
    debug: bool = False,
) -> list[dict]:
    """Scrape YouTube channel videos using yt-dlp.

    Args:
        username: YouTube channel username (without @) or full URL.
        max_videos: Maximum number of videos to scrape.
        debug: Enable verbose yt-dlp output.

    Returns:
        List of video dicts with views, likes, comments, etc.
    """
    ytdlp = _check_ytdlp()
    channel_url = _build_channel_url(username)

    logger.info("Scraping YouTube channel: %s (max %d videos)", channel_url, max_videos)

    cmd = [
        ytdlp,
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", str(max_videos),
        channel_url,
    ]

    if not debug:
        cmd.insert(1, "--quiet")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    results = []
    scroll_count = 0

    async for line in process.stdout:
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if debug:
                logger.debug("Skipping non-JSON line: %s", line[:100])
            continue

        video = _map_video(entry)
        results.append(video)

        # Log progress in the same format other scrapers use
        if len(results) % 5 == 0 or len(results) == 1:
            scroll_count += 1
            logger.info(
                "Scroll %d: found %d total videos", scroll_count, len(results)
            )

    await process.wait()

    if process.returncode != 0:
        stderr = await process.stderr.read()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if not results:
            # Extract the last ERROR line for a cleaner message
            error_lines = [l for l in stderr_text.splitlines() if l.startswith("ERROR")]
            msg = error_lines[-1] if error_lines else stderr_text[-300:]
            raise RuntimeError(f"yt-dlp failed: {msg}")
        else:
            logger.warning("yt-dlp exited with warnings: %s", stderr_text[:200])

    # Final progress log
    if results:
        scroll_count += 1
        logger.info("Scroll %d: found %d total videos", scroll_count, len(results))

    logger.info("Scraped %d YouTube videos for %s", len(results), username)
    return results
