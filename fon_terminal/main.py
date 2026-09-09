"""FastAPI application for the TEFAS Fund Tracker.

Serves the static dashboard (`index.html` + `fund_database.json`) and
exposes a single API endpoint that scrapes and persists data for a new fund
code on demand, reusing the exact same hybrid Playwright + requests pipeline
as the standalone `data_scraper.py` CLI.

Every fund already tracked in `fund_database.json` is refreshed in the
BACKGROUND right after startup, and then again on a fixed interval for as
long as the server runs, so the dashboard keeps serving current data
without needing a restart.

Run with:
    uvicorn main:app --reload
"""
import asyncio
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from data_scraper import DATABASE_FILE, load_database, save_database, scrape_and_update

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / DATABASE_FILE
FUND_CODE_PATTERN = re.compile(r"^[A-Z0-9]{2,10}$")

# Funds hidden from the UI ("show_on_ui": false) but flagged for continued
# background tracking are only re-scraped once their last successful scrape
# is at least this many days old, so restarts don't hammer TEFAS refreshing
# funds nobody is actively looking at.
BACKGROUND_TRACKING_INTERVAL_DAYS = 15

# How often every tracked fund is re-scraped while the server is running.
# TEFAS only publishes END-OF-DAY values (there is no intraday/live feed on
# these endpoints), so polling faster than this buys nothing but load.
REFRESH_INTERVAL_MINUTES = 60

# Serializes every scrape run (startup, periodic, and /api/add-fund) against
# each other. `scrape_and_update` is a read-modify-write cycle over a single
# JSON file, so two concurrent runs could overwrite one another's freshly
# saved funds. A plain `threading.Lock` (not an asyncio one) is used because
# the scraper is synchronous and runs in worker threads on both paths: the
# periodic task via `asyncio.to_thread`, and the sync `/api/add-fund`
# endpoint via FastAPI's own threadpool.
_scrape_lock = threading.Lock()

# How long /api/add-fund waits for an in-flight background refresh to finish
# before giving up, so a user-triggered add can never hang indefinitely.
ADD_FUND_LOCK_WAIT_SECONDS = 300


def _normalize_fund_code(value: str) -> str:
    """Shared fund-code normalization/validation used by every endpoint
    that accepts one, whether via a Pydantic request body or a raw query
    parameter.
    """
    code = value.strip().upper()
    if not FUND_CODE_PATTERN.match(code):
        raise ValueError("fund_code must be 2-10 alphanumeric characters (e.g. 'MAC').")
    return code


