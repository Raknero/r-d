# TEFAS Fund Tracker (Fon Terminali) v5.2

A self-updating, full-stack financial dashboard and data pipeline for tracking Turkish mutual fund asset distributions via the TEFAS (Turkish Electronic Fund Trading Platform) API.

This project goes beyond simple data retrieval. It is a FastAPI application that keeps its own database warm on every boot, lets users add or remove tracked funds directly from the UI, and features a hybrid web-scraping/API architecture designed to seamlessly bypass enterprise-grade Web Application Firewalls (WAF) while gracefully handling complex, edge-case financial data.

## Project Overview & Architecture Evolution

**Legacy vs. v4.0 Architecture:**
Earlier iterations of this pipeline relied on traditional HTML DOM parsing. While functional, DOM scraping is inherently fragile and susceptible to unannounced frontend UI updates by TEFAS. v4.0 represented a complete architectural shift: moving away from UI-dependent scraping to a direct API interception model. By targeting the underlying Next.js backend, this evolution drastically reduced execution time, eliminated DOM-related breakage, and established a resilient, enterprise-grade data pipeline.

**v4.0 vs. v5.0 Architecture:**
v4.0 was still a static dashboard: a human had to run `data_scraper.py` on a schedule, hardcode which fund codes to track, and manually edit the frontend to add a new one. v5.0 removes the human from that loop. `main.py` wraps the same scraping engine in a FastAPI application whose `lifespan` hook re-warms the database on every startup, a set of REST endpoints let the dashboard add, hide, or permanently delete funds at runtime, and a soft-delete metadata layer keeps a fund's history alive in the background even after it's removed from the UI, so re-adding it later never starts from a blank slate.

**v5.0 vs. v5.1 Architecture:**
v5.0 paid the full cost of a Playwright handshake on *every single* `scrape_and_update()` call — once per API request, once per fund in the startup scan, every time. v5.1 introduces an in-memory token cache: the Bearer token and cookies captured by Playwright are reused across calls for as long as TEFAS keeps accepting them, so adding a fund from the UI or running the 15-day background scan no longer launches a fresh browser unless it truly has to. A smart-retry layer backstops this cache — if TEFAS ever responds with `401`/`403` mid-run, the stale token is invalidated, a new one is acquired transparently, and the failed request is retried once, all without interrupting the scrape.

**v5.1 vs. v5.2 Architecture:**
v5.1 could fetch data efficiently but had no answer for being *throttled*. It scraped funds in database insertion order, treated TEFAS's `HTTP 429` as a permanent failure, and only refreshed at boot — so once the fund count grew past what TEFAS would serve in one burst, the funds at the end of the list were dropped from every run and quietly served days-old numbers. v5.2 makes the pipeline survive its own rate limit: `429` is retried with exponential backoff instead of abandoned, funds are scanned stalest-first so a throttled tail rotates rather than starving, the startup scan moved off the request path so the dashboard is never held hostage to it, and an hourly refresh keeps a long-running server current instead of drifting until the next restart.

TEFAS publishes fund-level data (price, NAV, shares, and asset distribution), which is highly valuable for portfolio tracking. However, it is not exposed through a stable public API. The core engineering challenges overcome in this architecture include:

### 1. The F5 BIG-IP WAF & Dynamic Token Challenge

**The Problem:** Direct HTTP requests to fund detail URLs are blocked by the site's F5 BIG-IP WAF. Furthermore, TEFAS's Next.js-based frontend protects its internal `/api/funds/` backend with dynamically issued `Authorization: Bearer` session tokens and browser cookies.

**The Solution:** Implemented a **hybrid authentication approach**. A headless `Playwright` browser performs a one-time "handshake"—loading the page just long enough to intercept a real outgoing API request, capturing the `Bearer` token and cookies. It then closes, injecting those credentials into a fast `requests.Session()` to execute direct, bulk POST requests.

### 2. Handling Incomplete Financial Data

**The Problem:** Financial data pipelines are inherently subject to upstream API inconsistencies. Due to platform-side quirks on TEFAS, certain funds (like `YAS`) do not return distribution data through the bulk search endpoints. Additionally, funds (like `PHE`) might temporarily liquidate a specific asset, causing that asset's key to vanish from the API response entirely.

