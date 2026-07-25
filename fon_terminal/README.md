# TEFAS Fund Tracker (Fon Terminali) v5.0

A self-updating, full-stack financial dashboard and data pipeline for tracking Turkish mutual fund asset distributions via the TEFAS (Turkish Electronic Fund Trading Platform) API.

This project goes beyond simple data retrieval. It is a FastAPI application that keeps its own database warm on every boot, lets users add or remove tracked funds directly from the UI, and features a hybrid web-scraping/API architecture designed to seamlessly bypass enterprise-grade Web Application Firewalls (WAF) while gracefully handling complex, edge-case financial data.

## Project Overview & Architecture Evolution

**Legacy vs. v4.0 Architecture:**
Earlier iterations of this pipeline relied on traditional HTML DOM parsing. While functional, DOM scraping is inherently fragile and susceptible to unannounced frontend UI updates by TEFAS. v4.0 represented a complete architectural shift: moving away from UI-dependent scraping to a direct API interception model. By targeting the underlying Next.js backend, this evolution drastically reduced execution time, eliminated DOM-related breakage, and established a resilient, enterprise-grade data pipeline.

**v4.0 vs. v5.0 Architecture:**
v4.0 was still a static dashboard: a human had to run `data_scraper.py` on a schedule, hardcode which fund codes to track, and manually edit the frontend to add a new one. v5.0 removes the human from that loop. `main.py` wraps the same scraping engine in a FastAPI application whose `lifespan` hook re-warms the database on every startup, a set of REST endpoints let the dashboard add, hide, or permanently delete funds at runtime, and a soft-delete metadata layer keeps a fund's history alive in the background even after it's removed from the UI, so re-adding it later never starts from a blank slate.

TEFAS publishes fund-level data (price, NAV, shares, and asset distribution), which is highly valuable for portfolio tracking. However, it is not exposed through a stable public API. The core engineering challenges overcome in this architecture include:

### 1. The F5 BIG-IP WAF & Dynamic Token Challenge

**The Problem:** Direct HTTP requests to fund detail URLs are blocked by the site's F5 BIG-IP WAF. Furthermore, TEFAS's Next.js-based frontend protects its internal `/api/funds/` backend with dynamically issued `Authorization: Bearer` session tokens and browser cookies.

**The Solution:** Implemented a **hybrid authentication approach**. A headless `Playwright` browser performs a one-time "handshake"—loading the page just long enough to intercept a real outgoing API request, capturing the `Bearer` token and cookies. It then closes, injecting those credentials into a fast `requests.Session()` to execute direct, bulk POST requests.

### 2. Handling Incomplete Financial Data

**The Problem:** Financial data is inherently messy. Some funds (like the Qualified/Free fund `YAS`) are restricted from sharing daily distributions, while others (like `PHE`) might temporarily liquidate a specific asset, causing that key to vanish from the API response entirely.

**The Solution:** Engineered a robust validation layer. If an asset is completely sold off, the UI intelligently defaults to `0.00%` rather than throwing `undefined` errors. For fully restricted funds, a dual-layer logging system alerts the backend terminal, while the frontend gracefully displays a muted `-` indicator to maintain visual UI harmony.

### 3. Sub-Pixel Rendering & UI Matrix

**The Problem:** The complex data table required simultaneous vertical and horizontal sticky scrolling, which caused browser sub-pixel rendering issues resulting in text "bleeding" through headers.

**The Solution:** Designed a precise CSS matrix using strict `z-index` layering (up to `z-index: 20` for origin corners) and a `top: -1px` physical offset to crush the browser rendering gap, achieving a flawless, zero-bleed scrolling experience in a dark-mode environment.

### 4. Blocking Playwright on an Async Event Loop

**The Problem:** `scrape_and_update()` uses Playwright's *synchronous* API internally. FastAPI's `lifespan` and request handlers run on an `asyncio` event loop, and Playwright's sync API raises immediately if it's ever invoked directly on that loop's thread.

**The Solution:** Every call site that needs to await the scraper from async code (the `lifespan` startup hook) runs it via `asyncio.to_thread()`, moving the blocking Playwright/`requests` pipeline onto a worker thread. The scraper's implementation itself is untouched; only the boundary between async FastAPI code and the sync scraping engine was made non-blocking.

### 5. Removing a Fund Without Losing Its History

**The Problem:** Once a fund's history has been scraped, deleting it outright to "clean up" the dashboard means starting from zero if it's ever added back — and TEFAS doesn't let you fetch arbitrarily old data on demand.

**The Solution:** Every fund entry carries a `_metadata` object (`show_on_ui`, `background_tracking`, `last_scraped_date`) alongside its `records`. Removing a fund from the UI is a soft delete: its tab disappears, but if the user opts to keep tracking it, the `lifespan` startup hook quietly re-scrapes it once every 15 days so its history stays current without wasting a scrape on every single restart. A separate, explicit "Management Panel" and hard-delete endpoint exist for genuinely permanent removal.

## Key Features

**Backend (`main.py` + `data_scraper.py`)**
- **Full-stack FastAPI app:** a single process serves the dashboard (`index.html`, `fund_database.json`) and exposes the management API — no separate static file server is needed.
- **Self-updating lifespan:** on every boot, automatically re-scrapes every fund currently shown on the UI, plus any hidden/background-tracked fund whose last scrape is 15+ days old, before the app starts accepting traffic.
- **Fund lifecycle endpoints:** `POST /api/add-fund`, `POST /api/remove-fund` (soft delete, with optional continued background tracking), and `DELETE /api/hard-delete-fund` (permanent removal).
- **Modular scraping engine:** `scrape_and_update(fund_list)` is the single entry point shared by the CLI, the lifespan hook, and every API endpoint — the Playwright/WAF-bypass logic itself never changes based on who's calling it.
- **Historical Merging:** Upserts into `fund_database.json`, grouping by fund code. Existing dates are overwritten (auto-correcting TEFAS revisions), and new dates are inserted chronologically.
- **Fault Tolerance:** Per-fund error handling ensures one fund's failure doesn't abort the entire run.