def _parse_scraped_date(last_scraped_date):
    """Parses a `_metadata.last_scraped_date` ("YYYY-MM-DD") into a `date`,
    or None when it's missing/unparseable (i.e. "never successfully
    scraped", which callers treat as maximally stale).
    """
    if not last_scraped_date:
        return None
    try:
        return datetime.strptime(last_scraped_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_background_scan_due(last_scraped_date):
    """Returns True if a background-tracked fund's last scrape is missing,
    unparseable, or at least BACKGROUND_TRACKING_INTERVAL_DAYS days old.
    """
    last_scraped = _parse_scraped_date(last_scraped_date)
    if last_scraped is None:
        return True
    return (datetime.now().date() - last_scraped).days >= BACKGROUND_TRACKING_INTERVAL_DAYS


def _load_scan_targets(context="STARTUP"):
    """Reads `fund_database.json` and decides which fund codes should be
    refreshed:
    - every fund currently visible on the UI ("show_on_ui": true, the
      default for funds with no metadata yet), and
    - any hidden-but-background-tracked fund ("show_on_ui": false,
      "background_tracking": true) whose last scrape is 15+ days old (or
      missing), so it stays alive without being re-scraped on every restart.

    Returned STALEST FIRST (funds never scraped come first, then oldest
    `last_scraped_date`), NOT in the database's own insertion order.

    This ordering mattered enormously when a run sent 2 requests per fund
    and paused between them: an interrupted or rate-limited run always left
    a tail unscraped, and in insertion order that tail was deterministic --
    the most recently ADDED funds -- so a new fund could sit stale for days
    while the funds ahead of it were refreshed on every restart.

    `scrape_and_update` now serves every fund from one bulk response, so
    there is no per-fund tail to starve and the order is largely moot. It's
    kept because it still governs the per-fund fallback path, and because
    it costs nothing to have the funds most in need of data listed first.

    Returns an empty list if the database file is missing, empty, or
    unreadable.
    """
    if not os.path.exists(DATABASE_PATH):
        print(f"[{context}] [INFO] {DATABASE_FILE} henuz mevcut degil; tarama atlaniyor.")
        return []

    try:
        with open(DATABASE_PATH, "r", encoding="utf-8") as file:
            database = json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[{context}] [ERROR] {DATABASE_FILE} okunamadi: {exc}. Tarama atlaniyor.")
        return []

    if not isinstance(database, dict):
        print(f"[{context}] [ERROR] {DATABASE_FILE} beklenmeyen bir formatta. Tarama atlaniyor.")
        return []

    scan_targets = []

    for fund_code, entry in database.items():
        metadata = entry.get("_metadata", {}) if isinstance(entry, dict) else {}
        show_on_ui = metadata.get("show_on_ui", True)
        background_tracking = metadata.get("background_tracking", False)
        last_scraped = metadata.get("last_scraped_date")

        if show_on_ui:
            scan_targets.append((fund_code, last_scraped))
            continue

        if not background_tracking:
            continue

        if _is_background_scan_due(last_scraped):
            print(
                f"[{context}] [INFO] '{fund_code}' arka planda takip ediliyor ve son taramanin "
                f"uzerinden {BACKGROUND_TRACKING_INTERVAL_DAYS}+ gun gecti; taramaya dahil edildi."
            )
            scan_targets.append((fund_code, last_scraped))
        else:
            print(
                f"[{context}] [INFO] '{fund_code}' arka planda takip ediliyor, henuz "
                f"{BACKGROUND_TRACKING_INTERVAL_DAYS} gunluk sure dolmadi; atlaniyor."
            )

    # `date.min` for never-scraped funds puts them at the very front.
    scan_targets.sort(key=lambda item: _parse_scraped_date(item[1]) or date.min)
    return [fund_code for fund_code, _ in scan_targets]


def _run_scrape_locked(fund_codes, context, wait_seconds=None):
    """Runs `scrape_and_update` while holding `_scrape_lock`, so scrape runs
    can never interleave and clobber each other's writes to the shared JSON
    database.

    `wait_seconds=None` means "don't wait": if another scrape is already in
    flight the run is skipped and None is returned, which is what the
    periodic refresh wants (the next tick will pick it up anyway). A numeric
    `wait_seconds` blocks for at most that long, which is what a
    user-triggered /api/add-fund wants.

    Always called from a worker thread, never from the event loop.
    """
    if wait_seconds is None:
        acquired = _scrape_lock.acquire(blocking=False)
    else:
        acquired = _scrape_lock.acquire(timeout=wait_seconds)

    if not acquired:
        print(f"[{context}] [INFO] Baska bir tarama devam ediyor; bu calisma atlandi.")
        return None

    try:
        return scrape_and_update(fund_codes)
    finally:
        _scrape_lock.release()


async def _refresh_tracked_funds(context):
    """Refreshes every tracked fund once, off the event loop, and logs a
    one-line summary. Never raises: a failed refresh must not take the
    server (or the periodic loop) down with it.
    """
    scan_targets = _load_scan_targets(context=context)

    if not scan_targets:
        print(f"[{context}] [INFO] Takip edilen fon bulunamadi; guncelleme atlaniyor.")
        return

    print(f"[{context}] {len(scan_targets)} fon guncelleniyor: {', '.join(scan_targets)}")

    try:
        # scrape_and_update() uses Playwright's *sync* API internally, which
        # raises if invoked directly on the asyncio event loop thread.
        # Running it in a worker thread via asyncio.to_thread() keeps it
        # awaitable here without blocking the loop or touching its sync
        # implementation.
        results = await asyncio.to_thread(_run_scrape_locked, scan_targets, context)
    except RuntimeError as exc:
        print(f"[{context}] [ERROR] TEFAS oturumu acilamadi, guncelleme atlandi: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - a bad refresh must never kill the loop
        print(f"[{context}] [ERROR] Guncelleme sirasinda beklenmeyen hata: {exc}")
        return

    if results is None:
        return

    succeeded = [code for code, result in results.items() if result.get("status") == "success"]
    failed = [code for code, result in results.items() if result.get("status") != "success"]

    print(f"[{context}] Guncelleme tamamlandi. Basarili: {len(succeeded)}, Basarisiz: {len(failed)}.")
    if failed:
        print(f"[{context}] [WARNING] Guncellenemeyen fonlar: {', '.join(failed)}")


async def _periodic_refresh_loop():
    """Re-scrapes every tracked fund every REFRESH_INTERVAL_MINUTES for as
    long as the server runs.

    Without this, data only ever refreshed on startup, on /api/add-fund, or
    via a manual CLI run -- so a long-running server served data that got
    steadily older the longer it stayed up.
    """
    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL_MINUTES * 60)
        except asyncio.CancelledError:
            raise
        await _refresh_tracked_funds("REFRESH")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the background refresh machinery, then immediately hands
    control back to FastAPI/uvicorn.

    The startup refresh deliberately runs as a BACKGROUND TASK rather than
    being awaited here. Awaiting it meant the dashboard served no traffic
    at all until every tracked fund had been scraped (~4-5s per fund, so
    roughly a minute for a dozen funds) -- long enough that interrupting
    the wait was the natural thing to do, which in turn left the tail of
    the scan list unrefreshed. Serving immediately and filling data in
    behind the scenes removes that incentive entirely: the UI comes up at
    once with whatever is already on disk, and each fund's numbers update
    as its scrape lands.
    """
    print("=" * 70)
    print("[STARTUP] TEFAS Fund Tracker API baslatiliyor...")

    startup_task = asyncio.create_task(_refresh_tracked_funds("STARTUP"))
    refresh_task = asyncio.create_task(_periodic_refresh_loop())

    print(
        f"[STARTUP] Baslangic guncellemesi arka planda basladi; periyodik yenileme her "
        f"{REFRESH_INTERVAL_MINUTES} dakikada bir calisacak."
    )
    print("[STARTUP] Uygulama hazir: API ve statik sunucu istekleri kabul ediyor.")
    print("=" * 70)

    yield

    print("[SHUTDOWN] TEFAS Fund Tracker API kapatiliyor.")
    for task in (startup_task, refresh_task):
        task.cancel()
    # An in-flight scrape runs in a worker thread and can't be interrupted
    # mid-request; gathering here just reaps the cancelled awaitables so
    # shutdown doesn't log "Task was destroyed but it is pending".
    await asyncio.gather(startup_task, refresh_task, return_exceptions=True)


app = FastAPI(title="TEFAS Fund Tracker API", version="1.0.0", lifespan=lifespan)


class AddFundRequest(BaseModel):
    fund_code: str

    @field_validator("fund_code")
    @classmethod
    def validate_fund_code(cls, value: str) -> str:
        return _normalize_fund_code(value)


class RemoveFundRequest(BaseModel):
    fund_code: str
    keep_tracking: bool = False

    @field_validator("fund_code")
    @classmethod
    def validate_fund_code(cls, value: str) -> str:
        return _normalize_fund_code(value)


@app.post("/api/add-fund")
def add_fund(payload: AddFundRequest):
    """Scrapes and persists data for a single new fund code.

    NOTE: this is a synchronous route handler on purpose. FastAPI runs sync
    `def` endpoints in a worker thread pool, so the blocking Playwright/
    requests pipeline (including a fresh WAF handshake) does not stall the
    server's event loop, though the request itself can take several
    seconds to complete.

    The scrape goes through `_run_scrape_locked` so it can't run at the
    same time as the startup/periodic refresh: both paths are a
    read-modify-write cycle over the same JSON file, and interleaving them
    would let one overwrite funds the other had just saved.
    """
    fund_code = payload.fund_code

    try:
        results = _run_scrape_locked(
            [fund_code], context="ADD-FUND", wait_seconds=ADD_FUND_LOCK_WAIT_SECONDS
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error while scraping '{fund_code}': {exc}",
        ) from exc

    if results is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Arka planda devam eden bir fon guncellemesi zaman asimina kadar bitmedi; "
                "lutfen birkac dakika sonra tekrar deneyin."
            ),
        )

    result = results.get(fund_code, {"status": "error", "message": "No result returned."})

    if result.get("status") != "success":
        raise HTTPException(
            status_code=502,
            detail=result.get("message", f"Failed to fetch data for '{fund_code}'."),
        )

    return {"fund_code": fund_code, **result}


@app.post("/api/remove-fund")
def remove_fund(payload: RemoveFundRequest):
    """Hides a fund from the UI (soft delete) without touching its stored
    history. If `keep_tracking` is true, the fund is additionally flagged
    for periodic background refreshes (see the lifespan startup hook) so
    re-adding it later doesn't lose continuity; otherwise it's simply
    frozen in place until re-added or hard-deleted.
    """
    fund_code = payload.fund_code
    database = load_database()

    if fund_code not in database:
        raise HTTPException(status_code=404, detail=f"'{fund_code}' fon veritabaninda bulunamadi.")

    database[fund_code]["_metadata"]["show_on_ui"] = False
    database[fund_code]["_metadata"]["background_tracking"] = payload.keep_tracking
    save_database(database)

    print(
        f"[MANAGE] '{fund_code}' arayuzden kaldirildi. Arka plan takibi: "
        f"{'Acik' if payload.keep_tracking else 'Kapali'}."
    )

    return {
        "fund_code": fund_code,
        "show_on_ui": False,
        "background_tracking": payload.keep_tracking,
    }


@app.delete("/api/hard-delete-fund")
def hard_delete_fund(fund_code: str = Query(..., min_length=2, max_length=10)):
    """Permanently removes a fund and all of its stored history from
    `fund_database.json`. This cannot be undone; re-adding the same fund
    code later starts from a blank slate.
    """
    try:
        normalized_code = _normalize_fund_code(fund_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    database = load_database()

    if normalized_code not in database:
        raise HTTPException(status_code=404, detail=f"'{normalized_code}' fon veritabaninda bulunamadi.")

    del database[normalized_code]
    save_database(database)

    print(f"[MANAGE] '{normalized_code}' fonu veritabanindan kalici olarak silindi.")

    return {"fund_code": normalized_code, "status": "deleted"}


# Mounted last (and at the root path) so it acts as a catch-all: it serves
# index.html at "/" and fund_database.json (and any other static asset)
# alongside it, without ever shadowing the "/api/add-fund" route registered
# above it.
app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")
