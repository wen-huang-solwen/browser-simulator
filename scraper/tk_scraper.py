"""Core scraping logic for TikTok videos: use real Chrome via CDP to avoid CAPTCHA.

On macOS: uses the local Chrome installation with a separate user-data-dir.
On Linux (server): uses Chrome + Xvfb for headless operation with a real display.
"""

import asyncio
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone

from playwright.async_api import Playwright, Browser, BrowserContext, Response

from config import (
    MAX_SCROLL_ATTEMPTS,
    NO_NEW_ITEMS_THRESHOLD,
    TIKTOK_BASE,
)
from utils.human_behavior import human_scroll, random_delay

logger = logging.getLogger(__name__)

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    "/usr/bin/google-chrome",           # Linux (apt/deb)
    "/usr/bin/google-chrome-stable",    # Linux (snap/other)
    "/usr/bin/chromium-browser",        # Linux (chromium)
    "/usr/bin/chromium",                # Linux (chromium alt)
]
CDP_PORT = 9222
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TK_SESSION_PATH = os.path.join(_BASE_DIR, ".auth", "tk_session.json")

IS_LINUX = platform.system() == "Linux"

# Chrome data dir: platform-appropriate location
if IS_LINUX:
    TK_CHROME_DATA_DIR = os.path.join(os.path.expanduser("~"), ".chrome-tiktok")
else:
    TK_CHROME_DATA_DIR = os.path.join(
        os.path.expanduser("~/Library/Application Support/Google/Chrome"), "TikTokScraper"
    )


def _find_chrome() -> str:
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "Google Chrome not found. Install Chrome:\n"
        "  Linux: apt install -y google-chrome-stable  (or chromium-browser)\n"
        "  macOS: Install from https://www.google.com/chrome/"
    )


def _xvfb_available() -> bool:
    """Check if Xvfb is installed."""
    return shutil.which("Xvfb") is not None


def _start_xvfb(display: str = ":99") -> subprocess.Popen | None:
    """Start Xvfb virtual display on Linux. Returns the process or None on macOS."""
    if not IS_LINUX:
        return None

    if not _xvfb_available():
        raise RuntimeError(
            "Xvfb not found. Install it:\n  apt install -y xvfb"
        )

    # Kill any existing Xvfb on this display
    subprocess.run(["pkill", "-f", f"Xvfb {display}"], capture_output=True)

    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x800x24", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    logger.info("Started Xvfb on display %s (pid %d)", display, proc.pid)
    return proc


def _stop_xvfb(proc: subprocess.Popen | None) -> None:
    """Stop Xvfb process if running."""
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logger.info("Stopped Xvfb")


async def launch_chrome_cdp(pw: Playwright) -> tuple[Browser, subprocess.Popen, subprocess.Popen | None]:
    """Launch real Chrome with remote debugging and connect via CDP.

    On Linux, starts Xvfb first to provide a virtual display.
    Returns (browser, chrome_proc, xvfb_proc).
    """
    chrome_path = _find_chrome()
    logger.info("Launching Chrome via CDP on port %d", CDP_PORT)

    # Start Xvfb on Linux
    xvfb_proc = _start_xvfb() if IS_LINUX else None

    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={TK_CHROME_DATA_DIR}",
    ]

    # Extra flags for Linux server (no GPU, sandbox issues in containers)
    if IS_LINUX:
        chrome_args.extend([
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--window-size=1280,800",
        ])

    env = os.environ.copy()

    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    # Wait for Chrome to start
    for _ in range(15):
        await asyncio.sleep(1)
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            logger.info("Connected to Chrome via CDP")
            return browser, proc, xvfb_proc
        except Exception:
            continue

    proc.terminate()
    _stop_xvfb(xvfb_proc)
    raise RuntimeError("Failed to connect to Chrome via CDP")


async def scrape_tk_videos(
    context: BrowserContext,
    username: str,
    max_videos: int,
    with_details: bool = False,
    debug: bool = False,
    pw: "Playwright | None" = None,
) -> list[dict]:
    """Scrape TikTok videos from a user profile using real Chrome via CDP.

    We bypass TikTok's anti-bot CAPTCHA by using real Chrome instead of Playwright's
    built-in Chromium. The video stats come from intercepted /api/post/item_list responses.
    """
    from playwright.async_api import async_playwright

    own_pw = False
    if pw is None:
        pw = await async_playwright().start()
        own_pw = True

    chrome_proc = None
    xvfb_proc = None
    try:
        browser, chrome_proc, xvfb_proc = await launch_chrome_cdp(pw)
        cdp_context = browser.contexts[0]

        # Import TikTok cookies from saved session if available
        if os.path.exists(TK_SESSION_PATH):
            import json
            with open(TK_SESSION_PATH) as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if cookies:
                await cdp_context.add_cookies(cookies)
                logger.info("Imported %d TikTok cookies", len(cookies))

        page = await cdp_context.new_page()

        # Collect items from intercepted API responses
        api_items: dict[str, dict] = {}

        async def on_response(response: Response) -> None:
            if "/api/post/item_list" not in response.url:
                return
            try:
                body = await response.json()
                item_list = body.get("itemList") or []
                for item in item_list:
                    vid = item.get("id", "")
                    if not vid:
                        continue
                    stats = item.get("stats", {})
                    api_items[vid] = {
                        "url": f"{TIKTOK_BASE}/@{username}/video/{vid}",
                        "shortcode": vid,
                        "views": stats.get("playCount"),
                        "likes": stats.get("diggCount"),
                        "comments": stats.get("commentCount"),
                        "shares": stats.get("shareCount"),
                        "caption": (item.get("desc") or "")[:500],
                        "timestamp": item.get("createTime"),
                    }
                logger.debug("API intercepted %d items (total: %d)", len(item_list), len(api_items))
            except Exception:
                pass

        page.on("response", on_response)

        username_clean = username.lstrip("@")
        profile_url = f"{TIKTOK_BASE}/@{username_clean}"
        logger.info("Navigating to %s", profile_url)
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for initial API response to arrive
        for _ in range(15):
            await asyncio.sleep(1)
            if api_items:
                break
        logger.info("After initial load: %d items from API", len(api_items))

        no_new_count = 0
        prev_count = 0
        # TikTok needs more patience — API responses can be slow
        tk_no_new_threshold = 5

        for attempt in range(MAX_SCROLL_ATTEMPTS):
            current_count = len(api_items)
            logger.info(
                "Scroll %d: found %d total videos (%d new)",
                attempt + 1,
                current_count,
                current_count - prev_count,
            )

            if current_count >= max_videos:
                break

            if current_count == prev_count:
                no_new_count += 1
                if no_new_count >= tk_no_new_threshold:
                    logger.info("No new videos after %d scrolls, stopping", tk_no_new_threshold)
                    break
            else:
                no_new_count = 0

            prev_count = current_count
            await human_scroll(page)

            # Wait for API response to arrive after scroll
            for _ in range(10):
                await asyncio.sleep(0.5)
                if len(api_items) > prev_count:
                    break

        results = list(api_items.values())[:max_videos]

        if not results:
            logger.warning("No videos found for @%s", username_clean)
            return []

        now = datetime.now(timezone.utc).isoformat()
        for item in results:
            item["scraped_at"] = now
            ts = item.get("timestamp")
            if ts and isinstance(ts, int):
                item["timestamp"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        logger.info("Collected %d videos with stats", len(results))
        await page.close()
        return results
    finally:
        if chrome_proc:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
            logger.info("Chrome process terminated")
        _stop_xvfb(xvfb_proc)
        if own_pw:
            await pw.stop()
