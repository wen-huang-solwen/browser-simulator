"""Core scraping logic: grid-based stats extraction + reel page for caption/timestamp."""

import logging
import re
from datetime import datetime, timezone

from playwright.async_api import BrowserContext, Page

from config import (
    INSTAGRAM_BASE,
    MAX_SCROLL_ATTEMPTS,
    NAVIGATION_TIMEOUT,
    NO_NEW_ITEMS_THRESHOLD,
)
from utils.human_behavior import human_scroll, page_delay, random_delay, scroll_delay

logger = logging.getLogger(__name__)


def parse_count(text: str) -> int | None:
    """Parse counts like '1.2K', '3.5M', '6.1萬', '120' into integers."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace("\xa0", "")

    # Chinese format: "6.1萬" (萬 = 10,000)
    match = re.match(r"^([\d.]+)\s*萬$", text)
    if match:
        return int(float(match.group(1)) * 10_000)

    # English abbreviated: "1.2K", "3.5M"
    match = re.match(r"^([\d.]+)\s*([KkMmBb]?)$", text)
    if match:
        num = float(match.group(1))
        suffix = match.group(2).upper()
        multipliers = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        return int(num * multipliers.get(suffix, 1))

    return None


def _extract_shortcode(url: str) -> str:
    """Extract shortcode from a reel URL."""
    match = re.search(r"/reel/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else ""


async def collect_reels_from_grid(
    page: Page, username: str, max_reels: int
) -> list[dict]:
    """Scroll the reels grid and extract URLs + stats (views, likes, comments)
    directly from the grid link spans.

    Each grid reel link contains spans in order:
    Views come from the link's innerText (the visible overlay, always reliable).
    When 6 spans are present they follow [likes, likes, comments, comments, views, views],
    so we extract likes/comments from the first two pairs when available.
    """
    reels_url = f"{INSTAGRAM_BASE}/{username}/reels/"
    logger.info("Navigating to %s", reels_url)
    await page.goto(reels_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    await page_delay()

    # Check if Instagram session is valid
    if "/accounts/login" in page.url:
        raise RuntimeError(
            "Instagram session is invalid or expired — redirected to login. "
            "Please upload a new session file or re-login with: "
            "python main.py <username> --login"
        )
    has_session = await page.evaluate("() => document.cookie.includes('ds_user_id')")
    if not has_session:
        raise RuntimeError(
            "Instagram session is invalid or expired. "
            "Please upload a new session file or re-login with: "
            "python main.py <username> --login"
        )

    if "Page Not Found" in (await page.title()) or await page.query_selector(
        "text=Sorry, this page isn't available"
    ):
        raise ValueError(f"Account '{username}' not found or not accessible")

    collected: dict[str, dict] = {}  # shortcode -> data
    no_new_count = 0

    for attempt in range(MAX_SCROLL_ATTEMPTS):
        grid_data = await page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href*="/reel/"]');
                return Array.from(links).map(link => {
                    const spans = link.querySelectorAll('span');
                    const texts = Array.from(spans)
                        .map(s => s.innerText.trim())
                        .filter(t => t);
                    return {
                        href: link.href,
                        viewText: link.innerText.trim(),
                        spans: texts,
                    };
                });
            }
        """)

        prev_count = len(collected)
        for item in grid_data:
            url = item["href"]
            shortcode = _extract_shortcode(url)
            if not shortcode or shortcode in collected:
                continue

            views = parse_count(item["viewText"])
            likes = None
            comments = None

            # Deduplicate paired spans: [a, a, b, b, c, c] -> [a, b, c]
            spans = item["spans"]
            unique = []
            i = 0
            while i < len(spans):
                unique.append(spans[i])
                # Skip duplicate in pair
                if i + 1 < len(spans) and spans[i + 1] == spans[i]:
                    i += 2
                else:
                    i += 1

            # 3 unique values = [likes, comments, views]
            if len(unique) >= 3:
                likes = parse_count(unique[0])
                comments = parse_count(unique[1])

            collected[shortcode] = {
                "url": url,
                "shortcode": shortcode,
                "views": views,
                "likes": likes,
                "comments": comments,
            }

        logger.info(
            "Scroll %d: found %d total reels (%d new)",
            attempt + 1,
            len(collected),
            len(collected) - prev_count,
        )

        if len(collected) >= max_reels:
            break

        if len(collected) == prev_count:
            no_new_count += 1
            if no_new_count >= NO_NEW_ITEMS_THRESHOLD:
                logger.info(
                    "No new reels after %d scrolls, stopping", NO_NEW_ITEMS_THRESHOLD
                )
                break
        else:
            no_new_count = 0

        # The reels grid lives inside an inner scrollable div, not the window —
        # window.scrollTo and mouse.wheel at the page level do nothing. Use
        # scrollIntoView on the last reel link, which automatically walks up to
        # the correct scrollable parent and triggers the lazy-load sentinel.
        await human_scroll(page)
        await page.evaluate(
            "() => { const links = document.querySelectorAll('a[href*=\"/reel/\"]');"
            " if (links.length) links[links.length - 1].scrollIntoView({block: 'end'}); }"
        )
        await scroll_delay()

    results = list(collected.values())[:max_reels]
    logger.info("Collected %d reels with stats from grid", len(results))
    return results


