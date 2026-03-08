"""Core scraping logic for Facebook Reels: grid-based view stats extraction."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from playwright.async_api import BrowserContext, Page

from config import (
    FACEBOOK_BASE,
    MAX_SCROLL_ATTEMPTS,
    NAVIGATION_TIMEOUT,
    NO_NEW_ITEMS_THRESHOLD,
)
from utils.human_behavior import random_delay, scroll_delay

logger = logging.getLogger(__name__)


async def _dismiss_login_dialog(page: Page) -> None:
    """Close the Facebook login dialog that appears for unauthenticated users."""
    closed = await page.evaluate("""() => {
        const dialog = document.querySelector('[role="dialog"]');
        if (dialog && dialog.getBoundingClientRect().width > 0) {
            const closeBtn = dialog.querySelector('[aria-label="Close"]');
            if (closeBtn) { closeBtn.click(); return true; }
        }
        return false;
    }""")
    if closed:
        await asyncio.sleep(1)


def parse_fb_count(text: str) -> int | None:
    """Parse Facebook view counts like '1.2K', '3.5M', '6.1萬', '120', '1,234' into integers."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace("\xa0", "")

    # Remove trailing label words (views/次觀看/次瀏覽 etc.)
    text = re.sub(r"\s*(views?|次觀看|次瀏覽|次播放)\s*$", "", text, flags=re.IGNORECASE)

    # Chinese format: "6.1萬"
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


def _extract_reel_id(url: str) -> str:
    """Extract reel ID from a Facebook reel URL like /reel/1234567890/"""
    match = re.search(r"/reel/(\d+)", url)
    return match.group(1) if match else ""


async def collect_fb_reels_from_grid(
    page: Page, username: str, max_reels: int
) -> list[dict]:
    """Scroll the Facebook reels grid and extract URLs + view counts.

    Facebook reel grid links have:
      - href like /reel/1234567890/?s=fb_shorts_profile&stack_idx=0
      - aria-label="Reel tile preview"
      - innerText contains the view count (e.g. "14K")
    """
    # Handle numeric profile IDs: profile.php?id=123&sk=reels_tab
    if username.startswith("profile.php?id="):
        reels_url = f"{FACEBOOK_BASE}/{username}&sk=reels_tab"
    else:
        reels_url = f"{FACEBOOK_BASE}/{username}/reels/"
    logger.info("Navigating to %s", reels_url)
    await page.goto(reels_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)

    # Facebook needs extra time for JS to render the reels grid
    await asyncio.sleep(5)

    # Check if the page exists
    page_title = await page.title()
    page_content = await page.content()
    if "Page Not Found" in page_title or "This content isn't available" in page_content:
        raise ValueError(f"Facebook page '{username}' not found or not accessible")

    # Check if Facebook session is valid (c_user cookie present after navigation)
    has_session = await page.evaluate("() => document.cookie.includes('c_user')")
    if not has_session:
        logger.warning(
            "Facebook session is invalid or expired — results may be limited. "
            "Re-login with: python main.py <username> --platform facebook --login"
        )

    # Dismiss login dialog that blocks scrolling for unauthenticated sessions
    await _dismiss_login_dialog(page)

    collected: dict[str, dict] = {}  # reel_id -> data
    no_new_count = 0

    for attempt in range(MAX_SCROLL_ATTEMPTS):
        # Dismiss login dialog if it reappeared
        await _dismiss_login_dialog(page)

        grid_data = await page.evaluate("""
            () => {
                const results = [];
                // Facebook reel tile links match /reel/<numeric_id>
                const links = document.querySelectorAll('a[href*="/reel/"]');
                for (const link of links) {
                    const href = link.href;
                    if (!href.match(/\\/reel\\/\\d+/)) continue;

                    // The link's innerText directly contains the view count
                    const viewText = link.innerText.trim();

                    results.push({
                        href: href,
                        viewText: viewText,
                    });
                }
                return results;
            }
        """)

        prev_count = len(collected)
        for item in grid_data:
            url = item["href"]
            reel_id = _extract_reel_id(url)
            if not reel_id or reel_id in collected:
                continue

            views = parse_fb_count(item["viewText"])

            collected[reel_id] = {
                "url": url,
                "shortcode": reel_id,
                "views": views,
                "likes": None,
                "comments": None,
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

        # Facebook uses a #scrollview container — mouse.wheel on the document
        # doesn't reach it. Use keyboard PageDown which scrolls the focused
        # scrollable element correctly.
        await page.keyboard.press("PageDown")
        await scroll_delay()

    results = list(collected.values())[:max_reels]
    logger.info("Collected %d reels with stats from grid", len(results))
    return results


async def scrape_fb_reels(
    context: BrowserContext,
    username: str,
    max_reels: int,
    with_details: bool = False,
    debug: bool = False,
) -> list[dict]:
    """Scrape all reels from a Facebook page/profile.

    Collects URLs + view counts from the reels grid page.
    """
    page = await context.new_page()

    try:
        reels = await collect_fb_reels_from_grid(page, username, max_reels)

        if not reels:
            logger.warning("No reels found for %s", username)
            return []

        now = datetime.now(timezone.utc).isoformat()
        for reel in reels:
            reel["caption"] = ""
            reel["timestamp"] = None
            reel["scraped_at"] = now

        return reels
    finally:
        await page.close()