**The Solution:** Engineered a robust validation layer. If an asset is completely sold off and missing from the payload, the UI intelligently defaults to `0.00%` rather than throwing `undefined` errors. For funds that return empty datasets from the TEFAS API, a dual-layer logging system alerts the backend terminal, while the frontend gracefully displays a muted `-` indicator to maintain visual UI harmony without breaking the application state.

### 3. Sub-Pixel Rendering & UI Matrix

**The Problem:** The complex data table required simultaneous vertical and horizontal sticky scrolling, which caused browser sub-pixel rendering issues resulting in text "bleeding" through headers.

**The Solution:** Designed a precise CSS matrix using strict `z-index` layering (up to `z-index: 20` for origin corners) and a `top: -1px` physical offset to crush the browser rendering gap, achieving a flawless, zero-bleed scrolling experience in a dark-mode environment.

### 4. Blocking Playwright on an Async Event Loop

**The Problem:** `scrape_and_update()` uses Playwright's *synchronous* API internally. FastAPI's `lifespan` and request handlers run on an `asyncio` event loop, and Playwright's sync API raises immediately if it's ever invoked directly on that loop's thread.

**The Solution:** Every call site that needs to await the scraper from async code (the `lifespan` startup hook) runs it via `asyncio.to_thread()`, moving the blocking Playwright/`requests` pipeline onto a worker thread. The scraper's implementation itself is untouched; only the boundary between async FastAPI code and the sync scraping engine was made non-blocking.

### 5. Removing a Fund Without Losing Its History

**The Problem:** Once a fund's history has been scraped, deleting it outright to "clean up" the dashboard means starting from zero if it's ever added back — and TEFAS doesn't let you fetch arbitrarily old data on demand.

**The Solution:** Every fund entry carries a `_metadata` object (`show_on_ui`, `background_tracking`, `last_scraped_date`) alongside its `records`. Removing a fund from the UI is a soft delete: its tab disappears, but if the user opts to keep tracking it, the `lifespan` startup hook quietly re-scrapes it once every 15 days so its history stays current without wasting a scrape on every single restart. A separate, explicit "Management Panel" and hard-delete endpoint exist for genuinely permanent removal.

### 6. Repeated Playwright Handshakes on Every Scrape

**The Problem:** `scrape_and_update()` is called far more often than once — every `POST /api/add-fund` request and every fund in the 15-day background scan each triggered their own independent Playwright handshake, even though the previously captured token was often still perfectly valid. This wasted seconds per call and increased the surface area for WAF detection.

**The Solution:** Implemented a **token caching and smart retry** layer. A module-level cache holds the last captured `Authorization` header and cookie string; `get_session_credentials()` returns this cache directly whenever it's populated, skipping Playwright entirely. If TEFAS ever rejects the cached token with a `401`/`403` (e.g. it expired between calls), `fetch_endpoint_data()` clears the cache, re-triggers the Playwright handshake for a fresh token, rebuilds the `requests.Session`, and retries the failed request exactly once — transparently, with no change to `scrape_and_update()`'s public signature.

**Measured impact:** benchmarked directly against the live TEFAS site (headless browser launch + navigation + token capture) — a cold handshake takes ~5.9-6.8s, while a cached lookup completes in well under 1ms. That is a **~100% reduction (roughly 6 seconds saved) on every `scrape_and_update()` call that can reuse an already-valid session** — e.g. every fund after the first in a multi-fund background scan, or any `/api/add-fund` request that arrives while a previous token is still valid.

### 7. Newly Added Funds Silently Stuck on Days-Old Data

**The Problem:** Funds added most recently showed data 2-3 days stale while the funds added first were always current, and the console never showed the stale ones being fetched. Four separate defects compounded into that one symptom:

1. **TEFAS rate limiting was treated as a permanent failure.** TEFAS answers a burst of API calls with `HTTP 429`. Measured 2026-09-04: scraping 11 funds back-to-back succeeded for the first 6, then returned `429` for *every* remaining request. `fetch_endpoint_data()` lumped `429` in with all other non-`200` codes — log once, give up — so those funds were dropped from the run and reported as `No general info retrieved (invalid fund code, or no data for this period)`, blaming the fund code for what was really a throttle.
2. **The scan always ran in database insertion order.** Since a newly added fund is appended last, the *same* trailing funds hit the rate limit on every single run. The failure wasn't random — it was pinned to whichever funds were added most recently.
3. **The startup scan blocked all traffic.** `lifespan` awaited the full scrape before serving a single request (~4-5s per fund, so about a minute for a dozen funds). Interrupting that wait — the natural reaction to a dashboard that won't load — left the tail of the list unscraped.
4. **The Playwright handshake opened a *visible* browser window.** `acquire_session_credentials()` had `headless=False` set in source — deliberately, since watching the challenge get solved is the only practical way to debug a handshake failure, but it then applied to *every* caller, including the server's background refresh where no one is watching the window. Closing it raised `Page.wait_for_timeout: Target page, context or browser has been closed` from inside the poll loop, and because the handshake happens *before* the fund loop, that exception escaped past `scrape_and_update()`'s per-fund `try/except` — one closed window meant **zero** funds refreshed, with nothing in the logs pointing at the window as the cause.

**The Solution:**

- `429` is now handled as the transient condition it is: retried with exponential backoff (20s → 40s → 80s, capped at 120s), honoring TEFAS's `Retry-After` header when present, with jitter so parallel retries don't re-synchronize into another burst. The inter-fund pause also rose from 1.5-3.5s to 5-9s, which keeps a full multi-fund run under the limiter instead of tripping it halfway through.
- Scan order is now **stalest-first** (never-scraped funds first, then oldest `last_scraped_date`). An interrupted or throttled run still leaves *some* tail unfinished, but that tail now rotates: whatever missed out is first in line next time, so no fund can starve indefinitely.
- The startup scan runs as a **background task**. The dashboard serves immediately (measured 0.04s for `index.html`, 0.18s for `fund_database.json`) with whatever is already on disk, and each fund's numbers update as its scrape lands — removing the incentive to interrupt.
- A **periodic refresh** repeats the same scan every 60 minutes for as long as the server runs, so data no longer ages until someone restarts the process. 60 minutes is deliberate rather than aggressive: TEFAS only publishes end-of-day values on these endpoints, so polling faster would buy no fresher numbers while pushing the run back toward the limiter.
- The visible browser became a **toggle instead of a source edit**: headless by default (`HANDSHAKE_HEADLESS`), with `TEFAS_HANDSHAKE_HEADLESS=0` (or `headless=False` passed directly) restoring the window for debugging. Independently of the default, any browser-level failure now degrades to the normal "no token" path, so closing the window mid-handshake costs that one handshake rather than the whole run.
- Every scrape path (startup, periodic, `/api/add-fund`) is serialized behind one lock, since each is a read-modify-write cycle over the same JSON file and interleaving them could let one overwrite funds the other had just saved.

## Key Features

**Backend (`main.py` + `data_scraper.py`)**
- **Full-stack FastAPI app:** a single process serves the dashboard (`index.html`, `fund_database.json`) and exposes the management API — no separate static file server is needed.
- **Self-updating lifespan:** on every boot, automatically re-scrapes every fund currently shown on the UI, plus any hidden/background-tracked fund whose last scrape is 15+ days old — in the background, so the dashboard serves traffic immediately instead of waiting out the scan.
- **Hourly background refresh:** a periodic task repeats that same scan every 60 minutes (`REFRESH_INTERVAL_MINUTES`) for as long as the server runs, so a long-lived process never drifts onto stale data between restarts.
- **Stalest-first scan order & rate-limit backoff:** funds are refreshed oldest-data-first, and TEFAS `429` responses are retried with exponential backoff (honoring `Retry-After`) rather than dropping the fund — so a throttled run recovers instead of permanently starving whichever funds sit at the end of the list.
- **Fund lifecycle endpoints:** `POST /api/add-fund`, `POST /api/remove-fund` (soft delete, with optional continued background tracking), and `DELETE /api/hard-delete-fund` (permanent removal).
- **Modular scraping engine:** `scrape_and_update(fund_list)` is the single entry point shared by the CLI, the lifespan hook, and every API endpoint — the Playwright/WAF-bypass logic itself never changes based on who's calling it.
- **Token caching & smart retry:** the Playwright-captured session (Bearer token + cookies) is cached in memory and reused across calls — measured at ~100% faster (~6s saved) per call versus repeating the handshake — while a `401`/`403` from TEFAS automatically invalidates the cache, re-authenticates, and retries the failed request once, no manual restarts required.
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