async def _extract_reel_details(page: Page, url: str) -> dict:
    """Visit a reel page to extract caption and timestamp from meta description.

    Meta description format:
      "61K likes, 382 comments - username 於 March 28, 2024 : \"caption text\""
    or in English:
      "61K likes, 382 comments - username on March 28, 2024: \"caption text\""
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    await random_delay((1.5, 3.0))

    details: dict = {"caption": "", "timestamp": None}

    meta_content = await page.evaluate("""
        () => {
            const meta = document.querySelector('meta[name="description"]')
                      || document.querySelector('meta[property="og:description"]');
            return meta ? meta.content : '';
        }
    """)

    if meta_content:
        # Extract caption: everything after the date/colon pattern
        # Patterns: "於 March 28, 2024 : \"caption\"" or "on March 28, 2024: \"caption\""
        caption_match = re.search(
            r'(?:於|on)\s+(.+?)\s*:\s*["\u201c](.+?)["\u201d]\s*\.?\s*$',
            meta_content,
            re.DOTALL,
        )
        if caption_match:
            details["timestamp"] = caption_match.group(1).strip()
            details["caption"] = caption_match.group(2).strip()
        else:
            # Try simpler: just get text after the last colon
            colon_match = re.search(r':\s*["\u201c](.+?)["\u201d]', meta_content, re.DOTALL)
            if colon_match:
                details["caption"] = colon_match.group(1).strip()

    # If meta didn't give timestamp, try the date link on the page
    if not details["timestamp"]:
        date_text = await page.evaluate("""
            () => {
                // Look for the date link (e.g., "2024年3月28日")
                const timeEl = document.querySelector('time[datetime]');
                if (timeEl) return timeEl.getAttribute('datetime');
                return '';
            }
        """)
        if date_text:
            details["timestamp"] = date_text

    return details


async def scrape_account_reels(
    context: BrowserContext,
    username: str,
    max_reels: int,
    with_details: bool = False,
    debug: bool = False,
) -> list[dict]:
    """Scrape all reels from an account.

    1. Collect URLs + views/likes/comments from the grid page (fast)
    2. Optionally visit each reel page for caption + timestamp (slow)
    """
    page = await context.new_page()

    try:
        # Step 1: Collect stats from grid
        reels = await collect_reels_from_grid(page, username, max_reels)

        if not reels:
            logger.warning("No reels found for %s", username)
            return []

        now = datetime.now(timezone.utc).isoformat()

        if not with_details:
            # Fast mode: grid stats only
            for reel in reels:
                reel["caption"] = ""
                reel["timestamp"] = None
                reel["scraped_at"] = now
            logger.info("Fast mode: skipping individual page visits")
            return reels

        # Step 2: Visit each reel for caption + timestamp
        results = []
        for i, reel in enumerate(reels, 1):
            logger.info(
                "Processing reel %d/%d: %s (views=%s, likes=%s, comments=%s)",
                i,
                len(reels),
                reel["shortcode"],
                reel["views"],
                reel["likes"],
                reel["comments"],
            )
            try:
                details = await _extract_reel_details(page, reel["url"])
                reel.update(details)
            except Exception as e:
                logger.error("Failed to get details for %s: %s", reel["shortcode"], e)
                reel["caption"] = ""
                reel["timestamp"] = None

            reel["scraped_at"] = datetime.now(timezone.utc).isoformat()
            results.append(reel)

            if i < len(reels):
                await random_delay((2.0, 4.0))

        return results
    finally:
        await page.close()
