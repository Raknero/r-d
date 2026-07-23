# TEFAS Fund Tracker (Fon Terminali) v4.0

An automated, robust financial dashboard and data pipeline for tracking Turkish mutual fund asset distributions via the TEFAS (Turkish Electronic Fund Trading Platform) API.

This project goes beyond simple data retrieval. It features a hybrid web-scraping/API architecture designed to seamlessly bypass enterprise-grade Web Application Firewalls (WAF) and a meticulously crafted UI that gracefully handles complex, edge-case financial data.

## Project Overview & Architecture Evolution

**Legacy vs. v4.0 Architecture:**
Earlier iterations of this pipeline relied on traditional HTML DOM parsing. While functional, DOM scraping is inherently fragile and susceptible to unannounced frontend UI updates by TEFAS. v4.0 represents a complete architectural shift: moving away from UI-dependent scraping to a direct API interception model. By targeting the underlying Next.js backend, this evolution drastically reduced execution time, eliminated DOM-related breakage, and established a resilient, enterprise-grade data pipeline.

TEFAS publishes fund-level data (price, NAV, shares, and asset distribution), which is highly valuable for portfolio tracking. However, it is not exposed through a stable public API. The core engineering challenges overcome in this v4.0 architecture include:

### 1. The F5 BIG-IP WAF & Dynamic Token Challenge

**The Problem:** Direct HTTP requests to fund detail URLs are blocked by the site's F5 BIG-IP WAF. Furthermore, TEFAS's Next.js-based frontend protects its internal `/api/funds/` backend with dynamically issued `Authorization: Bearer` session tokens and browser cookies.

**The Solution:** Implemented a **hybrid authentication approach**. A headless `Playwright` browser performs a one-time "handshake"—loading the page just long enough to intercept a real outgoing API request, capturing the `Bearer` token and cookies. It then closes, injecting those credentials into a fast `requests.Session()` to execute direct, bulk POST requests.

### 2. Handling Incomplete Financial Data

**The Problem:** Financial data is inherently messy. Some funds (like the Qualified/Free fund `YAS`) are restricted from sharing daily distributions, while others (like `PHE`) might temporarily liquidate a specific asset, causing that key to vanish from the API response entirely.

**The Solution:** Engineered a robust validation layer. If an asset is completely sold off, the UI intelligently defaults to `0.00%` rather than throwing `undefined` errors. For fully restricted funds, a dual-layer logging system alerts the backend terminal, while the frontend gracefully displays a muted `-` indicator to maintain visual UI harmony.

### 3. Sub-Pixel Rendering & UI Matrix

**The Problem:** The complex data table required simultaneous vertical and horizontal sticky scrolling, which caused browser sub-pixel rendering issues resulting in text "bleeding" through headers.

**The Solution:** Designed a precise CSS matrix using strict `z-index` layering (up to `z-index: 20` for origin corners) and a `top: -1px` physical offset to crush the browser rendering gap, achieving a flawless, zero-bleed scrolling experience in a dark-mode environment.

## Key Features

**Backend (`data_scraper.py`)**
- **Hybrid Extraction:** Captures live tokens via Playwright, then handles bulk API calls via Python `requests` for speed.
- **Historical Merging:** Upserts into `fund_database.json`, grouping by fund code. Existing dates are overwritten (auto-correcting TEFAS revisions), and new dates are inserted chronologically.
- **Fault Tolerance:** Per-fund error handling ensures one fund's failure doesn't abort the entire daily run.

**Frontend (`index.html`)**
- **Zero-Build Dashboard:** Fully automated loading via `fetch('fund_database.json')`—no framework or manual data entry required.
- **Advanced Analytics:** KPI cards, daily share-count changes ("Balina Radarı" / Whale Radar), and an automated daily report summarizing asset allocation shifts.
- **Interactive Visuals:** Zebra-striped data tables with day-over-day change badges, price trend charts, and asset-type mini-charts via Chart.js.

## Tech Stack

- **Backend:** Python 3.9+, Playwright, Requests
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

### Running the Scraper

Edit the `target_funds` list in `data_scraper.py` (default: `TLY`, `PHE`, `YAS`):

```python
target_funds = ["TLY", "PHE", "YAS"]
```

Run the scraper:

```bash
python data_scraper.py
```

Tip: Schedule this script to run daily via cron or Task Scheduler to keep the local `fund_database.json` up to date.

## Data Schema

Each run merges the latest data into `fund_database.json`, grouped by fund code, with each fund's records kept in chronological order:

```json
{
    "TLY": [ { "Tarih": "21.07.2026", "Fiyat": 7510.647463, "...": "..." } ],
    "PHE": [ { "...": "..." } ],
    "YAS": [ { "...": "..." } ]
}
```

| Field | Description |
|-------|-------------|
| `Tarih` | Record date (DD.MM.YYYY) |
| `Fiyat` | Unit price on that date |
| `Pay` | Shares outstanding |
| `ToplamDeger` | Total fund net asset value (TRY) |
| `Yatirimci` | Number of investors |
| `Varliklar` | Asset type → allocation percentage (can include negative values, e.g. net Repo; keys are translated from raw TEFAS abbreviations to full names) |

### Running the Dashboard

Because the dashboard uses the Fetch API, it must be served over HTTP:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/index.html` in your browser.

## Troubleshooting & Notes

- **Handshake Errors:** If you see `[ERROR] [HANDSHAKE] Failed to capture Authorization token`, TEFAS may have updated its flow. Check `acquire_session_credentials()` in `data_scraper.py`.
- **401/403 Status:** If the captured token expires mid-run, simply re-run the script for a fresh token.
- **Data Privacy:** `fund_database.json` is treated as local environment data and is ignored via `.gitignore`.
