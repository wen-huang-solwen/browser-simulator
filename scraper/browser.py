"""Browser launch and stealth configuration."""

import logging

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright_stealth import Stealth

from auth.session_manager import load_session_path
from config import USER_AGENT, VIEWPORT_HEIGHT, VIEWPORT_WIDTH

logger = logging.getLogger(__name__)

stealth = Stealth()


async def launch_browser(
    pw: Playwright,
    headless: bool = True,
    platform: str = "instagram",
) -> tuple[Browser, BrowserContext]:
    """Launch browser with stealth settings and optional saved session."""
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )

    context_kwargs = {
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "user_agent": USER_AGENT,
        "locale": "en-US",
        "timezone_id": "America/Los_Angeles",
    }

    session_path = load_session_path(platform)
    if session_path:
        context_kwargs["storage_state"] = session_path
        logger.info("Loading saved %s session", platform)

    context = await browser.new_context(**context_kwargs)
    await stealth.apply_stealth_async(context)

    return browser, context