**Frontend (`index.html`)**
- **Zero-touch fund management:** a "+ Yeni Fon Ekle" control lets users add a new fund directly from the UI — it POSTs to `/api/add-fund`, shows a loading state, and drops the new tab in without a full page reload.
- **Fund removal & background tracking:** an unobtrusive "×" on each tab opens a confirmation dialog to either keep tracking a fund quietly in the background or delete it permanently.
- **Management Panel:** a dedicated modal listing every background-tracked (UI-hidden) fund with its last scrape date, each with a one-click permanent delete action.
- **Advanced Analytics:** KPI cards, daily share-count changes ("Balina Radarı" / Whale Radar), and an automated daily report summarizing asset allocation shifts.
- **Interactive Visuals:** Zebra-striped data tables with day-over-day change badges, price trend charts, and asset-type mini-charts via Chart.js.

## Tech Stack

- **Backend:** Python 3.9+, FastAPI, Uvicorn, Playwright, Requests
- **Frontend:** HTML5, Vanilla JavaScript, CSS3 (Custom Dark Theme)
- **Charting:** Chart.js (via CDN)

## Installation & Usage

### Prerequisites

- Python 3.9+

### Setup

```bash
git clone <repository-url>
cd fon_terminal

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

(Note: `playwright install chromium` downloads the headless browser binary for the handshake; it runs only once per environment).

### Running the Application

```bash
uvicorn main:app --reload
```

Then open `http://127.0.0.1:8000` in your browser. On startup, the server automatically re-scrapes every fund currently visible on the dashboard (plus any overdue background-tracked one) before it starts accepting requests — watch the terminal for `[STARTUP]` log lines to confirm it finished warming up.

There is no fund list to hardcode anymore: the dashboard ships with `TLY`, `PHE`, and `YAS` tracked by default, and new funds are added from the UI itself via "+ Yeni Fon Ekle" (which calls `POST /api/add-fund` and scrapes that fund immediately).

### Standalone CLI Scraper (optional)

`data_scraper.py` still works as a direct script for scheduled/cron-style runs independent of the web server:

```bash
python data_scraper.py
```

By default this refreshes `TLY`, `PHE`, and `YAS`; edit the `scrape_and_update([...])` call at the bottom of the file to change which funds it covers. Both the CLI and the FastAPI app call the exact same `scrape_and_update()` function, so behavior is identical either way.

## API Reference

| Endpoint | Method | Body / Params | Description |
|----------|--------|----------------|--------------|
| `/api/add-fund` | `POST` | `{"fund_code": "MAC"}` | Scrapes and adds a brand-new fund, visible on the UI immediately. |
| `/api/remove-fund` | `POST` | `{"fund_code": "MAC", "keep_tracking": true}` | Hides a fund from the UI. If `keep_tracking` is `true`, it's still refreshed every 15 days in the background. |
| `/api/hard-delete-fund` | `DELETE` | `?fund_code=MAC` | Permanently deletes a fund and its entire history. Cannot be undone. |

## Data Schema

Each run merges the latest data into `fund_database.json`, grouped by fund code. Every fund entry carries a `_metadata` object (visibility/tracking state) alongside its chronologically-ordered `records`:

```json
{
    "TLY": {
        "_metadata": {
            "show_on_ui": true,
            "background_tracking": false,
            "last_scraped_date": "2026-07-25"
        },
        "records": [
            { "Tarih": "21.07.2026", "Fiyat": 7510.647463, "...": "..." }
        ]
    }
}
```

| Field | Description |
|-------|-------------|
| `_metadata.show_on_ui` | Whether the fund's tab is shown on the dashboard. |
| `_metadata.background_tracking` | Whether a hidden fund is still refreshed periodically (every 15 days). |
| `_metadata.last_scraped_date` | Date (`YYYY-MM-DD`) of the fund's most recent successful scrape. |
| `records[].Tarih` | Record date (DD.MM.YYYY) |
| `records[].Fiyat` | Unit price on that date |
| `records[].Pay` | Shares outstanding |
| `records[].ToplamDeger` | Total fund net asset value (TRY) |
| `records[].Yatirimci` | Number of investors |
| `records[].Varliklar` | Asset type → allocation percentage (can include negative values, e.g. net Repo; keys are translated from raw TEFAS abbreviations to full names) |

**Backward compatibility:** database files created before v5.0 store a bare list of records per fund (no `_metadata`). These are detected and transparently upgraded to the shape above the first time they're loaded — no manual migration step is required, and no historical data is lost.

## Troubleshooting & Notes

- **Handshake Errors:** If you see `[ERROR] [HANDSHAKE] Failed to capture Authorization token`, TEFAS may have updated its flow. Check `acquire_session_credentials()` in `data_scraper.py`.
- **401/403 Status:** If the captured token expires mid-run, simply re-run the script (or restart the server) for a fresh token.
- **Frequent restarts during development:** because the `lifespan` hook re-scrapes on every boot, restarting the server repeatedly (e.g. with `--reload`) will trigger a fresh WAF handshake each time. This is expected in production but can feel slow while iterating locally.
- **Tuning the background-tracking interval:** the 15-day cadence is controlled by `BACKGROUND_TRACKING_INTERVAL_DAYS` in `main.py`.
- **Data Privacy:** `fund_database.json` is treated as local environment data and is ignored via `.gitignore`.
