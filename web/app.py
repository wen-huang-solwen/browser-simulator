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
from web import db
from web.login_service import get_session_status, save_uploaded_session
from web.scrape_service import ScrapeJob, ScrapeService

# Ensure scraper loggers are at INFO level (worker process may not run basicConfig)
logging.getLogger("scraper").setLevel(logging.INFO)
logging.getLogger("scraper.fb_reels_scraper").setLevel(logging.INFO)
logging.getLogger("scraper.tk_scraper").setLevel(logging.INFO)
logging.getLogger("scraper.yt_scraper").setLevel(logging.INFO)

service = ScrapeService()


_scrape_queue: asyncio.Queue | None = None
_scrape_worker_task: asyncio.Task | None = None
_current_scrape_task: asyncio.Task | None = None
_current_scrape_item_id: int | None = None
_current_logs: list[dict] = []
_log_subscribers: list[asyncio.Queue] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scrape_queue, _scrape_worker_task
    db.init_db()
    await service.startup()
    _scrape_queue = asyncio.Queue()
    _scrape_worker_task = asyncio.create_task(_scrape_worker())
    # Resume any incomplete items from previous runs
    for item_id in db.get_pending_item_ids():
        await _scrape_queue.put(item_id)
    yield
    _scrape_worker_task.cancel()
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


# ── Dashboard ───────────────────────────────────────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    path = os.path.join(STATIC_DIR, "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/batch/add")
async def batch_add(
    url: str = Form(...),
    max_reels: int = Form(DEFAULT_MAX_REELS),
):
    """Add a single scrape item."""
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if max_reels < 1 or max_reels > 9999:
        max_reels = DEFAULT_MAX_REELS
    try:
        username = service.parse_username(url)
    except ValueError:
        username = url
    platform = service.detect_platform(url)
    ids = db.create_items([{"url": url, "username": username, "platform": platform, "max_reels": max_reels}])
    await _scrape_queue.put(ids[0])
    return {"id": ids[0]}


@app.post("/api/batch/upload")
async def batch_upload(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    reader = csv.DictReader(io.StringIO(text))
    if "url" not in (reader.fieldnames or []):
        raise HTTPException(status_code=400, detail="CSV must have a 'url' column")

    items: list[dict] = []
    for row in reader:
        url = row.get("url", "").strip()
        if not url:
            continue
        max_reels = int(row.get("max_reels", DEFAULT_MAX_REELS) or DEFAULT_MAX_REELS)
        if max_reels < 1 or max_reels > 9999:
            max_reels = DEFAULT_MAX_REELS
        try:
            username = service.parse_username(url)
        except ValueError:
            username = url
        platform = service.detect_platform(url)
        items.append({"url": url, "username": username, "platform": platform, "max_reels": max_reels})
        if len(items) >= 100:
            break

    if not items:
        raise HTTPException(status_code=400, detail="CSV contains no valid URLs")

    ids = db.create_items(items)
    for item_id in ids:
        await _scrape_queue.put(item_id)
    return {"total_items": len(ids)}


@app.get("/api/batch/items")
async def batch_items(
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD, inclusive)"),
):
    return {"items": db.list_items(date_from=date_from, date_to=date_to)}


