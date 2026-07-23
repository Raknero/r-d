# TEFAS Fund Data Scraper & Terminal

Automated extraction and visualization of daily mutual fund metrics and asset allocation data from [TEFAS](https://www.tefas.gov.tr) (Turkish Electronic Fund Trading Platform), the official registry operated by the Capital Markets Board of Turkey (SPK).

The project has two parts: a Python scraper that builds a historical fund database, and a static HTML dashboard that reads that database and renders it as an interactive, multi-fund terminal.

## Project Overview

TEFAS publishes fund-level data—price, total net asset value, share count, investor count, and asset distribution—on dynamically rendered fund analysis pages. This data is useful for portfolio tracking and downstream analysis, but it is not exposed through a stable public API. Direct HTTP requests to fund detail URLs are frequently blocked by the site's F5 BIG-IP Web Application Firewall (WAF).

TEFAS's newer Next.js-based fund data page also protects its internal `/api/funds/` backend with a dynamically issued `Authorization: Bearer` session token and browser cookies, so plain `requests.post()` calls are rejected outright. `data_scraper.py` solves this with a hybrid approach: a headless Playwright browser performs a one-time "handshake"—loading the fund data page just long enough to intercept a real outgoing API request and capture its Bearer token and cookies—then immediately closes. Those credentials are injected into a `requests.Session()`, which is used to make fast, direct POST requests to the general-info and portfolio-distribution endpoints for every configured fund. Each run merges the fetched data for the last 30 days per fund into a single master database (`fund_database.json`), preserving all historical records.

`index.html` is a self-contained dashboard that loads `fund_database.json` via `fetch` and presents it as a dark-mode terminal with per-fund tabs, KPI cards, a daily manager-activity report, a zebra-striped data table, and price/volume/asset-allocation charts. It requires no build step or backend—only a static file server.

## Features

**Scraper (`data_scraper.py`)**
- Hybrid architecture: a one-time headless Playwright handshake captures a live Bearer token and session cookies, then a plain `requests.Session()` handles all subsequent API calls for speed
- Calls TEFAS's internal `fonGnlBlgSiraliGetirT` (general info) and `dagilimSiraliGetirT` (portfolio distribution) endpoints directly, fetching the last 30 days of data per fund in each run
- Merges general info and portfolio distribution records by date, preserving raw asset names exactly as returned by the API
- Correctly parses both plain and Turkish-formatted numeric strings, including negative values (e.g. net Repo positions)
- Configurable fund list (`target_funds`); default targets: `TLY`, `PHE`, `YAS`
- Upserts into `fund_database.json`, grouped by fund code: existing dates are overwritten with fresh data (auto-correcting any historical revisions from TEFAS), new dates are inserted, and the fund's record list is kept chronologically sorted
- Per-fund error handling (HTTP status checks, JSON validation) so one fund's failure doesn't abort the run

**Dashboard (`index.html`)**
- Tabbed navigation to switch between funds; adding a new fund only requires appending its code to `FUND_CODES` in the script
- Fully automated data loading via `fetch('fund_database.json')`—no manual data entry required
- Graceful fallback with an on-screen status banner if the database file is missing or empty
- KPI cards for current price, total fund size, and daily share-count changes ("Balina Radarı" whale-activity indicator)
- Automated daily report summarizing which asset allocations increased or decreased
- Filterable, zebra-striped data table (last 7 / 10 / 14 days or full history) with per-cell day-over-day change badges
- Price trend + moving average chart, share-count vs. investor-count chart, and a grid of isolated mini-charts per asset type
- Dark mode throughout

## Tech Stack

| Component | Purpose |
|-----------|---------|
| Python 3 | Scraper runtime |
| [Playwright](https://playwright.dev/python/) | One-time headless browser handshake to capture the TEFAS session token/cookies |
| [Requests](https://requests.readthedocs.io/) | Fast, direct POST calls to the TEFAS backend API using the captured session |
| HTML / CSS / vanilla JavaScript | Dashboard front end (no framework, no build step) |
| [Chart.js](https://www.chartjs.org/) (via CDN) | Charting |

## How to Run

### Prerequisites

- Python 3.9+

### Installation

```bash
git clone <repository-url>
cd fon_terminal

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step downloads the headless browser binary Playwright needs for the one-time authentication handshake; it only needs to be run once per environment.

### Running the scraper

Edit the `target_funds` list in `data_scraper.py` to specify which fund codes to track:

```python
target_funds = ["TLY", "PHE", "YAS"]
```

Run it:

```bash
python data_scraper.py
```

Each run fetches the latest data for every configured fund and merges it into `fund_database.json` in the working directory. The file groups records by fund code:

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
| `PazarPayi` | Fund's market share (%) |
| `Varliklar` | Asset type → allocation percentage (can include negative values, e.g. net Repo) |

Schedule `data_scraper.py` to run daily (e.g. via Task Scheduler or cron) to keep the database up to date; the dashboard requires no changes when new data is added.

### Running the dashboard

Because the dashboard loads `fund_database.json` via `fetch`, it must be served over HTTP rather than opened directly as a `file://` path. From the project directory:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/index.html` in a browser.

### Notes

- Each run launches a headless Chromium instance briefly (a few seconds) just to capture a valid Bearer token and session cookies from TEFAS; it closes immediately afterward, and the rest of the run uses fast direct HTTP requests.
- If you see `[ERROR] [HANDSHAKE] Failed to capture Authorization token`, TEFAS may have changed its authentication flow, or the page took longer than expected to fire an API request—check `acquire_session_credentials()` in `data_scraper.py`.
- If a fund's requests return `HTTP 401`/`403`, the captured token was rejected or expired mid-run; simply re-run the script to perform a fresh handshake.
- `fund_database.json` is treated as local, environment-specific data and is excluded via `.gitignore`; each environment builds its own copy by running the scraper.