Then open `http://127.0.0.1:8000` in your browser. The dashboard is served **immediately**; the startup refresh runs in the background and each fund's numbers update as its scrape lands. Watch the terminal for `[STARTUP]` log lines to follow progress, and `[REFRESH]` lines for the hourly re-scrape that follows.

There is no fund list to hardcode anywhere: whatever is tracked in `fund_database.json` is what gets scanned, and new funds are added from the UI itself via "+ Yeni Fon Ekle" (which calls `POST /api/add-fund` and scrapes that fund immediately).

### Standalone CLI Scraper (optional)

`data_scraper.py` still works as a direct script for scheduled/cron-style runs independent of the web server:

```bash
# Refresh every fund tracked in fund_database.json
python data_scraper.py

# ...or just specific ones
python data_scraper.py TLY PHE
```

With no arguments it refreshes **every fund in `fund_database.json`**; pass fund codes to narrow it. (Before this was fixed, the CLI had a hardcoded `["TLY", "PHE", "YAS"]` list that silently rotted as funds were added and removed through the UI — it refreshed three funds, one of which no longer existed in the database, while leaving every other tracked fund untouched.) Both the CLI and the FastAPI app call the exact same `scrape_and_update()` function, so behavior is identical either way.

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
- **401/403 Status:** Handled automatically. `fetch_endpoint_data()` clears the cached token, re-runs the Playwright handshake, and retries the failed request once. Watch for `[CACHE] Cached TEFAS session token invalidated.` in the logs to confirm this happened; if the retry also fails, the fund is skipped for that run and the underlying error is reported.
- **`[CACHE] Reusing cached TEFAS session token...`:** this is expected and desirable — it means `scrape_and_update()` reused an already-valid session instead of launching a new browser. The cache lives only for the lifetime of the running process, so it resets on every server restart.
- **`HTTP 429` in the logs:** TEFAS is rate limiting, not rejecting the fund. `fetch_endpoint_data()` retries with exponential backoff (honoring `Retry-After`); look for `HTTP 429 (TEFAS istek limiti) dondurdu; Ns beklenip tekrar denenecek`. If a fund still fails after the retry budget, it keeps its old `last_scraped_date` and therefore moves to the **front** of the next scan, so it recovers on the following run. Restarting the server repeatedly in quick succession is the fastest way to provoke this, since each boot kicks off a full scan.
- **Watching the handshake (or an unexpected browser window):** set `TEFAS_HANDSHAKE_HEADLESS=0` to make the Chromium window visible — useful if TEFAS changes its challenge or starts blocking headless fingerprints. If a window appears when you didn't ask for one, that variable is set in your environment. Closing it mid-handshake is now safe for the process (the failure is caught and reported like any other missing token), but that handshake still yields no credentials, so the funds it was serving are skipped until the next scan.
- **Frequent restarts during development:** because the startup scan runs on every boot and the token cache doesn't persist across process restarts, restarting the server repeatedly (e.g. with `--reload`) triggers a fresh WAF handshake each time — and, since each boot also re-scans every fund, is the main way to hit the `429` limiter locally.
- **Tuning the intervals:** the 15-day background-tracking cadence is `BACKGROUND_TRACKING_INTERVAL_DAYS` and the hourly re-scrape is `REFRESH_INTERVAL_MINUTES`, both in `main.py`. Rate-limit backoff and the inter-fund pause are `RATE_LIMIT_*` and `INTER_FUND_DELAY_RANGE_SECONDS` in `data_scraper.py`.
- **No intraday data:** TEFAS publishes **end-of-day** values on these endpoints. There is no live/intraday feed to scrape, so "right now" pricing is not obtainable — the freshest possible figure is the current day's published value.
- **Data Privacy:** `fund_database.json` is treated as local environment data and is ignored via `.gitignore`.
