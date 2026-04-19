"""Create a new session file for Instagram/Facebook/TikTok without affecting stored sessions."""

import argparse
import asyncio
import json
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from auth.session_manager import validate_session
from config import VIEWPORT_WIDTH, VIEWPORT_HEIGHT, USER_AGENT

LOGIN_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "facebook": "https://www.facebook.com/login/",
    "tiktok": "https://www.tiktok.com/login",
}


async def create_session(platform: str) -> str:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        await Stealth().apply_stealth_async(context)

        page = await context.new_page()
        await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded")

        print("=" * 60)
        print(f"Please log in to {platform.title()} in the browser window.")
        print("Complete any 2FA if prompted.")
        print("Once logged in, press ENTER here to save the session.")
        print("=" * 60)
        input()

        valid = await validate_session(page, platform)
        if not valid:
            print("Login does not appear successful. Session file not saved.")
            await browser.close()
            return ""

        state = await context.storage_state()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{platform}_session_{ts}.json"
        with open(filename, "w") as f:
            json.dump(state, f)

        print(f"Session saved to: {filename}")
        print(f"Upload this file via the dashboard or copy to .auth/{platform}_session.json")
        await browser.close()
        return filename


def main():
    parser = argparse.ArgumentParser(description="Create a new session file")
    parser.add_argument(
        "--platform",
        choices=["instagram", "facebook", "tiktok"],
        required=True,
    )
    args = parser.parse_args()
    asyncio.run(create_session(args.platform))


if __name__ == "__main__":
    main()
