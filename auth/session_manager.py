"""Session management: save/load cookies, validate sessions, manual login flow."""

import json
import logging
import os

from playwright.async_api import BrowserContext, Page

from config import (
    AUTH_DIR, FACEBOOK_BASE, FB_LOGIN_URL, FB_SESSION_FILE,
    INSTAGRAM_BASE, LOGIN_URL, SESSION_FILE,
    TIKTOK_BASE, TK_SESSION_FILE,
)

logger = logging.getLogger(__name__)


_SESSION_PATHS = {
    "instagram": SESSION_FILE,
    "facebook": FB_SESSION_FILE,
    "tiktok": TK_SESSION_FILE,
}


def _session_path(platform: str) -> str:
    return _SESSION_PATHS.get(platform, SESSION_FILE)


def session_exists(platform: str = "instagram") -> bool:
    return os.path.exists(_session_path(platform))


async def save_session(context: BrowserContext, platform: str = "instagram") -> None:
    os.makedirs(AUTH_DIR, exist_ok=True)
    state = await context.storage_state()
    path = _session_path(platform)
    with open(path, "w") as f:
        json.dump(state, f)
    logger.info("Session saved to %s", path)


def load_session_path(platform: str = "instagram") -> str | None:
    if session_exists(platform):
        return _session_path(platform)
    return None


async def validate_session(page: Page, platform: str = "instagram") -> bool:
    """Check if the current session is authenticated."""
    if platform == "facebook":
        return await _validate_fb_session(page)
    if platform == "tiktok":
        return await _validate_tk_session(page)
    return await _validate_ig_session(page)


async def _validate_ig_session(page: Page) -> bool:
    """Check if the current session is authenticated by navigating to Instagram."""
    try:
        await page.goto(INSTAGRAM_BASE, wait_until="domcontentloaded", timeout=15000)
        if "/accounts/login" in page.url:
            logger.warning("Session invalid — redirected to login")
            return False
        logged_in = await page.evaluate("""
            () => {
                return document.cookie.includes('ds_user_id') ||
                       document.querySelector('[aria-label="Home"]') !== null ||
                       document.querySelector('svg[aria-label="Home"]') !== null;
            }
        """)
        if logged_in:
            logger.info("Session is valid")
            return True
        logger.warning("Session validation: no logged-in indicators found")
        return False
    except Exception as e:
        logger.error("Session validation failed: %s", e)
        return False


async def _validate_fb_session(page: Page) -> bool:
    """Check if the current session is authenticated by navigating to Facebook."""
    try:
        await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=15000)
        if "/login" in page.url:
            logger.warning("FB session invalid — redirected to login")
            return False
        logged_in = await page.evaluate("""
            () => {
                return document.cookie.includes('c_user') ||
                       document.querySelector('[aria-label="Facebook"]') !== null;
            }
        """)
        if logged_in:
            logger.info("Facebook session is valid")
            return True
        logger.warning("FB session validation: no logged-in indicators found")
        return False
    except Exception as e:
        logger.error("FB session validation failed: %s", e)
        return False


async def _validate_tk_session(page: Page) -> bool:
    """Check if the current session is authenticated by navigating to TikTok."""
    try:
        await page.goto(TIKTOK_BASE, wait_until="domcontentloaded", timeout=15000)
        # sessionid and sid_tt are httpOnly, so check via context cookies
        context = page.context
        cookies = await context.cookies()
        cookie_names = {c["name"] for c in cookies}
        has_session = "sessionid" in cookie_names or "sid_tt" in cookie_names
        if has_session:
            logger.info("TikTok session is valid")
            return True
        # Fallback: check DOM for logged-in indicators
        logged_in = await page.evaluate("""
            () => {
                return !document.querySelector('[data-e2e="top-login-button"]');
            }
        """)
        if logged_in:
            logger.info("TikTok session is valid (DOM check)")
            return True
        logger.warning("TikTok session validation: no logged-in indicators found")
        return False
    except Exception as e:
        logger.error("TikTok session validation failed: %s", e)
        return False


async def manual_login(page: Page, context: BrowserContext, platform: str = "instagram") -> bool:
    """Open login page and wait for user to log in manually."""
    _names = {"facebook": "Facebook", "tiktok": "TikTok", "instagram": "Instagram"}
    _urls = {"facebook": FB_LOGIN_URL, "tiktok": f"{TIKTOK_BASE}/login", "instagram": LOGIN_URL}
    platform_name = _names.get(platform, "Instagram")
    login_url = _urls.get(platform, LOGIN_URL)

    print("\n" + "=" * 60)
    print(f"MANUAL {platform_name.upper()} LOGIN REQUIRED")
    print("=" * 60)
    print(f"1. A browser window will open to {platform_name} login")
    print("2. Log in with your credentials")
    print("3. Complete any 2FA if prompted")
    print("4. Once you see your feed, press ENTER here to continue")
    print("=" * 60 + "\n")

    await page.goto(login_url, wait_until="domcontentloaded")

    input("Press ENTER after you have logged in successfully...")

    is_valid = await validate_session(page, platform)
    if is_valid:
        await save_session(context, platform)
        print(f"{platform_name} login successful! Session saved.")
        return True
    else:
        print("Login does not appear successful. Please try again.")
        return False
