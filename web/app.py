"""FastAPI application with SSE scrape endpoint."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from config import DATA_DIR, DEFAULT_MAX_REELS
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
