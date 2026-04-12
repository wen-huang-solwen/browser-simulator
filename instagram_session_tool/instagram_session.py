"""Instagram Session Creator — standalone tool.

How to use:
  1. Make sure Python 3.9+ is installed (https://www.python.org/downloads/).
  2. Double-click run.bat (Windows) or run.sh (macOS / Linux).
     Or run:  python instagram_session.py
  3. A browser window opens to Instagram login.
  4. Log in normally (including 2FA if prompted).
  5. When you see your Instagram home feed, return to the terminal
     window and press ENTER.
  6. A file named instagram_session.json is saved next to this script
     in this folder. Send it to your admin.
"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_FILE = SCRIPT_DIR / "instagram_session.json"
LOGIN_URL = "https://www.instagram.com/accounts/login/"


def ensure_dependencies() -> None:
    """Install playwright + chromium on first run. Idempotent on later runs."""
    need_pip_install = False
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        need_pip_install = True

    if need_pip_install:
        print("First-time setup — installing Playwright (this takes 1-2 minutes)...")
        print()
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", "playwright"]
            )
        except subprocess.CalledProcessError as e:
            print()
            print("ERROR: Could not install Playwright.")
            print("Please make sure you have an internet connection and try again.")
            print(f"Details: {e}")
            sys.exit(1)

    # Always run browser install — it's a fast no-op when already installed,
    # and handles the case where playwright is present but chromium isn't.
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"]
        )
    except subprocess.CalledProcessError as e:
        print()
        print("ERROR: Could not install the Chromium browser.")
        print("Please make sure you have an internet connection and try again.")
        print(f"Details: {e}")
        sys.exit(1)

    if need_pip_install:
        print()
        print("Setup complete.")
        print()


async def create_session() -> Path | None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print("=" * 64)
        print(" INSTAGRAM SESSION CREATOR")
        print("=" * 64)
        print(" 1. Log in to Instagram in the browser window that just opened.")
        print(" 2. Complete 2FA if prompted.")
        print(" 3. When you see your Instagram home feed, come back here.")
        print(" 4. Press ENTER below to save your session.")
        print("=" * 64)
        try:
            input("\nPress ENTER after you have logged in: ")
        except (EOFError, KeyboardInterrupt):
            await browser.close()
            return None

        cookies = await context.cookies()
        has_ds_user_id = any(c.get("name") == "ds_user_id" for c in cookies)
        has_sessionid = any(c.get("name") == "sessionid" for c in cookies)
        if not (has_ds_user_id and has_sessionid):
            print()
            print("Login does not appear successful — the ds_user_id / sessionid")
            print("cookies are missing. Please run this script again and make sure")
            print("to finish logging in before pressing ENTER.")
            await browser.close()
            return None

        state = await context.storage_state()
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)

        await browser.close()
        return OUTPUT_FILE


def main() -> None:
    ensure_dependencies()
    try:
        path = asyncio.run(create_session())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)

    if not path:
        print("\nNo session file was saved.")
        sys.exit(1)

    print()
    print("=" * 64)
    print(" SUCCESS")
    print("=" * 64)
    print(f" Session saved to: {path}")
    print(f" Created at:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print(" Next step: send this file to your admin, or upload it via")
    print(" the dashboard's session upload button.")
    print("=" * 64)
    try:
        input("\nPress ENTER to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
