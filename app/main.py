import asyncio
import os
import re
import sys
from urllib.parse import urlparse

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from app.core.database import (
    clear_endpoint_rows,
    fetch_cached_rows,
    save_endpoint_meta,
)
from app.schemas.models import (
    DiscoverRequest,
    DiscoverResponse,
    ExtractRequest,
    ExtractResponse,
)
from app.services.ai_analyzer import generate_or_load_blueprint
from app.services.dom_probe import detect_url_page_number
from app.services.executor import JOB_STATUS, execute_blueprint_scrape

app = FastAPI(
    title="Universal Web-to-API + Multi-Page PDF Engine",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background task reference so only 1 browser runs per endpoint at a time
ACTIVE_TASKS: dict[str, asyncio.Task] = {}


class StartJobRequest(BaseModel):
    url: str
    start_page: int = 1
    pages_to_scrape: int = 5
    rows_override: int | None = None
    rows_to_extract: int | None = None


def get_endpoint_id(url: str) -> str:
    domain = urlparse(url).netloc
    return re.sub(r"[^a-zA-Z0-9]", "_", domain).strip("_").lower()


@app.get("/", response_class=HTMLResponse)
async def home_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Web2API + Multi-Page PDF Engine</title>
        <style>
            * { box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                max-width: 1080px;
                margin: 32px auto;
                padding: 0 24px;
                background: #0b1120;
                color: #f1f5f9;
                line-height: 1.5;
            }
            h1 {
                display: flex;
                align-items: center;
                gap: 12px;
                font-size: 26px;
                font-weight: 700;
                color: #f8fafc;
                margin-bottom: 24px;
            }
            .card {
                background: #1e293b;
                padding: 24px;
                border-radius: 12px;
                margin-bottom: 24px;
                border: 1px solid #334155;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
            }
            .card h3 {
                margin-top: 0;
                margin-bottom: 16px;
                font-size: 18px;
                color: #e2e8f0;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .input-group {
                display: flex;
                gap: 12px;
                align-items: center;
                flex-wrap: wrap;
            }
            input, button {
                padding: 12px 16px;
                border-radius: 8px;
                border: 1px solid #475569;
                font-size: 15px;
            }
            input {
                background: #0f172a;
                color: #f8fafc;
                outline: none;
                transition: border-color 0.2s;
            }
            input:focus {
                border-color: #3b82f6;
            }
            button {
                background: #2563eb;
                color: white;
                border: none;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                display: inline-flex;
                align-items: center;
                gap: 8px;
            }
            button:hover:not(:disabled) {
                background: #1d4ed8;
            }
            button:disabled {
                background: #475569;
                cursor: not-allowed;
                opacity: 0.7;
            }
            button.btn-secondary {
                background: #334155;
                color: #e2e8f0;
            }
            button.btn-secondary:hover:not(:disabled) {
                background: #475569;
            }
            button.stop-btn {
                background: #dc2626;
            }
            button.stop-btn:hover:not(:disabled) {
                background: #b91c1c;
            }
            .badge-container {
                display: flex;
                gap: 10px;
                flex-wrap: wrap;
                margin-top: 16px;
            }
            .badge {
                display: inline-flex;
                align-items: center;
                padding: 6px 14px;
                background: rgba(16, 185, 129, 0.15);
                color: #34d399;
                border: 1px solid rgba(16, 185, 129, 0.3);
                border-radius: 6px;
                font-size: 14px;
                font-weight: 600;
            }
            .api-box {
                background: #020617;
                border: 1px solid #10b981;
                padding: 18px;
                border-radius: 8px;
                margin-top: 20px;
            }
            .api-url {
                margin: 8px 0;
                font-family: monospace;
                font-size: 14px;
                word-break: break-all;
            }
            .api-url a {
                color: #38bdf8;
                text-decoration: none;
            }
            .api-url a:hover {
                text-decoration: underline;
            }
            .progress-container {
                margin-top: 18px;
                padding: 16px;
                background: #0f172a;
                border-radius: 8px;
                border: 1px solid #334155;
            }
            .progress-bar {
                width: 100%;
                background: #334155;
                height: 18px;
                border-radius: 9px;
                overflow: hidden;
                margin: 10px 0;
            }
            .progress-fill {
                height: 100%;
                background: linear-gradient(90deg, #3b82f6, #10b981);
                width: 0%;
                transition: width 0.3s ease-in-out;
            }
            .status-text {
                margin-top: 10px;
                font-size: 14px;
                color: #94a3b8;
            }
            .status-text.error {
                color: #f87171;
            }
            .status-text.success {
                color: #34d399;
            }
            pre {
                background: #020617;
                padding: 16px;
                border-radius: 8px;
                overflow-x: auto;
                max-height: 480px;
                color: #38bdf8;
                font-size: 13px;
                border: 1px solid #1e293b;
            }
            .preset-btn {
                padding: 6px 12px;
                font-size: 12px;
                background: #334155;
                color: #cbd5e1;
                border-radius: 6px;
            }
        </style>
    </head>
    <body>
        <h1>⚡ Universal Web-to-API + Multi-Page PDF Engine</h1>

        <!-- Step 1 Card -->
        <div class="card">
            <h3>Step 1: Paste Website URL to Analyze Pattern</h3>
            <div class="input-group">
                <input type="text" id="urlInput" value="https://nomenclator.anm.ro/medicamente?page=1" style="flex: 1; min-width: 320px;" />
                <button id="analyzeBtn" onclick="discoverSite()">1. Analyze Website</button>
            </div>
            <div id="statusText" class="status-text"></div>
            <div id="statsBox" class="badge-container" style="display:none;">
                <span class="badge" id="pagesBadge"></span>
                <span class="badge" id="rowsPerPageBadge"></span>
                <span class="badge" id="rowsBadge"></span>
            </div>
        </div>

        <!-- Step 2 Card -->
        <div class="card" id="extractCard" style="display:none;">
            <h3>Step 2: Scrape Multiple Pages into Your Live API</h3>
            <div class="input-group" style="margin-bottom: 14px;">
                <div>
                    <label style="font-size: 14px; color: #94a3b8; display: block; margin-bottom: 4px;">Start From Page:</label>
                    <input type="number" id="startPageInput" value="1" style="width: 100px;" min="1" />
                </div>
                <div>
                    <label style="font-size: 14px; color: #94a3b8; display: block; margin-bottom: 4px;">Total Rows to Extract:</label>
                    <input type="number" id="rowsInput" value="20" style="width: 130px;" min="1" max="100000" />
                </div>
                <div style="align-self: flex-end; display: flex; gap: 6px;">
                    <button class="preset-btn" onclick="setRows(10)">10</button>
                    <button class="preset-btn" onclick="setRows(20)">20</button>
                    <button class="preset-btn" onclick="setRows(50)">50</button>
                    <button class="preset-btn" onclick="setRows(100)">100</button>
                </div>
            </div>

            <div style="display: flex; gap: 12px; flex-wrap: wrap; margin-top: 14px;">
                <button id="startJobBtn" onclick="startBackgroundScrape()">⚡ Start Live Scraping (Background Job)</button>
                <button id="extractBtn" class="btn-secondary" onclick="extractDirectly()">Direct Synchronous Extract</button>
                <button id="stopBtn" class="stop-btn" style="display:none;" onclick="stopCurrentJob()">⏹ Stop Scraping</button>
            </div>

            <!-- Progress Box -->
            <div id="progressBox" class="progress-container" style="display:none;">
                <div style="display: flex; justify-content: space-between; font-size: 14px; font-weight: 600;">
                    <span id="progressLabel">Scraping in background...</span>
                    <span id="progressPercent">0%</span>
                </div>
                <div class="progress-bar">
                    <div id="progressFill" class="progress-fill"></div>
                </div>
                <div id="progressSubText" style="font-size: 13px; color: #94a3b8;"></div>
            </div>

            <!-- Live API Links Box -->
            <div id="liveApiBox" class="api-box" style="display:none;">
                <p style="margin:0 0 10px 0; font-weight:bold; color:#f8fafc;">🚀 Your Live API Endpoints (Ready to query from any client):</p>
                <div class="api-url" id="liveApiUrlText"></div>
                <div class="api-url" id="dynamicApiUrlText"></div>
                <div class="api-url" id="swaggerDocText"></div>
                <div style="margin-top: 12px;">
                    <button class="preset-btn" onclick="refreshLivePreview()">🔄 Refresh Live Data Preview</button>
                </div>
            </div>
        </div>

        <!-- Step 3 Card -->
        <div class="card">
            <h3>JSON API Preview</h3>
            <pre id="jsonOutput">// Click "1. Analyze Website" above to start...</pre>
        </div>

        <script>
            let currentEndpointId = "";
            let pollInterval = null;

            function setRows(n) {
                document.getElementById('rowsInput').value = n;
                updateApiLinks();
            }

            async function discoverSite() {
                const btn = document.getElementById('analyzeBtn');
                const url = document.getElementById('urlInput').value.trim();
                if (!url) return;

                btn.disabled = true;
                const statusEl = document.getElementById('statusText');
                statusEl.className = "status-text";
                statusEl.innerText = "⏳ Probing website pagination, columns, modal, and PDFs with AI Analyzer...";

                try {
                    const res = await fetch('/api/v1/discover', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: url, force_reanalyze: true})
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

                    currentEndpointId = data.endpoint_id;
                    statusEl.className = "status-text success";
                    statusEl.innerText = "✅ Website Analyzed! Choose row count below and click Start Scraping.";
                    document.getElementById('statsBox').style.display = 'flex';
                    document.getElementById('extractCard').style.display = 'block';
                    document.getElementById('pagesBadge').innerText = "Total Pages: " + data.total_pages_detected;
                    document.getElementById('rowsPerPageBadge').innerText = "Rows Per Page: " + data.rows_per_page;
                    document.getElementById('rowsBadge').innerText = "Total Rows on Site: ~" + data.total_rows_estimated;

                    updateApiLinks(data.live_api_url);
                    document.getElementById('jsonOutput').innerText = JSON.stringify(data, null, 2);
                } catch (err) {
                    statusEl.className = "status-text error";
                    statusEl.innerText = "❌ Error: " + err.message;
                } finally {
                    btn.disabled = false;
                }
            }

            function updateApiLinks(baseUrl) {
                if (!baseUrl && currentEndpointId) {
                    baseUrl = `${window.location.origin}/api/v1/live/${currentEndpointId}`;
                }
                if (!baseUrl) return;

                const count = document.getElementById('rowsInput').value;
                const startPg = document.getElementById('startPageInput').value;
                document.getElementById('liveApiBox').style.display = 'block';
                document.getElementById('liveApiUrlText').innerHTML = `1. Full Live Dataset: <a href="${baseUrl}" target="_blank">${baseUrl}</a>`;
                document.getElementById('dynamicApiUrlText').innerHTML = `2. Filter / Pagination API: <a href="${baseUrl}?limit=${count}&page=${startPg}" target="_blank">${baseUrl}?limit=${count}&page=${startPg}</a>`;
                document.getElementById('swaggerDocText').innerHTML = `3. Interactive API Docs: <a href="/docs" target="_blank">${window.location.origin}/docs</a>`;
            }

            // Background Scraping with Real-time Progress Bar
            async function startBackgroundScrape() {
                const url = document.getElementById('urlInput').value.trim();
                const count = parseInt(document.getElementById('rowsInput').value) || 20;
                const startPg = parseInt(document.getElementById('startPageInput').value) || 1;

                const startBtn = document.getElementById('startJobBtn');
                const extractBtn = document.getElementById('extractBtn');
                const stopBtn = document.getElementById('stopBtn');
                const progressBox = document.getElementById('progressBox');
                const progressFill = document.getElementById('progressFill');
                const progressLabel = document.getElementById('progressLabel');
                const progressPercent = document.getElementById('progressPercent');
                const progressSub = document.getElementById('progressSubText');

                startBtn.disabled = true;
                extractBtn.disabled = true;
                stopBtn.style.display = 'inline-flex';
                progressBox.style.display = 'block';
                progressFill.style.width = '0%';
                progressPercent.innerText = '0%';
                progressLabel.innerText = `Starting scrape from Page ${startPg} for ${count} rows...`;
                progressSub.innerText = "Launching Playwright browser engine...";

                try {
                    const res = await fetch('/api/v1/jobs/start', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            url: url,
                            start_page: startPg,
                            rows_to_extract: count
                        })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

                    currentEndpointId = data.endpoint_id;
                    updateApiLinks(data.live_api_url);

                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = setInterval(async () => {
                        try {
                            const sRes = await fetch(`/api/v1/jobs/status/${currentEndpointId}`);
                            const status = await sRes.json();
                            const scraped = status.rows_scraped || 0;
                            const target = status.rows_target || count;
                            const pct = Math.min(100, Math.round((scraped / target) * 100));

                            progressFill.style.width = pct + '%';
                            progressPercent.innerText = pct + '%';
                            progressLabel.innerText = `Scraped ${scraped} / ${target} rows (Page ${status.current_page || startPg})`;
                            progressSub.innerText = `Status: ${status.status} | Real-time SQLite index updated`;

                            if (status.status === 'completed' || status.status === 'stopped' || status.status === 'error') {
                                clearInterval(pollInterval);
                                pollInterval = null;
                                startBtn.disabled = false;
                                extractBtn.disabled = false;
                                stopBtn.style.display = 'none';

                                if (status.status === 'completed') {
                                    progressLabel.innerText = `🎉 Scrape Complete! ${scraped} rows indexed.`;
                                } else if (status.status === 'stopped') {
                                    progressLabel.innerText = `⏹ Scrape stopped by user at ${scraped} rows.`;
                                } else {
                                    progressLabel.innerText = `❌ Scrape encountered error: ${status.error || 'Unknown error'}`;
                                }
                                refreshLivePreview();
                            }
                        } catch (pollErr) {
                            console.error("Poll error:", pollErr);
                        }
                    }, 800);

                } catch (err) {
                    startBtn.disabled = false;
                    extractBtn.disabled = false;
                    stopBtn.style.display = 'none';
                    progressLabel.innerText = "❌ Error: " + err.message;
                }
            }

            async function stopCurrentJob() {
                if (!currentEndpointId) return;
                try {
                    await fetch(`/api/v1/jobs/stop/${currentEndpointId}`, {method: 'POST'});
                    document.getElementById('progressLabel').innerText = "Stopping scraper...";
                } catch (err) {
                    console.error("Stop error:", err);
                }
            }

            // Direct synchronous extraction (/api/v1/extract)
            async function extractDirectly() {
                const btn = document.getElementById('extractBtn');
                const startBtn = document.getElementById('startJobBtn');
                const url = document.getElementById('urlInput').value.trim();
                const count = parseInt(document.getElementById('rowsInput').value) || 20;
                const startPg = parseInt(document.getElementById('startPageInput').value) || 1;

                btn.disabled = true;
                startBtn.disabled = true;
                const statusEl = document.getElementById('statusText');
                statusEl.className = "status-text";
                statusEl.innerText = `⏳ Running synchronous extraction: collecting ${count} rows starting from Page ${startPg}...`;

                try {
                    const res = await fetch('/api/v1/extract', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: url, rows_to_extract: count, start_page: startPg})
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

                    currentEndpointId = data.endpoint_id;
                    statusEl.className = "status-text success";
                    statusEl.innerText = `🎉 Successfully extracted ${data.rows_returned} rows!`;
                    updateApiLinks(data.live_api_url);
                    document.getElementById('jsonOutput').innerText = JSON.stringify(data, null, 2);
                } catch (err) {
                    statusEl.className = "status-text error";
                    statusEl.innerText = "❌ Extraction error: " + err.message;
                } finally {
                    btn.disabled = false;
                    startBtn.disabled = false;
                }
            }

            // Compatibility alias for the old button function
            async function extractRows() {
                await startBackgroundScrape();
            }

            async function refreshLivePreview() {
                if (!currentEndpointId) return;
                try {
                    const res = await fetch(`/api/v1/live/${currentEndpointId}?limit=10`);
                    const data = await res.json();
                    document.getElementById('jsonOutput').innerText = JSON.stringify(data, null, 2);
                } catch (err) {
                    console.error("Live preview error:", err);
                }
            }
        </script>
    </body>
    </html>
    """


@app.post("/api/v1/discover", response_model=DiscoverResponse)
async def discover_website(payload: DiscoverRequest, request: Request):
    blueprint = await generate_or_load_blueprint(
        payload.url, force_reanalyze=payload.force_reanalyze
    )
    start_pg = detect_url_page_number(payload.url)
    endpoint_id = get_endpoint_id(payload.url)

    save_endpoint_meta(
        endpoint_id,
        payload.url,
        blueprint.pagination.total_pages_detected,
        blueprint.pagination.rows_per_page,
    )

    sample_rows = await execute_blueprint_scrape(
        blueprint, rows_to_extract=1, start_page=start_pg, endpoint_id=endpoint_id
    )

    total_estimated = (
        blueprint.pagination.total_pages_detected * blueprint.pagination.rows_per_page
    )
    base_host = str(request.base_url).rstrip("/")
    live_api_url = f"{base_host}/api/v1/live/{endpoint_id}"

    return DiscoverResponse(
        source_url=payload.url,
        endpoint_id=endpoint_id,
        live_api_url=live_api_url,
        total_pages_detected=blueprint.pagination.total_pages_detected,
        rows_per_page=blueprint.pagination.rows_per_page,
        total_rows_estimated=total_estimated,
        blueprint=blueprint,
        sample_preview_row=sample_rows[0].model_dump() if sample_rows else None,
    )


@app.post("/api/v1/extract", response_model=ExtractResponse)
async def extract_data(payload: ExtractRequest, request: Request):
    """
    Synchronous extraction endpoint: scrapes the requested number of rows starting from start_page.
    Also indexes all rows into SQLite so the Live API is automatically populated.
    """
    blueprint = await generate_or_load_blueprint(payload.url, force_reanalyze=False)
    endpoint_id = get_endpoint_id(payload.url)

    clear_endpoint_rows(endpoint_id)
    save_endpoint_meta(
        endpoint_id,
        payload.url,
        blueprint.pagination.total_pages_detected,
        blueprint.pagination.rows_per_page,
    )

    rows = await execute_blueprint_scrape(
        blueprint=blueprint,
        rows_to_extract=payload.rows_to_extract,
        start_page=payload.start_page,
        endpoint_id=endpoint_id,
    )

    base_host = str(request.base_url).rstrip("/")
    live_api_url = f"{base_host}/api/v1/live/{endpoint_id}"
    total_estimated = (
        blueprint.pagination.total_pages_detected * blueprint.pagination.rows_per_page
    )

    return ExtractResponse(
        source_url=payload.url,
        endpoint_id=endpoint_id,
        live_api_url=live_api_url,
        total_rows_estimated=total_estimated,
        rows_requested=payload.rows_to_extract,
        rows_returned=len(rows),
        data=rows,
    )


@app.post("/api/v1/jobs/start")
async def start_background_scrape_job(payload: StartJobRequest, request: Request):
    """
    Starts a non-blocking background scraping job across as many pages as requested.
    Saves row-by-row to SQLite so the server never hits an HTTP timeout!
    """
    blueprint = await generate_or_load_blueprint(payload.url, force_reanalyze=False)
    endpoint_id = get_endpoint_id(payload.url)

    # Cancel any existing running task on this endpoint so two browsers do not collide
    old_task = ACTIVE_TASKS.get(endpoint_id)
    if old_task and not old_task.done():
        if endpoint_id in JOB_STATUS:
            JOB_STATUS[endpoint_id]["stop_requested"] = True
        try:
            old_task.cancel()
        except Exception:
            pass
        await asyncio.sleep(0.5)

    clear_endpoint_rows(endpoint_id)
    save_endpoint_meta(
        endpoint_id,
        payload.url,
        blueprint.pagination.total_pages_detected,
        blueprint.pagination.rows_per_page,
    )

    total_rows_to_get = payload.rows_override or payload.rows_to_extract or (
        payload.pages_to_scrape * blueprint.pagination.rows_per_page
    )

    # Launch scraping in background task - returns immediately to caller
    task = asyncio.create_task(
        execute_blueprint_scrape(
            blueprint=blueprint,
            rows_to_extract=total_rows_to_get,
            start_page=payload.start_page,
            endpoint_id=endpoint_id,
        )
    )
    ACTIVE_TASKS[endpoint_id] = task

    base_host = str(request.base_url).rstrip("/")
    return {
        "message": "Background scraping started",
        "endpoint_id": endpoint_id,
        "live_api_url": f"{base_host}/api/v1/live/{endpoint_id}",
        "rows_target": total_rows_to_get,
        "start_page": payload.start_page,
    }


@app.get("/api/v1/jobs/status/{endpoint_id}")
async def get_job_status(endpoint_id: str):
    return JOB_STATUS.get(
        endpoint_id,
        {"status": "idle", "current_page": 1, "rows_scraped": 0, "rows_target": 0},
    )


@app.post("/api/v1/jobs/stop/{endpoint_id}")
async def stop_job(endpoint_id: str):
    if endpoint_id in JOB_STATUS:
        JOB_STATUS[endpoint_id]["stop_requested"] = True
        JOB_STATUS[endpoint_id]["status"] = "stopped"
    return {"stopped": True}


@app.get("/api/v1/live/{endpoint_id}")
async def get_live_api_data(
    endpoint_id: str,
    limit: int = Query(default=100000, ge=1, le=100000, description="Max rows to return per call"),
    rows: int | None = Query(default=None, ge=1, le=100000, description="Alias for limit"),
    offset: int = Query(default=0, ge=0, description="Skip rows for pagination"),
    page: int | None = Query(default=None, description="Optional: filter by specific website page_number"),
    start_page: int | None = Query(default=None, description="Alias for page"),
):
    """
    BLAZING-FAST LIVE REST API FOR EXTERNAL PROJECTS!
    Reads directly from indexed SQLite database even while the background scraper is still running.
    """
    effective_limit = rows if rows is not None else limit
    effective_limit = max(1, min(100000, effective_limit))
    effective_page = page if page is not None else start_page

    result = fetch_cached_rows(
        endpoint_id=endpoint_id, limit=effective_limit, offset=offset, page_number=effective_page
    )
    if not result["meta"]:
        raise HTTPException(
            status_code=404,
            detail=f"Endpoint '{endpoint_id}' not found. Analyze the website first.",
        )

    meta = result["meta"]
    job_info = JOB_STATUS.get(endpoint_id, {"status": "idle"})

    return JSONResponse(
        content={
            "source_url": meta["source_url"],
            "scraper_status": job_info.get("status", "idle"),
            "total_pages_on_site": meta["total_pages_detected"],
            "total_rows_estimated_on_site": meta["total_rows_estimated"],
            "total_rows_saved_in_api": result["total_cached"],
            "rows_returned_in_this_response": len(result["rows"]),
            "limit": effective_limit,
            "offset": offset,
            "filtered_page": effective_page,
            "data": result["rows"],
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)