@app.post("/api/batch/download")
async def batch_download_selected(ids: list[int]):
    """Download a merged CSV for the given item IDs."""
    if not ids:
        raise HTTPException(status_code=400, detail="No items selected")

    from output.exporter import CSV_FIELDS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    found = False
    for item_id in ids:
        item = db.get_item(item_id)
        if not item or item["status"] != "done" or not item.get("csv_filename"):
            continue
        filepath = os.path.join(DATA_DIR, item["csv_filename"])
        if not os.path.isfile(filepath):
            continue
        found = True
        with open(filepath, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                writer.writerow(row)

    if not found:
        raise HTTPException(status_code=404, detail="No completed scrapes to download")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"scrapes_{ts}.csv"
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/batch/delete")
async def batch_delete(ids: list[int]):
    """Delete scrape items by IDs. Stops the running one if included."""
    if not ids:
        raise HTTPException(status_code=400, detail="No items specified")
    # If the currently running item is in the delete list, cancel it
    if _current_scrape_item_id in ids and _current_scrape_task:
        _current_scrape_task.cancel()
    # Remove pending items from the queue (rebuild it without deleted IDs)
    deleted_set = set(ids)
    new_queue: asyncio.Queue = asyncio.Queue()
    while not _scrape_queue.empty():
        try:
            qid = _scrape_queue.get_nowait()
            if qid not in deleted_set:
                new_queue.put_nowait(qid)
        except asyncio.QueueEmpty:
            break
    # Swap queues
    while not new_queue.empty():
        _scrape_queue.put_nowait(new_queue.get_nowait())
    count = db.delete_items(ids)
    return {"deleted": count}


@app.post("/api/batch/rerun")
async def batch_rerun(ids: list[int]):
    """Re-queue selected scrape items for another run."""
    if not ids:
        raise HTTPException(status_code=400, detail="No items specified")
    requeued = 0
    for item_id in ids:
        item = db.get_item(item_id)
        if not item:
            continue
        db.update_item_status(item_id, "pending", error_message="", csv_filename="", result_count=0)
        await _scrape_queue.put(item_id)
        requeued += 1
    if requeued == 0:
        raise HTTPException(status_code=404, detail="No valid items to rerun")
    return {"requeued": requeued}


@app.get("/api/batch/logs/{item_id}")
async def batch_logs(item_id: int):
    """Stream logs for a scrape item via SSE (running) or return saved logs (completed)."""
    # Currently running — stream live
    if _current_scrape_item_id == item_id:
        sub: asyncio.Queue = asyncio.Queue(maxsize=200)
        _log_subscribers.append(sub)

        async def live_generator():
            try:
                for event in list(_current_logs):
                    yield f"data: {json.dumps(event)}\n\n"
                while True:
                    event = await sub.get()
                    if event.get("type") == "done":
                        yield f"data: {json.dumps(event)}\n\n"
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                if sub in _log_subscribers:
                    _log_subscribers.remove(sub)

        return StreamingResponse(
            live_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Completed / error — return saved logs from DB
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item["status"] in ("pending",):
        raise HTTPException(status_code=404, detail="Item has not started yet")

    saved_logs = json.loads(item.get("logs") or "[]")

    async def saved_generator():
        for event in saved_logs:
            yield f"data: {json.dumps(event)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        saved_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/batch/stop")
async def batch_stop():
    """Stop the currently running scrape."""
    if _current_scrape_task and _current_scrape_item_id:
        _current_scrape_task.cancel()
        db.update_item_status(_current_scrape_item_id, "error", error_message="Stopped by user")
        return {"stopped": _current_scrape_item_id}
    raise HTTPException(status_code=404, detail="No scrape is currently running")


async def _drain_progress(job: ScrapeJob) -> str:
    """Drain progress_queue in real-time, store logs and broadcast to subscribers.

    Returns error message if any, empty string otherwise.
    """
    error_message = ""
    while True:
        event = await job.progress_queue.get()
        if event.get("type") == "done":
            # Broadcast done to subscribers
            for sub in _log_subscribers:
                sub.put_nowait(event)
            break
        if event.get("type") == "error":
            error_message = event.get("message", "Unknown error")
        _current_logs.append(event)
        for sub in _log_subscribers:
            try:
                sub.put_nowait(event)
            except asyncio.QueueFull:
                pass
    return error_message


async def _scrape_worker() -> None:
    """Background worker that processes scrape items one at a time."""
    global _current_scrape_task, _current_scrape_item_id, _current_logs, _log_subscribers
    while True:
        item_id = await _scrape_queue.get()
        item = db.get_item(item_id)
        if not item or item["status"] in ("done", "error"):
            continue
        _current_scrape_item_id = item_id
        _current_logs = []
        _log_subscribers = []
        db.update_item_status(item_id, "running")
        try:
            job = ScrapeJob(
                username=item["username"] or item["url"],
                max_reels=item["max_reels"],
                platform=item["platform"] or "instagram",
            )
            _current_scrape_task = asyncio.create_task(service.run_scrape(job))
            drain_task = asyncio.create_task(_drain_progress(job))
            await _current_scrape_task
            error_message = await drain_task

            logs_json = json.dumps(_current_logs)
            if error_message:
                db.update_item_status(item_id, "error", error_message=error_message[:500], logs=logs_json)
            else:
                csv_filename = os.path.basename(job.csv_path) if job.csv_path else ""
                db.update_item_status(
                    item_id, "done",
                    csv_filename=csv_filename,
                    result_count=len(job.results),
                    logs=logs_json,
                )
        except asyncio.CancelledError:
            logs_json = json.dumps(_current_logs)
            # Re-check if item was deleted or just stopped
            if db.get_item(item_id):
                db.update_item_status(item_id, "error", error_message="Stopped by user", logs=logs_json)
        except Exception as e:
            logs_json = json.dumps(_current_logs)
            db.update_item_status(item_id, "error", error_message=str(e)[:500], logs=logs_json)
        finally:
            _current_scrape_item_id = None
            _current_scrape_task = None
