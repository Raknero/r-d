"""FastAPI application for the TEFAS Fund Tracker.

Serves the static dashboard (`index.html` + `fund_database.json`) and
exposes a single API endpoint that scrapes and persists data for a new fund
code on demand, reusing the exact same hybrid Playwright + requests pipeline
as the standalone `data_scraper.py` CLI.

On startup, every fund already tracked in `fund_database.json` is refreshed
once via the same pipeline, so the dashboard never serves stale data right
after a (re)deploy or restart.

Run with:
    uvicorn main:app --reload
"""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
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


def _normalize_fund_code(value: str) -> str:
    """Shared fund-code normalization/validation used by every endpoint
    that accepts one, whether via a Pydantic request body or a raw query
    parameter.
    """
    code = value.strip().upper()
    if not FUND_CODE_PATTERN.match(code):
        raise ValueError("fund_code must be 2-10 alphanumeric characters (e.g. 'MAC').")
    return code


def _is_background_scan_due(last_scraped_date):
    """Returns True if a background-tracked fund's last scrape is missing,
    unparseable, or at least BACKGROUND_TRACKING_INTERVAL_DAYS days old.
    """
    if not last_scraped_date:
        return True
    try:
        last_scraped = datetime.strptime(last_scraped_date, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (datetime.now().date() - last_scraped).days >= BACKGROUND_TRACKING_INTERVAL_DAYS


def _load_startup_scan_targets():
    """Reads `fund_database.json` and decides which fund codes should be
    refreshed on startup:
    - every fund currently visible on the UI ("show_on_ui": true, the
      default for funds with no metadata yet), and
    - any hidden-but-background-tracked fund ("show_on_ui": false,
      "background_tracking": true) whose last scrape is 15+ days old (or
      missing), so it stays alive without being re-scraped on every restart.

    Returns an empty list if the database file is missing, empty, or
    unreadable.
    """
    if not os.path.exists(DATABASE_PATH):
        print(f"[STARTUP] [INFO] {DATABASE_FILE} henuz mevcut degil; baslangic taramasi atlaniyor.")
        return []

    try:
        with open(DATABASE_PATH, "r", encoding="utf-8") as file:
            database = json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[STARTUP] [ERROR] {DATABASE_FILE} okunamadi: {exc}. Baslangic taramasi atlaniyor.")
        return []

    if not isinstance(database, dict):
        print(f"[STARTUP] [ERROR] {DATABASE_FILE} beklenmeyen bir formatta. Baslangic taramasi atlaniyor.")
        return []

    scan_targets = []

    for fund_code, entry in database.items():
        metadata = entry.get("_metadata", {}) if isinstance(entry, dict) else {}
        show_on_ui = metadata.get("show_on_ui", True)
        background_tracking = metadata.get("background_tracking", False)

        if show_on_ui:
            scan_targets.append(fund_code)
            continue

        if not background_tracking:
            continue

        if _is_background_scan_due(metadata.get("last_scraped_date")):
            print(
                f"[STARTUP] [INFO] '{fund_code}' arka planda takip ediliyor ve son taramanin "
                f"uzerinden {BACKGROUND_TRACKING_INTERVAL_DAYS}+ gun gecti; taramaya dahil edildi."
            )
            scan_targets.append(fund_code)
        else:
            print(
                f"[STARTUP] [INFO] '{fund_code}' arka planda takip ediliyor, henuz "
                f"{BACKGROUND_TRACKING_INTERVAL_DAYS} gunluk sure dolmadi; atlaniyor."
            )

    return scan_targets


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Refreshes every already-tracked fund once before the app starts
    accepting traffic, then hands control back to FastAPI/uvicorn.
    """
    print("=" * 70)
    print("[STARTUP] TEFAS Fund Tracker API baslatiliyor...")

    scan_targets = _load_startup_scan_targets()

    if scan_targets:
        print(
            f"[STARTUP] {len(scan_targets)} fon icin baslangic guncellemesi "
            f"tetikleniyor: {', '.join(scan_targets)}"
        )
        try:
            # scrape_and_update() uses Playwright's *sync* API internally,
            # which raises if invoked directly on the asyncio event loop
            # thread. Running it in a worker thread via asyncio.to_thread()
            # keeps it awaitable here without blocking the loop or touching
            # its sync implementation.
            results = await asyncio.to_thread(scrape_and_update, scan_targets)
            succeeded = [code for code, result in results.items() if result.get("status") == "success"]
            failed = [code for code, result in results.items() if result.get("status") != "success"]

            print(
                f"[STARTUP] Baslangic guncellemesi tamamlandi. "
                f"Basarili: {len(succeeded)}, Basarisiz: {len(failed)}."
            )
            if failed:
                print(f"[STARTUP] [WARNING] Guncellenemeyen fonlar: {', '.join(failed)}")
        except RuntimeError as exc:
            print(f"[STARTUP] [ERROR] TEFAS oturumu acilamadi, baslangic guncellemesi atlandi: {exc}")
        except Exception as exc:
            print(f"[STARTUP] [ERROR] Baslangic guncellemesi sirasinda beklenmeyen hata: {exc}")
    else:
        print("[STARTUP] [INFO] Takip edilen fon bulunamadi; baslangic guncellemesi atlaniyor.")

    print("[STARTUP] Uygulama hazir: API ve statik sunucu istekleri kabul ediyor.")
    print("=" * 70)

    yield

    print("[SHUTDOWN] TEFAS Fund Tracker API kapatiliyor.")


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
    """
    fund_code = payload.fund_code

    try:
        results = scrape_and_update([fund_code])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error while scraping '{fund_code}': {exc}",
        ) from exc

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
