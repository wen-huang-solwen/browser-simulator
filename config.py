"""Configuration constants for the Reels scraper (Instagram + Facebook)."""

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.path.join(BASE_DIR, ".auth")
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSION_FILE = os.path.join(AUTH_DIR, "session.json")
FB_SESSION_FILE = os.path.join(AUTH_DIR, "fb_session.json")

# Instagram URLs
INSTAGRAM_BASE = "https://www.instagram.com"
LOGIN_URL = f"{INSTAGRAM_BASE}/accounts/login/"

# Facebook URLs
FACEBOOK_BASE = "https://www.facebook.com"
FB_LOGIN_URL = f"{FACEBOOK_BASE}/login/"

# TikTok URLs
TIKTOK_BASE = "https://www.tiktok.com"
TK_SESSION_FILE = os.path.join(AUTH_DIR, "tk_session.json")
TK_PROXY = os.environ.get("TIKTOK_PROXY", "socks5://127.0.0.1:1080")
# Smartproxy residential (TW): http://user-<USER>-country-tw:<PASS>@gate.smartproxy.com:7000

# YouTube URLs
YOUTUBE_BASE = "https://www.youtube.com"

# Browser settings
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

# Timeouts (milliseconds)
NAVIGATION_TIMEOUT = 30_000
PAGE_LOAD_TIMEOUT = 15_000
ELEMENT_TIMEOUT = 10_000

# Delays (seconds) — ranges for randomization
SCROLL_DELAY = (2.0, 4.0)
PAGE_DELAY = (3.0, 6.0)
ACTION_DELAY = (0.5, 1.5)

# Scraping limits
DEFAULT_MAX_REELS = 50
SCROLL_BATCH_SIZE = 12  # Instagram loads ~12 items per scroll
MAX_SCROLL_ATTEMPTS = 100  # Safety limit for infinite scroll
NO_NEW_ITEMS_THRESHOLD = 8  # Stop after N scrolls with no new items
