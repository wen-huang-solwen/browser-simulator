"""FastAPI application with SSE scrape endpoint and REST API."""

import asyncio
import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from config import DATA_DIR, DEFAULT_MAX_REELS
from web.login_service import get_session_status, save_uploaded_session
from web.scrape_service import ScrapeJob, ScrapeService

# Ensure scraper loggers are at INFO level (worker process may not run basicConfig)
logging.getLogger("scraper").setLevel(logging.INFO)
logging.getLogger("scraper.fb_reels_scraper").setLevel(logging.INFO)
logging.getLogger("scraper.tk_scraper").setLevel(logging.INFO)
logging.getLogger("scraper.yt_scraper").setLevel(logging.INFO)

service = ScrapeService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await service.startup()
    yield
    await service.shutdown()


app = FastAPI(title="Reels Scraper", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/docs", response_class=HTMLResponse)
async def api_docs():
    docs_path = os.path.join(STATIC_DIR, "api-docs.html")
    with open(docs_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/scrape")
async def scrape(
    url: str = Query(..., description="Instagram/Facebook/TikTok/YouTube profile URL or username"),
    max_reels: int = Query(DEFAULT_MAX_REELS, ge=1, le=9999),
    platform: str = Query("auto", description="Platform: instagram, facebook, or auto"),
):
    try:
        username = service.parse_username(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if platform == "auto":
        platform = service.detect_platform(url)

    job = ScrapeJob(username=username, max_reels=max_reels, platform=platform)

    async def event_generator():
        task = asyncio.create_task(service.run_scrape(job))

        try:
            while True:
                event = await job.progress_queue.get()

                if event["type"] == "done":
                    # Send results before done sentinel
                    results_event = {
                        "type": "results",
                        "data": job.results,
                        "csv_filename": os.path.basename(job.csv_path) if job.csv_path else "",
                    }
                    yield f"data: {json.dumps(results_event)}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break

                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/session/status")
async def session_status():
    return get_session_status()


@app.post("/api/session/upload")
async def session_upload(
    platform: str = Form(...),
    file: UploadFile = File(...),
):
    if platform not in ("instagram", "facebook", "tiktok"):
        raise HTTPException(status_code=400, detail="Invalid platform")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    try:
        save_uploaded_session(platform, content)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Restart service to pick up new session
    await service.shutdown()
    await service.startup()

    return {"ok": True, "status": get_session_status()}


@app.get("/api/download/{filename}")
async def download(filename: str):
    # Path traversal guard
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        filepath,
        media_type="text/csv",
        filename=filename,
    )


# ── REST API v1: synchronous scrape → CSV response ──────────────────────


API_CSV_FIELDS = ["link", "id", "views", "likes", "comments", "scraped_at"]


def _results_to_csv(results: list[dict], username: str) -> str:
    """Convert scrape results to CSV string with the API schema."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=API_CSV_FIELDS)
    writer.writeheader()
    for r in results:
        writer.writerow({
            "link": r.get("url", ""),
            "id": r.get("shortcode", ""),
            "views": r.get("views", ""),
            "likes": r.get("likes", ""),
            "comments": r.get("comments", ""),
            "scraped_at": r.get("scraped_at", ""),
        })
    return buf.getvalue()


@app.post("/api/v1/scrape")
async def api_scrape(
    profile_link: str = Form(..., description="Profile URL, e.g. https://www.instagram.com/username/reels/"),
    max_reels: int = Form(DEFAULT_MAX_REELS, ge=1, le=9999),
    auth_file: Optional[UploadFile] = File(None, description="Session JSON file (optional)"),
):
    """Scrape videos/reels and return a CSV.

    Accepts:
      - profile_link: Instagram/Facebook/TikTok/YouTube profile URL
      - max_reels: maximum number of videos to scrape
      - auth_file: optional Playwright session JSON for authentication

    Returns: CSV with columns link, id, views, likes, comments, scraped_at
    """
    # If an auth file is uploaded, install it temporarily
    if auth_file is not None:
        platform_for_auth = ScrapeService.detect_platform(profile_link)
        if platform_for_auth in ("instagram", "facebook", "tiktok"):
            content = await auth_file.read()
            if len(content) > 5 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Auth file too large (max 5MB)")
            try:
                save_uploaded_session(platform_for_auth, content)
            except (json.JSONDecodeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=f"Invalid auth file: {e}")
            # Restart service to pick up new session
            await service.shutdown()
            await service.startup()

    try:
        username = service.parse_username(profile_link)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    platform = service.detect_platform(profile_link)
    job = ScrapeJob(username=username, max_reels=max_reels, platform=platform)

    await service.run_scrape(job)

    if not job.results:
        raise HTTPException(status_code=404, detail="No videos/reels found for this profile")

    csv_content = _results_to_csv(job.results, username)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{username}_reels_{ts}.csv"

    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
