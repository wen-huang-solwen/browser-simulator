"""CLI entry point for Reels scraper (Instagram + Facebook)."""

import argparse
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

from auth.session_manager import manual_login, session_exists, validate_session
from config import DEFAULT_MAX_REELS
from output.exporter import export
from scraper.browser import launch_browser
from scraper.fb_reels_scraper import scrape_fb_reels
from scraper.reels_scraper import scrape_account_reels
from scraper.tk_scraper import scrape_tk_videos
from scraper.yt_scraper import scrape_yt_videos


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Reels stats (views, likes, comments) from Instagram or Facebook"
    )
    parser.add_argument(
        "username",
        help="Username or page name to scrape reels from",
    )
    parser.add_argument(
        "--platform",
        choices=["instagram", "facebook", "tiktok", "youtube"],
        default="instagram",
        help="Platform to scrape (default: instagram)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open browser for manual login and save session",
    )
    parser.add_argument(
        "--max-reels",
        type=int,
        default=DEFAULT_MAX_REELS,
        help=f"Maximum number of reels to scrape (default: {DEFAULT_MAX_REELS})",
    )
    parser.add_argument(
        "--output-format",
        choices=["json", "csv", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--with-details",
        action="store_true",
        help="Visit each reel page for caption + timestamp (slower, Instagram only)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and visible browser",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    platform = args.platform

    # Login mode or no session: need visible browser
    headless = not (args.login or args.debug)
    if args.login or not session_exists(platform):
        headless = False

    # YouTube uses yt-dlp (no browser needed)
    if platform == "youtube":
        print(f"\nScraping YouTube videos for @{args.username} (max: {args.max_reels})...")
        results = await scrape_yt_videos(
            args.username,
            args.max_reels,
            debug=args.debug,
        )

        if not results:
            print("No videos found.")
            return

        files = export(results, args.username, args.output_format)
        print(f"\nScraped {len(results)} videos from @{args.username}")
        for f in files:
            print(f"  Saved: {f}")
        return

    # TikTok uses real Chrome via CDP (to avoid CAPTCHA)
    if platform == "tiktok":
        if args.login:
            # For TikTok login, still use Playwright browser
            async with async_playwright() as pw:
                browser, context = await launch_browser(pw, headless=False, platform=platform)
                try:
                    page = await context.new_page()
                    success = await manual_login(page, context, platform)
                    if not success:
                        print("Login failed. Exiting.")
                        return
                    print("Session saved. You can now run without --login.")
                finally:
                    await browser.close()
            return

        print(f"\nScraping TikTok videos for @{args.username} (max: {args.max_reels})...")
        import platform as _pf
        if _pf.system() == "Darwin":
            print("(Using real Chrome — please close Chrome if it's running)")
        else:
            print("(Using Chrome + Xvfb on Linux server)")
        results = await scrape_tk_videos(
            None,  # context not used for TikTok
            args.username,
            args.max_reels,
            debug=args.debug,
        )

        if not results:
            print("No videos found.")
            return

        files = export(results, args.username, args.output_format)
        print(f"\nScraped {len(results)} videos from @{args.username}")
        for f in files:
            print(f"  Saved: {f}")
        return

    # Instagram / Facebook flow
    async with async_playwright() as pw:
        browser, context = await launch_browser(pw, headless=headless, platform=platform)

        try:
            page = await context.new_page()

            # Handle login
            if args.login or not session_exists(platform):
                success = await manual_login(page, context, platform)
                if not success:
                    print("Login failed. Exiting.")
                    return
                if args.login:
                    print("Session saved. You can now run without --login.")
                    return

            # Validate existing session
            is_valid = await validate_session(page, platform)
            if not is_valid:
                print("Saved session is invalid. Please run with --login to re-authenticate.")
                return

            await page.close()

            # Scrape reels
            _labels = {"facebook": "Facebook", "instagram": "Instagram"}
            platform_label = _labels.get(platform, "Instagram")
            print(f"\nScraping {platform_label} videos for @{args.username} (max: {args.max_reels})...")

            if platform == "facebook":
                results = await scrape_fb_reels(
                    context,
                    args.username,
                    args.max_reels,
                    debug=args.debug,
                )
            else:
                results = await scrape_account_reels(
                    context,
                    args.username,
                    args.max_reels,
                    with_details=args.with_details,
                    debug=args.debug,
                )

            if not results:
                print("No reels found.")
                return

            # Export results
            files = export(results, args.username, args.output_format)

            # Summary
            print(f"\nScraped {len(results)} reels from @{args.username}")
            for f in files:
                print(f"  Saved: {f}")

        finally:
            await browser.close()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
