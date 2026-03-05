"""Export scraped data to JSON and CSV formats."""

import csv
import json
import logging
import os
import re
from datetime import datetime, timezone

from config import DATA_DIR

logger = logging.getLogger(__name__)

CSV_FIELDS = [
    "account", "url", "shortcode", "views", "likes", "comments",
    "shares", "caption", "timestamp", "scraped_at",
]


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _generate_filename(username: str, ext: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Sanitize username for filesystem (e.g. profile.php?id=123 -> profile_123)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", username)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    return os.path.join(DATA_DIR, f"{safe_name}_reels_{ts}.{ext}")


def export_json(data: list[dict], username: str) -> str:
    _ensure_data_dir()
    filepath = _generate_filename(username, "json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("JSON exported to %s", filepath)
    return filepath


def export_csv(data: list[dict], username: str) -> str:
    _ensure_data_dir()
    filepath = _generate_filename(username, "csv")
    rows = [{**row, "account": username} for row in data]
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CSV exported to %s", filepath)
    return filepath


def export(data: list[dict], username: str, fmt: str = "both") -> list[str]:
    """Export data in the specified format(s). Returns list of file paths."""
    files = []
    if fmt in ("json", "both"):
        files.append(export_json(data, username))
    if fmt in ("csv", "both"):
        files.append(export_csv(data, username))
    return files
