"""SQLite persistence for scrape jobs."""

import os
import sqlite3
from datetime import datetime, timezone

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "batch.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scrape_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT NOT NULL,
            username      TEXT,
            platform      TEXT,
            max_reels     INTEGER NOT NULL DEFAULT 50,
            status        TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            csv_filename  TEXT,
            result_count  INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            finished_at   TEXT
        );
    """)
    conn.close()


def create_items(items: list[dict]) -> list[int]:
    """Insert scrape items. Returns list of new row IDs."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    ids = []
    for item in items:
        cur = conn.execute(
            "INSERT INTO scrape_items (url, username, platform, max_reels, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (item["url"], item.get("username"), item.get("platform"),
             item.get("max_reels", 50), now),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def list_items(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Return scrape items, newest first. Optionally filter by created_at date range."""
    query = "SELECT * FROM scrape_items"
    params: list = []
    conditions = []
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(date_to)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC"
    conn = _connect()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_item_ids() -> list[int]:
    """Return IDs of items that are pending or running (for resume on startup)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id FROM scrape_items WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def get_item(item_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM scrape_items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_item_status(item_id: int, status: str, **kwargs) -> None:
    """Update an item's status plus optional fields (error_message, csv_filename, result_count)."""
    sets = ["status = ?"]
    vals: list = [status]
    if status in ("done", "error"):
        sets.append("finished_at = ?")
        vals.append(datetime.now(timezone.utc).isoformat())
    for key in ("error_message", "csv_filename", "result_count"):
        if key in kwargs:
            sets.append(f"{key} = ?")
            vals.append(kwargs[key])
    vals.append(item_id)
    conn = _connect()
    conn.execute(f"UPDATE scrape_items SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()
