"""Bridge between web interface and existing scraper."""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from auth.session_manager import session_exists, validate_session
from config import DEFAULT_MAX_REELS
from output.exporter import export_csv
from scraper.browser import launch_browser
from scraper.fb_reels_scraper import scrape_fb_reels
from scraper.reels_scraper import scrape_account_reels
from scraper.tk_scraper import scrape_tk_videos
from scraper.yt_scraper import scrape_yt_videos


class QueueLogHandler(logging.Handler):
    """Logging handler that pushes scraper log messages to an asyncio.Queue."""

    _scroll_re = re.compile(r"Scroll (\d+): found (\d+) total (?:reels|videos)")

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self.queue = queue

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        event: dict = {"type": "log", "message": msg}

        match = self._scroll_re.search(record.getMessage())
        if match:
            event["type"] = "progress"
            event["scroll"] = int(match.group(1))
            event["found"] = int(match.group(2))

        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


@dataclass
class ScrapeJob:
    username: str
    max_reels: int = DEFAULT_MAX_REELS
    platform: str = "instagram"
    progress_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    results: list[dict] = field(default_factory=list)
    csv_path: str = ""


class ScrapeService:
    """Manages browser lifecycle and scrape execution."""

    def __init__(self):
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ig_context: BrowserContext | None = None
        self._fb_context: BrowserContext | None = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        """Launch browser once at server start."""
        self._pw = await async_playwright().start()

        # Instagram context
        if session_exists("instagram"):
            self._browser, self._ig_context = await launch_browser(
                self._pw, headless=True, platform="instagram"
            )
            page = await self._ig_context.new_page()
            try:
                valid = await validate_session(page, "instagram")
                if not valid:
                    logging.warning(
                        "Instagram session is invalid. Run `python main.py <username> --login` to re-authenticate."
                    )
            finally:
                await page.close()

        # Facebook context
        if session_exists("facebook"):
            if not self._browser:
                self._browser, self._fb_context = await launch_browser(
                    self._pw, headless=True, platform="facebook"
                )
            else:
                from auth.session_manager import load_session_path
                from config import VIEWPORT_WIDTH, VIEWPORT_HEIGHT, USER_AGENT
                from playwright_stealth import Stealth
                ctx_kwargs = {
                    "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                    "user_agent": USER_AGENT,
                    "locale": "en-US",
                    "timezone_id": "America/Los_Angeles",
                }
                fb_session = load_session_path("facebook")
                if fb_session:
                    ctx_kwargs["storage_state"] = fb_session
                self._fb_context = await self._browser.new_context(**ctx_kwargs)
                await Stealth().apply_stealth_async(self._fb_context)

            page = await self._fb_context.new_page()
            try:
                valid = await validate_session(page, "facebook")
                if not valid:
                    logging.warning(
                        "Facebook session is invalid. Run `python main.py <username> --platform facebook --login` to re-authenticate."
                    )
            finally:
                await page.close()

        # TikTok uses its own Chrome/CDP, no Playwright context needed
        self._tk_session_exists = session_exists("tiktok")
        if self._tk_session_exists:
            logging.info("TikTok session found — TikTok scraping available.")

        if not self._ig_context and not self._fb_context and not self._tk_session_exists:
            logging.warning(
                "No saved session found for Instagram/Facebook/TikTok. "
                "Only YouTube scraping will be available. "
                "Run `python main.py <username> --login` to enable other platforms."
            )

    async def shutdown(self) -> None:
        """Close browser on server shutdown."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    @staticmethod
    def parse_username(url_or_username: str) -> str:
        """Extract username from an Instagram/Facebook URL or plain username.

        Accepts:
          - https://www.instagram.com/username/
          - https://www.facebook.com/pagename/reels/
          - https://www.facebook.com/profile.php?id=61576250430963
          - https://www.tiktok.com/@username
          - @username
          - username
        """
        url_or_username = url_or_username.strip().rstrip("/")

        # Facebook numeric profile: profile.php?id=123456
        match = re.search(r"facebook\.com/profile\.php\?id=(\d+)", url_or_username)
        if match:
            return f"profile.php?id={match.group(1)}"

        # TikTok: https://www.tiktok.com/@username
        match = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", url_or_username)
        if match:
            return match.group(1)

        # YouTube: https://www.youtube.com/@username or /channel/ID
        match = re.search(r"youtube\.com/@([A-Za-z0-9_.]+)", url_or_username)
        if match:
            return match.group(1)
        match = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", url_or_username)
        if match:
            return match.group(1)

        match = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url_or_username)
        if match:
            return match.group(1)

        match = re.search(r"facebook\.com/([A-Za-z0-9_.]+)", url_or_username)
        if match:
            return match.group(1)

        username = url_or_username.lstrip("@")
        if re.match(r"^[A-Za-z0-9_.]+$", username):
            return username

        raise ValueError(f"Cannot parse username from: {url_or_username}")

    @staticmethod
    def detect_platform(url_or_username: str) -> str:
        """Detect platform from URL. Returns 'facebook', 'tiktok', 'youtube', or 'instagram'."""
        if "facebook.com" in url_or_username:
            return "facebook"
        if "tiktok.com" in url_or_username:
            return "tiktok"
        if "youtube.com" in url_or_username or "youtu.be" in url_or_username:
            return "youtube"
        return "instagram"

    def _get_context(self, platform: str) -> BrowserContext:
        ctx = self._fb_context if platform == "facebook" else self._ig_context
        if not ctx:
            platform_name = "Facebook" if platform == "facebook" else "Instagram"
            raise RuntimeError(
                f"No {platform_name} session. Run `python main.py <username> --platform {platform} --login` first."
            )
        return ctx

    async def run_scrape(self, job: ScrapeJob) -> None:
        """Run a scrape job while sending progress to the job's queue."""
        async with self._lock:
            platform = job.platform
            logger_names = {
                "facebook": "scraper.fb_reels_scraper",
                "tiktok": "scraper.tk_scraper",
                "youtube": "scraper.yt_scraper",
            }
            logger_name = logger_names.get(platform, "scraper.reels_scraper")
            scraper_logger = logging.getLogger(logger_name)
            handler = QueueLogHandler(job.progress_queue)
            handler.setFormatter(logging.Formatter("%(message)s"))
            scraper_logger.addHandler(handler)

            try:
                platform_labels = {
                    "facebook": "Facebook",
                    "tiktok": "TikTok",
                    "youtube": "YouTube",
                }
                platform_label = platform_labels.get(platform, "Instagram")
                job.progress_queue.put_nowait(
                    {"type": "status", "message": f"Starting {platform_label} scrape for @{job.username}..."}
                )

                if platform == "youtube":
                    results = await scrape_yt_videos(
                        job.username,
                        job.max_reels,
                    )
                elif platform == "tiktok":
                    results = await scrape_tk_videos(
                        None,
                        job.username,
                        job.max_reels,
                    )
                elif platform == "facebook":
                    context = self._get_context(platform)
                    results = await scrape_fb_reels(
                        context,
                        job.username,
                        job.max_reels,
                    )
                else:
                    context = self._get_context(platform)
                    results = await scrape_account_reels(
                        context,
                        job.username,
                        job.max_reels,
                    )
                job.results = results

                item_label = "videos" if platform in ("tiktok", "youtube") else "reels"
                if results:
                    job.csv_path = export_csv(results, job.username)
                    job.progress_queue.put_nowait(
                        {"type": "status", "message": f"Scraped {len(results)} {item_label}, CSV saved."}
                    )
                else:
                    job.progress_queue.put_nowait(
                        {"type": "status", "message": f"No {item_label} found."}
                    )

            except Exception as e:
                job.progress_queue.put_nowait(
                    {"type": "error", "message": str(e)}
                )
            finally:
                scraper_logger.removeHandler(handler)
                job.progress_queue.put_nowait({"type": "done"})
