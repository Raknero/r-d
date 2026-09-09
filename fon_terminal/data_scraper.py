import json
import os
import re
import sys
import time
import random
from datetime import datetime, timedelta

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --- TEFAS internal backend API endpoints ---------------------------------
TEFAS_DATA_PAGE = "https://www.tefas.gov.tr/tr/fon-verileri"
GENERAL_INFO_URL = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetirDosya"
DISTRIBUTION_URL = "https://www.tefas.gov.tr/api/funds/dagilimSiraliGetirT"

DATABASE_FILE = "fund_database.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Whether the Playwright WAF handshake runs without a visible window.
#
# Watching the challenge get solved is the only practical way to debug a
# handshake failure, so the visible browser stays available -- but as a
# toggle rather than a source edit, because `headless=False` is easy to
# leave behind and the FastAPI server calls this from its background
# refresh too, where a window nobody is watching just gets closed and
# kills the page mid-poll.
#
# TEFAS_HANDSHAKE_HEADLESS=0 brings the window back (equivalently, pass
# headless=False to acquire_session_credentials).
HANDSHAKE_HEADLESS = os.environ.get("TEFAS_HANDSHAKE_HEADLESS", "1").strip().lower() not in (
    "0", "false", "no",
)

# --- Rate limiting -----------------------------------------------------------
#
# TEFAS rate-limits bursts of API calls with HTTP 429. Measured 2026-09-04:
# scraping 11 funds back-to-back (2 requests each) succeeded for the first 6
# and then returned 429 for EVERY remaining request. Because 429 used to be
# handled as a plain non-200 -- logged and given up on immediately -- those
# funds were reported as "No general info retrieved (invalid fund code...)",
# which both hid the real cause and left their `last_scraped_date` untouched.
# Scanning always in database insertion order then made it deterministic:
# the same trailing funds (i.e. the most recently ADDED ones) were rate
# limited on every single run and could stay stale for days.
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BASE_DELAY_SECONDS = 20
RATE_LIMIT_MAX_DELAY_SECONDS = 120

# Pause between two consecutive funds, used ONLY by the per-fund fallback
# path (see `scrape_fund_individually`). Deliberately well above the old
# 1.5-3.5s: that pace reliably tripped the 429 limiter partway through a
# multi-fund run. The bulk path doesn't need this at all, because it issues
# a request count that doesn't grow with the number of funds.
INTER_FUND_DELAY_RANGE_SECONDS = (5.0, 9.0)

# --- Bulk (all-funds) fetching ----------------------------------------------
#
# Both TEFAS endpoints are really "list funds, optionally filtered" queries:
# passing no fund filter returns EVERY fund in one response. Measured
# 2026-09-04 against the live site:
#
#   fonGnlBlgSiraliGetirDosya, fonKod=None, 1 day  -> 2041 funds, 0.45s, 444 KB
#   dagilimSiraliGetirT, aramaMetni=None, 1 day    -> 1907 funds, 1.01s, 1.4 MB
#
# So a refresh costs the SAME number of requests regardless of how many
# funds are tracked, instead of 2 per fund: 2 for a routine incremental
# window, plus a page or two more only when a wide backfill window pushes
# the distribution endpoint past one page. For 30 funds that is ~2 requests
# and a couple of seconds instead of 60 requests and ~200s of inter-fund
# pauses, plus the 429 storms a 60-request burst reliably provokes. It is
# also markedly gentler on TEFAS.
#
# The distribution endpoint paginates via basSira/bitSira -- a 1-indexed,
# inclusive row range (verified: rows 1-100 and 101-200 don't overlap and
# compose exactly into 1-200). The frontend's default of 100 rows truncates
# a bulk query badly, and a single large page isn't enough either: an
# all-funds pull is ~2000 rows per day, so a 30-day backfill needs ~47k
# rows and silently stopped at whatever ceiling we asked for. Requests are
# therefore paged until a short page proves the end was reached.
BULK_PAGE_SIZE = 20000

# Hard stop on paging, so a misbehaving response can't spin forever.
# 10 pages x 20000 rows covers ~100 days of every fund on TEFAS.
BULK_MAX_PAGES = 10

# --- Choosing between bulk and targeted requests -----------------------------
#
# Bulk isn't unconditionally cheaper. Its cost scales with the WIDTH OF THE
# DATE WINDOW (it downloads every fund for every day in range), while the
# targeted path's cost scales with the NUMBER OF FUNDS (2 requests each,
# plus a 5-9s pause between them). Measured 2026-09-09, 30-day window:
#
#   one fund, targeted   -> 0.63s   (2 requests, 23+23 rows)
#   all funds, bulk      -> 15.74s  (4 requests, 46816+46629 rows)
#
# So a single fund being backfilled -- exactly what /api/add-fund does -- is
# ~25x faster targeted, while the hourly refresh of a dozen funds over a
# 3-day window is ~40x faster in bulk (0.8s vs 22 requests and ~70s of
# pauses). The run picks whichever fits; see `should_use_bulk`.
BULK_MIN_FUNDS = 3
BULK_CHEAP_WINDOW_DAYS = 10

# Days of already-stored history re-fetched on every run. TEFAS revises
# published values after the fact, so the newest few days are pulled again
# rather than trusted permanently once seen.
BULK_WINDOW_OVERLAP_DAYS = 3

# Ceiling on the incremental window. A wider window multiplies the bulk
# response size (~1.4 MB per day of distribution data for all funds), so a
# fund that has been stale for months is caught up over consecutive runs
# instead of in one huge request.
MAX_BULK_WINDOW_DAYS = 60

# A bulk response is far bigger than a single fund's (measured 7.3 MB for a
# 5-day all-funds distribution pull, 4.4s), so the old 20s per-request
# timeout is too tight to double as the bulk ceiling.
BULK_REQUEST_TIMEOUT_SECONDS = 90

# Keys found in the distribution endpoint response that describe metadata
# rather than an actual asset allocation percentage. Everything else in a
# distribution record is treated as a raw asset name -> percentage pair and
# copied into "Varliklar" exactly as returned by the API.
DISTRIBUTION_METADATA_KEYS = {
    "fonkodu", "fonkod", "fonunvan", "fonunvantip", "tarih", "tarihstr",
    "fontip", "fontipi", "fontur", "fonturkod", "sfonturkod", "fongrup",
    "fongrubu", "kurucukod", "dil", "borsabultenfiyat", "bilfiyat",
    "id", "rownum", "sirano",
}

# `dagilimSiraliGetirT` returns portfolio distribution fields keyed by short
# TEFAS abbreviations rather than the full human-readable category names our
# database (and the frontend charts) expect. Matched case-insensitively.
TEFAS_DISTRIBUTION_MAP = {
    "hs": "Hisse Senedi",
    "fb": "Finansman Bonosu",
    "osks": "Özel Sektör Kira Sertifikaları",
    "tr": "Ters-Repo",
    "r": "Repo",
    "d": "Diğer",
    "vmtl": "Mevduat (TL)",
    "gykb": "Gayrimenkul Yatırım Fonları Katılma Payları",
    "yyf": "Yatırım Fonları Katılma Payları",
    "vint": "Vadeli İşlemler Nakit Teminatları",
    "bpp": "Borsa İstanbul Para Piyasası",
    "yhs": "Yabancı Hisse Senedi",
    "byf": "Borsa Yatırım Fonları Katılma Payları",
    "kibd": "Döviz Cinsi Kamu İç Borçlanma Araçları",
    "dt": "Devlet Tahvili",
    "khtl": "Katılma Hesabı (TL)",
    "kkstl": "Kamu Kira Sertifikaları (TL)",
    "ost": "Özel Sektör Tahvili",
    "vdm": "Varlığa Dayalı Menkul Kıymetler",
    "tpp": "Borsa İstanbul Para Piyasası",
}


# --- Generic helpers --------------------------------------------------------

def convert_to_float(value):
    """Converts an API value (number or string, TR or plain formatted) to float.

    Handles values that arrive as native JSON numbers as well as strings that
    may use Turkish thousands/decimal separators (e.g. "1.234,56") or a plain
    dot-decimal format (e.g. "1234.56"). Negative values are preserved.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text == "-":
        return 0.0

    is_negative = text.startswith("-")
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return 0.0

    if "," in cleaned:
        # Turkish formatted number: '.' is a thousands separator, ',' is decimal.
        cleaned = cleaned.replace(".", "").replace(",", ".")

    try:
        result = float(cleaned)
    except ValueError:
        return 0.0

    return -result if is_negative else result


def get_field(record, *candidate_keys):
    """Case-insensitively looks up the first matching key in a dict."""
    lowered = {str(k).lower(): v for k, v in record.items()}
    for candidate in candidate_keys:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def normalize_date(raw_value):
    """Converts a TEFAS API date value into our local DD.MM.YYYY format.

    Supports epoch-millisecond timestamps, ISO "YYYY-MM-DD[...]" strings,
    compact "YYYYMMDD" strings, and already-formatted "DD.MM.YYYY" strings.
    """
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        try:
            return datetime.utcfromtimestamp(raw_value / 1000).strftime("%d.%m.%Y")
        except (ValueError, OverflowError, OSError):
            return None

    text = str(raw_value).strip()
    if not text:
        return None

    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        return text

    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{day}.{month}.{year}"

    if re.match(r"^\d{8}$", text):
        return f"{text[6:8]}.{text[4:6]}.{text[0:4]}"

    if re.match(r"^\d{13}$", text):
        try:
            return datetime.utcfromtimestamp(int(text) / 1000).strftime("%d.%m.%Y")
        except (ValueError, OverflowError, OSError):
            return None

    return None


def extract_records(payload_json):
    """Pulls the list of record dicts out of a TEFAS API JSON response,
    regardless of whether it is wrapped in a "data" envelope or returned
    as a bare list.

    The "Dosya" (file export) style endpoints, such as
    `fonGnlBlgSiraliGetirDosya`, sometimes wrap the list under a different
    or unexpected key rather than one of the common envelope names, so as a
    last resort we scan every value in the response dict and return the
    first list of dicts we find.
    """
    if isinstance(payload_json, list):
        return payload_json

    if isinstance(payload_json, dict):
        for key in ("data", "Data", "DATA", "result", "Result", "results"):
            value = payload_json.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = extract_records(value)
                if nested:
                    return nested

        for value in payload_json.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value

    return []


def filter_by_fund_code(records, fund_code):
    """Keeps only records matching the target fund code, in case the API
    returns fuzzy/partial matches for the search text. Records without an
    identifiable fund code field are kept as-is.
    """
    filtered = []
    for record in records:
        if not isinstance(record, dict):
            continue
        code = get_field(record, "FONKODU", "FonKodu", "fonKodu", "fonkodu")
        if code is None or str(code).strip().upper() == fund_code.upper():
            filtered.append(record)
    return filtered


# --- Playwright handshake: solves the Next.js auth challenge ----------------

def acquire_session_credentials(
    basTarih_str,
    bitTarih_str,
    nav_timeout_ms=45000,
    recon_mode=False,
    poll_interval_ms=1000,
    max_poll_seconds=30,
    headless=None,
):
    """Launches a Chromium instance, loads the TEFAS fund data page
    with query parameters that force the Next.js frontend to immediately
    fetch data on mount, and inspects EVERY outgoing request for an
    Authorization header whose value starts with "Bearer" (rather than
    matching on a specific URL pattern, since Next.js may fire the
    authenticated call from a chunk that isn't literally "/api/funds/").
    The matching request's Authorization and Cookie headers are captured.

    The bare fund-data page loads an empty form and never triggers an API
    call on its own, so navigation uses the exact same basTarih/bitTarih
    values already generated for the payload, embedded in a URL that
    mirrors a real user search (fundType/search/startDate/endDate), which
    causes the page to fetch data as soon as it hydrates. Navigation only
    waits for "domcontentloaded" (not "networkidle", which can be blocked
    indefinitely by background analytics scripts keeping the network
    active) so the function can move on immediately to the polling loop
    below, which actively watches for the token while Next.js hydrates and
    fires its client-side request. The browser is closed as soon as the
    token is found (or once the poll budget is exhausted).

    `headless` defaults to HANDSHAKE_HEADLESS (see that constant for why
    running headless matters here); pass False explicitly to watch the
    challenge being solved while debugging.

    Returns a tuple (auth_header, cookies_header) or (None, None) on failure
    -- including when Playwright itself fails (browser closed mid-poll,
    crash, launch error), which is reported like any other failed handshake
    rather than raised. See the try/except around the browser block.
    """
    if headless is None:
        headless = HANDSHAKE_HEADLESS

    trigger_url = (
        f"https://www.tefas.gov.tr/tr/fon-verileri?fundType=YAT&search=TLY"
        f"&startDate={basTarih_str}&endDate={bitTarih_str}"
    )

    mode = "headless" if headless else "visible"
    print(f"[HANDSHAKE] Launching {mode} browser to solve Next.js auth challenge...")

    # Plain dict, mutated in place by the request listener closure so the
    # polling loop below (running in the same sync context) can observe it.
    captured = {"authorization": None, "cookie": None, "source_url": None}

    def handle_request(request):
        # --- RECON: log every call to the funds API so we can see the real,
        # current endpoint paths and payload schema the frontend uses. This
        # is purely observational and never blocks token/cookie capture.
        if recon_mode and "/api/funds/" in request.url:
            print(f"\n[RECON] >>> {request.method} {request.url}")
            post_data = request.post_data
            if post_data:
                print(f"[RECON] Payload: {post_data}")
            else:
                print("[RECON] Payload: <empty/none>")

        if captured["authorization"]:
            return
        headers = request.headers
        auth_header = headers.get("authorization") or headers.get("Authorization")
        if auth_header and auth_header.strip().lower().startswith("bearer"):
            captured["authorization"] = auth_header
            captured["cookie"] = headers.get("cookie")
            captured["source_url"] = request.url
            print(f"[HANDSHAKE] Captured Bearer token from request to: {request.url}")

    with sync_playwright() as playwright:
        # --disable-blink-features=AutomationControlled strips the basic
        # `navigator.webdriver` flag that WAFs/bot-detection scripts check
        # for, since Playwright/Chromium sets it by default.
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # A hardcoded, standard Windows Chrome User-Agent (instead of
            # Playwright's default Headless/Chromium UA string) plus
            # realistic Accept-Language headers, to better blend in with a
            # genuine browser session and avoid fingerprinting.
            context = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={
                    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = context.new_page()
            page.on("request", handle_request)

            print(f"[HANDSHAKE] Navigating to {trigger_url} (waiting for domcontentloaded)...")
            try:
                page.goto(trigger_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            except PlaywrightTimeoutError:
                print("[HANDSHAKE] domcontentloaded wait timed out; continuing to poll anyway.")

            # Next.js hydrates on the client and fires its data-fetching
            # request shortly after domcontentloaded, so keep polling here
            # instead of closing the browser or blocking on networkidle.
            #
            # In recon_mode we deliberately keep the browser open for the
            # FULL poll budget even after the token is captured, since the
            # TLY charts/tables fire additional "/api/funds/" calls as they
            # progressively load, and we want to log all of them.
            max_polls = max(1, int((max_poll_seconds * 1000) / poll_interval_ms))
            for attempt in range(max_polls):
                if captured["authorization"] and not recon_mode:
                    break
                page.wait_for_timeout(poll_interval_ms)
                print(f"[HANDSHAKE] Waiting/observing... ({(attempt + 1) * poll_interval_ms}ms elapsed)")

            if not captured["cookie"]:
                # Fall back to reading cookies directly from the browser context
                # in case the intercepted request didn't carry a Cookie header
                # (e.g. cookies attached by the browser at the network layer).
                context_cookies = context.cookies()
                if context_cookies:
                    captured["cookie"] = "; ".join(
                        f"{c['name']}={c['value']}" for c in context_cookies
                    )

        except Exception as exc:  # noqa: BLE001
            # A handshake is allowed to FAIL, but it must never explode: the
            # browser/page can disappear mid-poll (window closed by hand when
            # running non-headless, tab crash, WAF killing the session), and
            # `page.wait_for_timeout` then raises "Target page, context or
            # browser has been closed". That exception used to propagate all
            # the way out of `scrape_and_update` -- past its per-fund
            # try/except, since the handshake happens BEFORE the fund loop --
            # so one closed window meant zero funds refreshed. Degrading to
            # the normal "(None, None)" failure path keeps a flaky handshake
            # a handshake problem instead of a whole-run outage.
            print(f"[ERROR] [HANDSHAKE] Browser handshake failed: {exc}")

        finally:
            try:
                browser.close()
                print(f"[HANDSHAKE] Browser closed ({mode}).")
            except Exception as exc:  # noqa: BLE001 - already-dead browser
                print(f"[HANDSHAKE] [INFO] Browser was already closed: {exc}")

    if not captured["authorization"]:
        print("[ERROR] [HANDSHAKE] Failed to capture a Bearer Authorization token from any request.")
        return None, None

    return captured["authorization"], captured["cookie"]


# --- Token cache: avoids a Playwright handshake on every scrape run ---------
#
# `_cached_headers` holds the captured `Authorization: Bearer ...` value and
# `_cached_cookies` holds the raw Cookie header string; together they
# represent one authenticated TEFAS session. As long as TEFAS keeps
# accepting them, `scrape_and_update()` can be called repeatedly (e.g. once
# per `/api/add-fund` request, or once per fund in the lifespan startup
# scan) without paying the cost of a fresh Playwright browser launch every
# single time. The cache lives for the lifetime of the Python process.
_cached_headers = None
_cached_cookies = None


def get_session_credentials(bas_tarih, bit_tarih, force_refresh=False):
    """Returns (auth_header, cookie_header), reusing the in-memory cache
    whenever a valid one is available so Playwright is only launched when
    there's truly no usable session yet (first run, or after the cache was
    invalidated by a 401/403 -- see `invalidate_cached_credentials`).

    `force_refresh=True` skips the cache unconditionally, e.g. right after
    invalidating it to obtain a brand-new token.
    """
    global _cached_headers, _cached_cookies

    if not force_refresh and _cached_headers and _cached_cookies:
        print("[CACHE] Reusing cached TEFAS session token (skipping Playwright handshake).")
        return _cached_headers, _cached_cookies

    auth_header, cookie_header = acquire_session_credentials(bas_tarih, bit_tarih, recon_mode=False)
    if auth_header:
        _cached_headers = auth_header
        _cached_cookies = cookie_header

    return auth_header, cookie_header


def invalidate_cached_credentials():
    """Clears the in-memory token/cookie cache, forcing the next
    `get_session_credentials()` call to perform a fresh Playwright
    handshake. Called when TEFAS responds with 401/403, signalling that
    the cached token has expired or been rejected.
    """
    global _cached_headers, _cached_cookies
    _cached_headers = None
    _cached_cookies = None
    print("[CACHE] Cached TEFAS session token invalidated.")


def build_authenticated_session(auth_header, cookie_header):
    """Builds a requests.Session pre-loaded with the Bearer token AND the
    full cookie string captured from the Playwright browser context. TEFAS's
    Next.js backend/WAF appears to validate the session cookies together
    with the bearer token, so both must be present and consistent on every
    request or the API rejects the call.
    """
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Origin": "https://www.tefas.gov.tr",
        "Referer": TEFAS_DATA_PAGE,
        "X-Requested-With": "XMLHttpRequest",
        "Authorization": auth_header,
    })

    if cookie_header:
        # Parse the raw "name=value; name2=value2" cookie string captured
        # from Playwright into the session's cookie jar (rather than just
        # setting a raw "Cookie" header) so requests manages them properly
        # and sends them consistently alongside the Authorization header.
        cookie_count = 0
        for chunk in cookie_header.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            session.cookies.set(name.strip(), value.strip(), domain="www.tefas.gov.tr")
            cookie_count += 1
        print(f"[HANDSHAKE] Injected {cookie_count} cookie(s) into the requests session.")
    else:
        print("[WARNING] [HANDSHAKE] No cookies were captured; API calls may be rejected without them.")

    return session


# --- TEFAS API access --------------------------------------------------------

def build_plain_session():
    """An unauthenticated session for the bulk endpoints.

    Measured 2026-09-04: `fonGnlBlgSiraliGetirDosya` and
    `dagilimSiraliGetirT` both return full data over plain HTTPS with no
    Bearer token, no WAF cookies, and no User-Agent (5/5 attempts, HTTP 200).
    The Playwright handshake that the rest of this module is built around is
    therefore not needed for the data path, and skipping it saves the ~4-6s
    browser launch on every run while removing the biggest bot-detection
    surface we have.

    This is an observation about TEFAS's current behavior, not a guarantee:
    a browser-like User-Agent is still sent, and if TEFAS starts enforcing
    credentials again the 401/403 branch in `post_tefas_endpoint`
    transparently performs the handshake and retries. The handshake code
    stays as the fallback rather than the default.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": TEFAS_DATA_PAGE,
    })
    return session


def group_records_by_fund(records):
    """Groups a bulk response into {FUND_CODE: [records]} in one pass.

    The per-fund path calls `filter_by_fund_code` once per fund, which would
    mean re-walking a ~10k-row bulk response for every tracked fund.
    """
    grouped = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        code = get_field(record, "FONKODU", "FonKodu", "fonKodu", "fonkodu")
        if code is None:
            continue
        grouped.setdefault(str(code).strip().upper(), []).append(record)
    return grouped


def latest_stored_date(entry):
    """Newest `Tarih` (as a date) already stored for a fund, or None."""
    records = (entry or {}).get("records") or []
    newest = None
    for record in records:
        raw = record.get("Tarih")
        if not raw:
            continue
        try:
            day, month, year = str(raw).split(".")
            parsed = datetime(int(year), int(month), int(day)).date()
        except (ValueError, TypeError):
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def compute_scrape_window(database, fund_list, days_back):
    """Chooses the date range to request, as (bas_tarih, bit_tarih, reason).

    The old behavior was to always ask for the last `days_back` (30) days
    for every fund on every run, which re-downloaded a month of history to
    learn about one new day. The window is now derived from what's already
    stored:

    - Any tracked fund with no records at all needs its history built, so
      the full `days_back` window is used (a brand-new fund added from the
      UI gets its 30 days in the same request everyone else is served by).
    - Otherwise the window starts at the OLDEST "newest stored date" across
      the requested funds, minus BULK_WINDOW_OVERLAP_DAYS so recently
      published values are re-checked for revisions.

    Capped at MAX_BULK_WINDOW_DAYS: response size grows with the window, so
    a badly stale fund catches up over consecutive runs rather than in one
    enormous request.
    """
    today = datetime.now().date()
    bit_tarih = today.strftime("%Y%m%d")

    newest_dates = []
    for raw_code in fund_list:
        code = raw_code.strip().upper()
        newest = latest_stored_date(database.get(code))
        if newest is None:
            return (
                (today - timedelta(days=days_back)).strftime("%Y%m%d"),
                bit_tarih,
                f"{code} icin gecmis yok; {days_back} gunluk dolum penceresi",
            )
        newest_dates.append(newest)

    if not newest_dates:
        return (
            (today - timedelta(days=days_back)).strftime("%Y%m%d"),
            bit_tarih,
            f"{days_back} gunluk varsayilan pencere",
        )

    stale_days = (today - min(newest_dates)).days
    window_days = min(max(stale_days + BULK_WINDOW_OVERLAP_DAYS, 1), MAX_BULK_WINDOW_DAYS)
    reason = (
        f"artimli pencere: en bayat fon {stale_days} gun geride, "
        f"+{BULK_WINDOW_OVERLAP_DAYS} gun revizyon payi"
    )
    if window_days == MAX_BULK_WINDOW_DAYS and stale_days + BULK_WINDOW_OVERLAP_DAYS > MAX_BULK_WINDOW_DAYS:
        reason = f"{MAX_BULK_WINDOW_DAYS} gunluk tavana kirpildi (fon {stale_days} gun geride)"

    return (today - timedelta(days=window_days)).strftime("%Y%m%d"), bit_tarih, reason


def should_use_bulk(fund_count, window_days):
    """Whether to serve this run from one all-funds response.

    Bulk wins as soon as a few funds are involved, because its request count
    is flat in fund count. It loses only in the one case where the window is
    wide AND barely any funds need it: pulling ~47k rows of every fund's
    month to update one fund is far more work than asking for that fund's
    month directly.
    """
    if fund_count >= BULK_MIN_FUNDS:
        return True
    return window_days <= BULK_CHEAP_WINDOW_DAYS


def window_length_days(bas_tarih, bit_tarih):
    """Days spanned by a YYYYMMDD window, used to size up its cost."""
    try:
        start = datetime.strptime(bas_tarih, "%Y%m%d").date()
        end = datetime.strptime(bit_tarih, "%Y%m%d").date()
    except (ValueError, TypeError):
        return 0
    return max((end - start).days, 0)


def fetch_bulk_records(session, url, payload, bas_tarih, bit_tarih, label):
    """One bulk request, with timing logging so a slow response is visible
    in the logs rather than inferred."""
    started = time.perf_counter()
    records, session = post_tefas_endpoint(
        session, url, payload, f"BULK/{label}", bas_tarih, bit_tarih
    )
    elapsed = time.perf_counter() - started
    print(f"[BULK] {label}: {len(records)} kayit, {elapsed:.2f}s")
    return records, session


def fetch_bulk_distribution(session, bas_tarih, bit_tarih):
    """All-funds distribution data for the window, paging until complete.

    A single fixed page silently truncates: a 30-day all-funds query needs
    ~47k rows, so asking for 20000 returned exactly 20000 and the missing
    rows looked like funds that simply publish no distribution breakdown --
    indistinguishable, in the logs, from a genuinely qualified/closed fund.
    Paging until a short page arrives is what makes "no distribution data"
    trustworthy.
    """
    all_records = []
    first_row = 1

    for page_index in range(BULK_MAX_PAGES):
        payload = build_distribution_payload(
            None, bas_tarih, bit_tarih, first_row=first_row, page_size=BULK_PAGE_SIZE
        )
        label = "dagilim" if page_index == 0 else f"dagilim sayfa {page_index + 1}"
        records, session = fetch_bulk_records(
            session, DISTRIBUTION_URL, payload, bas_tarih, bit_tarih, label
        )
        all_records.extend(records)

        # A short page means the last row was reached.
        if len(records) < BULK_PAGE_SIZE:
            return all_records, session

        first_row += BULK_PAGE_SIZE
    else:
        print(
            f"[WARNING] [BULK] Dagilim verisi {BULK_MAX_PAGES} sayfada bitmedi "
            f"({len(all_records)} kayit alindi); veri eksik olabilir. "
            "Tarih penceresi daraltilmali."
        )

    return all_records, session


def build_general_info_payload(fund_code, bas_tarih, bit_tarih):
    """Payload schema for `fonGnlBlgSiraliGetirDosya`, the unpaginated
    "file export" variant of the general info endpoint (bypasses the
    frontend's basSira/bitSira pagination), captured via Playwright recon.
    """
    return {
        "dil": "TR",
        "fonTipi": "YAT",
        "fonKod": fund_code,
        "fonGrup": None,
        "basTarih": bas_tarih,
        "bitTarih": bit_tarih,
        "fonTurKod": None,
        "fonUnvanTip": None,
        "kurucuKod": None,
        "fonTurAciklama": None,
        "sfonTurKod": None,
    }


def build_distribution_payload(fund_code, bas_tarih, bit_tarih, first_row=1, page_size=100):
    """Payload schema for `dagilimSiraliGetirT`, the portfolio distribution
    endpoint, captured via Playwright recon after manually triggering the
    "Portföy Dağılımı" / "Varlık Dağılımı" tab.

    `fund_code=None` drops the fund filter so every fund is returned.
    `first_row`/`page_size` map onto the endpoint's 1-indexed inclusive
    basSira/bitSira row range; see `fetch_bulk_distribution` for why bulk
    queries must page rather than ask for one huge range.
    """
    return {
        "fonTipi": "YAT",
        "fonKodu": None,
        "aramaMetni": fund_code,
        "fonTurKod": None,
        "fonGrubu": None,
        "sfonTurKod": None,
        "basTarih": bas_tarih,
        "bitTarih": bit_tarih,
        "basSira": first_row,
        "bitSira": first_row + page_size - 1,
        "fonTurAciklama": None,
        "dil": "TR",
        "kurucuKod": None,
        "sFonTurKod": "",
        "fonKod": None,
        "fonGrup": "",
        "fonUnvanTip": "",
    }


def rate_limit_delay_seconds(response, retry_index):
    """Seconds to wait before retrying a rate-limited (HTTP 429) request.

    Prefers TEFAS's own `Retry-After` header when it sends one (seconds
    form), otherwise falls back to exponential backoff
    (20s -> 40s -> 80s), capped at RATE_LIMIT_MAX_DELAY_SECONDS. A little
    jitter is added so several funds retrying in the same run don't line up
    into a synchronized burst that trips the limiter all over again.
    """
    retry_after = (response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_MAX_DELAY_SECONDS)
        except ValueError:
            pass

    delay = RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** retry_index)
    return min(delay, RATE_LIMIT_MAX_DELAY_SECONDS) + random.uniform(0, 3)


def post_tefas_endpoint(session, url, payload, label, bas_tarih, bit_tarih):
    """Issues a POST to a TEFAS API endpoint and returns (records, session)
    with every record the response contained, unfiltered.

    `label` only tags log lines -- a fund code on the per-fund path, or
    something like "BULK" when one request covers all funds.

    Two distinct failures are retried, each with its own budget:

    - **HTTP 401/403** -- the session was rejected. The in-memory token cache
      is invalidated, a fresh Playwright handshake obtains a new
      token/cookie pair, a new authenticated `requests.Session` is built
      from it, and the request is retried once. The (possibly refreshed)
      session is always returned so the caller can keep reusing it. This is
      also what upgrades an unauthenticated session (see
      `build_plain_session`) to a full WAF handshake if TEFAS ever starts
      demanding credentials on these endpoints again.
    - **HTTP 429** -- TEFAS is rate limiting us (see RATE_LIMIT_MAX_RETRIES).
      This is a TRANSIENT condition, so the request waits and retries
      instead of giving up: treating it as permanent is what previously
      caused whole funds to be silently dropped from a multi-fund run and
      mislabelled as "invalid fund code".
    """
    auth_retries = 0
    rate_limit_retries = 0

    while True:
        try:
            response = session.post(url, json=payload, timeout=BULK_REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            print(f"[ERROR] [{label}] Network error calling {url}: {exc}")
            return [], session

        if response.status_code in (401, 403):
            if auth_retries >= 1:
                print(
                    f"[ERROR] [{label}] {url} still returned HTTP {response.status_code} "
                    "after refreshing the session token; giving up for this request."
                )
                return [], session

            auth_retries += 1
            print(
                f"[WARNING] [{label}] {url} returned HTTP {response.status_code} "
                "(session rejected). Re-authenticating via Playwright and retrying..."
            )
            invalidate_cached_credentials()
            auth_header, cookie_header = get_session_credentials(bas_tarih, bit_tarih, force_refresh=True)
            if not auth_header:
                print(f"[ERROR] [{label}] Failed to refresh TEFAS session; aborting retry.")
                return [], session

            session = build_authenticated_session(auth_header, cookie_header)
            continue

        if response.status_code == 429:
            if rate_limit_retries >= RATE_LIMIT_MAX_RETRIES:
                print(
                    f"[ERROR] [{label}] {url} hala HTTP 429 donuyor "
                    f"({RATE_LIMIT_MAX_RETRIES} yeniden denemeden sonra); bu istek birakildi. "
                    "TEFAS istek limiti asildi -- fon kodu gecersiz DEGIL."
                )
                return [], session

            delay = rate_limit_delay_seconds(response, rate_limit_retries)
            rate_limit_retries += 1
            print(
                f"[WARNING] [{label}] {url} HTTP 429 (TEFAS istek limiti) dondurdu; "
                f"{delay:.1f}s beklenip tekrar denenecek "
                f"({rate_limit_retries}/{RATE_LIMIT_MAX_RETRIES})."
            )
            time.sleep(delay)
            continue

        if response.status_code != 200:
            print(f"[ERROR] [{label}] {url} returned HTTP {response.status_code}")
            return [], session

        try:
            payload_json = response.json()
        except ValueError:
            print(f"[ERROR] [{label}] Invalid JSON response from {url}")
            return [], session

        records = extract_records(payload_json)
        if not records:
            print(f"[WARNING] [{label}] No records found in response from {url}")

        return records, session


def fetch_endpoint_data(session, url, fund_code, payload, bas_tarih, bit_tarih):
    """`post_tefas_endpoint` narrowed to a single fund's records.

    Used by the per-fund fallback path; the bulk path keeps the full
    response and groups it locally instead.
    """
    records, session = post_tefas_endpoint(
        session, url, payload, fund_code, bas_tarih, bit_tarih
    )
    return filter_by_fund_code(records, fund_code), session


# --- Response parsing / merging ---------------------------------------------

def build_general_info_map(records):
    """Maps general info records into our local schema, keyed by Tarih.

    NOTE: "Pazar Payı" (market share) is intentionally excluded here. TEFAS
    no longer returns real data for this field (it always comes back as 0),
    so it is dropped entirely from the pipeline rather than being saved to
    the database as dead/empty data.
    """
    info_map = {}
    for record in records:
        raw_date = get_field(record, "TARIH", "Tarih", "tarih", "Date")
        date_str = normalize_date(raw_date)
        if not date_str:
            continue

        info_map[date_str] = {
            "Tarih": date_str,
            "Fiyat": convert_to_float(
                get_field(record, "FIYAT", "Fiyat", "fiyat", "SonFiyat", "sonFiyat")
            ),
            "Pay": int(convert_to_float(
                get_field(record, "TEDPAYSAYISI", "tedPaySayisi", "PaySayisi", "paySayisi", "PAY")
            )),
            "ToplamDeger": convert_to_float(
                get_field(record, "PORTFOYBUYUKLUK", "portfoyBuyukluk", "FonToplamDeger", "fonToplamDeger")
            ),
            "Yatirimci": int(convert_to_float(
                get_field(record, "KISISAYISI", "kisiSayisi", "YatirimciSayisi", "yatirimciSayisi")
            )),
        }
    return info_map


def build_distribution_map(records):
    """Maps portfolio distribution records into {Tarih: {asset_name: pct}}.

    `dagilimSiraliGetirT` returns raw short abbreviations (e.g. "hs", "fb")
    for each asset category instead of the full names our database and the
    frontend charts expect, so each key is translated via
    TEFAS_DISTRIBUTION_MAP (case-insensitively) before being stored. Any key
    that doesn't have a known mapping is kept as-is (raw), rather than
    dropped, so unexpected/new categories aren't silently lost.
    """
    dist_map = {}
    for record in records:
        raw_date = get_field(record, "TARIH", "Tarih", "tarih", "Date")
        date_str = normalize_date(raw_date)
        if not date_str:
            continue

        varliklar = {}
        for key, raw_value in record.items():
            if str(key).lower() in DISTRIBUTION_METADATA_KEYS:
                continue
            if isinstance(raw_value, (int, float)):
                value = convert_to_float(raw_value)
            elif isinstance(raw_value, str) and re.search(r"\d", raw_value):
                value = convert_to_float(raw_value)
            else:
                continue

            asset_name = TEFAS_DISTRIBUTION_MAP.get(str(key).strip().lower(), key)
            varliklar[asset_name] = value

        dist_map[date_str] = varliklar
    return dist_map


def merge_fund_data(general_map, distribution_map):
    """Merges general info and portfolio distribution data on matching dates."""
    merged = []
    for date_str, info in general_map.items():
        record = dict(info)
        record["Varliklar"] = distribution_map.get(date_str, {})
        merged.append(record)
    return merged


def general_map_has_changes(entry, general_map):
    """Whether a freshly fetched general-info map differs from what's stored.

    Lets a refresh that found nothing new skip the expensive distribution
    request and the database write entirely. TEFAS publishes once a day, so
    with an hourly refresh most runs have nothing to do, and rewriting a
    large JSON file (plus logging a line per fund per date) for no reason is
    pure waste.

    Both new dates and REVISED values for an already-stored date count as
    changes, as does a stored record that never got a distribution key at
    all -- an empty `Varliklar` is legitimate for qualified/closed funds, a
    missing one means we simply never fetched it.
    """
    stored = {
        record.get("Tarih"): record
        for record in (entry or {}).get("records") or []
    }

    for date_str, info in general_map.items():
        existing = stored.get(date_str)
        if existing is None or "Varliklar" not in existing:
            return True
        for key in ("Pay", "Yatirimci"):
            if existing.get(key) != info.get(key):
                return True
        for key in ("Fiyat", "ToplamDeger"):
            old_value = existing.get(key)
            new_value = info.get(key)
            if old_value is None or new_value is None:
                if old_value is not new_value:
                    return True
            elif abs(float(old_value) - float(new_value)) > 1e-6:
                return True

    return False


# --- Local database (fund_database.json) persistence ------------------------
#
# Each fund entry is stored as {"_metadata": {...}, "records": [...]} so
# funds can be hidden from the UI ("show_on_ui": false) while optionally
# still being refreshed occasionally in the background ("background_tracking":
# true) without losing their historical "records", tracked via
# "last_scraped_date" (YYYY-MM-DD). Older database files predate this
# metadata wrapper and store a bare list of records per fund directly; that
# legacy shape is transparently upgraded on every load (see
# `ensure_fund_entry_shape`) so no separate one-off migration is needed.

def default_metadata():
    """Fresh metadata for a fund that has never been given explicit
    show/track settings: visible on the UI, not background-tracked, and
    never (yet) scraped.
    """
    return {"show_on_ui": True, "background_tracking": False, "last_scraped_date": None}


def ensure_fund_entry_shape(entry):
    """Normalizes a single fund's database entry into the current
    {"_metadata": {...}, "records": [...]} shape, upgrading the legacy
    format (a bare list of daily records with no metadata) on the fly.
    """
    if isinstance(entry, list):
        return {"_metadata": default_metadata(), "records": entry}

    if isinstance(entry, dict):
        metadata = default_metadata()
        metadata.update(entry.get("_metadata") or {})
        records = entry.get("records")
        return {"_metadata": metadata, "records": records if isinstance(records, list) else []}

    return {"_metadata": default_metadata(), "records": []}


def load_database():
    """Loads the master fund database, or returns an empty dict if missing.

    Every fund entry is passed through `ensure_fund_entry_shape` so legacy
    (pre-metadata) entries are upgraded transparently as soon as they're
    read, without requiring a separate migration pass.
    """
    if not os.path.exists(DATABASE_FILE):
        return {}
    with open(DATABASE_FILE, "r", encoding="utf-8") as file:
        raw_database = json.load(file)
    return {
        fund_code: ensure_fund_entry_shape(entry)
        for fund_code, entry in raw_database.items()
    }


def save_database(database):
    """Persists the master fund database to disk."""
    with open(DATABASE_FILE, "w", encoding="utf-8") as file:
        json.dump(database, file, ensure_ascii=False, indent=4)


def sort_fund_records(records):
    """Sorts fund records chronologically by Tarih (DD.MM.YYYY)."""
    def parse_date(entry):
        day, month, year = entry["Tarih"].split(".")
        return datetime(int(year), int(month), int(day))
    return sorted(records, key=parse_date)


def upsert_fund_record(database, fund_code, new_record):
    """Inserts a new daily record or overwrites an existing one for the same
    date, then keeps the fund's record list chronologically sorted.
    Returns "inserted" or "updated" for logging purposes.
    """
    if fund_code not in database:
        database[fund_code] = ensure_fund_entry_shape(None)

    entry = database[fund_code]
    records = entry["records"]
    existing_index = next(
        (i for i, record in enumerate(records) if record.get("Tarih") == new_record["Tarih"]),
        -1,
    )

    if existing_index > -1:
        records[existing_index] = new_record
        action = "updated"
    else:
        records.append(new_record)
        action = "inserted"

    entry["records"] = sort_fund_records(records)
    database[fund_code] = entry
    return action


# --- Reusable pipeline entry point -------------------------------------------

def store_fund_records(database, fund_code, merged_records, quiet=False):
    """Upserts a fund's merged records and stamps `last_scraped_date`.

    Returns (inserted_count, updated_count).
    """
    inserted_count = 0
    updated_count = 0

    for record in merged_records:
        if not record.get("Varliklar"):
            # Qualified/closed funds legitimately publish no distribution
            # breakdown; flagged per date, not treated as an error.
            print(
                f"[INFO] [{fund_code}] {record['Tarih']} için varlık dağılım verisi "
                "boş/bulunamadı (Nitelikli/Kapalı fon olabilir)."
            )

        action = upsert_fund_record(database, fund_code, record)
        if not quiet:
            print(f"  [{fund_code}] {record['Tarih']} -> {action}")
        if action == "inserted":
            inserted_count += 1
        else:
            updated_count += 1

    # Stamped regardless of whether the fund is shown on the UI or only
    # tracked in the background, so the 15-day background-tracking cadence
    # (see main.py's scan target selection) is measured from the most recent
    # successful run.
    database[fund_code]["_metadata"]["last_scraped_date"] = datetime.now().strftime("%Y-%m-%d")
    return inserted_count, updated_count


def scrape_fund_individually(session, database, fund_code, bas_tarih, bit_tarih, reason):
    """Two requests scoped to one fund code.

    Reached two ways: deliberately, when `should_use_bulk` decides a wide
    window for very few funds is cheaper served directly, and as a fallback
    when a fund is missing from an otherwise successful bulk response (e.g.
    a type the `fonTipi: "YAT"` filter excludes). `reason` says which, so
    the logs don't imply a failure when none occurred.

    Returns (result_dict, session).
    """
    print(f"[TEK FON] [{fund_code}] {reason}")

    general_payload = build_general_info_payload(fund_code, bas_tarih, bit_tarih)
    distribution_payload = build_distribution_payload(fund_code, bas_tarih, bit_tarih)

    general_records, session = fetch_endpoint_data(
        session, GENERAL_INFO_URL, fund_code, general_payload, bas_tarih, bit_tarih
    )
    distribution_records, session = fetch_endpoint_data(
        session, DISTRIBUTION_URL, fund_code, distribution_payload, bas_tarih, bit_tarih
    )

    if not general_records:
        # Deliberately does NOT claim the fund code is invalid: rate
        # limiting is the most common cause in practice and is logged in
        # detail just above. Blaming the fund code here sent past debugging
        # in entirely the wrong direction.
        message = (
            "No general info retrieved -- see the errors logged above "
            "(TEFAS rate limit, rejected session, invalid fund code, or "
            "genuinely no data for this period)."
        )
        print(f"[WARNING] [{fund_code}] {message} Skipping fund.")
        return {"status": "error", "message": message}, session

    merged_records = merge_fund_data(
        build_general_info_map(general_records),
        build_distribution_map(distribution_records),
    )
    if not merged_records:
        message = "Merge produced no usable records."
        print(f"[WARNING] [{fund_code}] {message} Skipping fund.")
        return {"status": "error", "message": message}, session

    inserted, updated = store_fund_records(database, fund_code, merged_records)
    print(f"[SUCCESS] [{fund_code}] {inserted} inserted, {updated} updated.")
    return {"status": "success", "inserted": inserted, "updated": updated}, session


def scrape_funds_individually(session, database, fund_codes, bas_tarih, bit_tarih, reason):
    """Runs the targeted path for several funds, pacing between them.

    This is the only path whose request count grows with the number of
    funds, so it's also the only one that still needs the inter-fund pause
    that used to guard the whole pipeline against rate limiting.
    """
    results = {}
    for index, fund_code in enumerate(fund_codes):
        try:
            result, session = scrape_fund_individually(
                session, database, fund_code, bas_tarih, bit_tarih, reason
            )
            results[fund_code] = result
        except Exception as exc:
            print(f"[CRITICAL] [{fund_code}] Unhandled exception: {exc}")
            results[fund_code] = {"status": "error", "message": str(exc)}

        if index < len(fund_codes) - 1:
            time.sleep(random.uniform(*INTER_FUND_DELAY_RANGE_SECONDS))

    return results, session


def _finish_scrape_run(database, results, run_started):
    """Persists the run's changes (if any) and prints its summary.

    One write per run instead of one per fund: the per-fund work is now pure
    in-memory merging, so re-serializing the whole database after each fund
    was measurable overhead for no added safety. A run that changed nothing
    doesn't rewrite the file at all.
    """
    if any(
        result.get("status") == "success" and (result.get("inserted") or result.get("updated"))
        for result in results.values()
    ):
        save_database(database)
        print(f"[SYSTEM] {DATABASE_FILE} kaydedildi.")

    elapsed = time.perf_counter() - run_started
    succeeded = sum(1 for result in results.values() if result.get("status") == "success")
    print(f"[SYSTEM] Tarama bitti: {succeeded}/{len(results)} fon basarili, {elapsed:.2f}s.")


def scrape_and_update(fund_list, days_back=30):
    """Refreshes the given fund codes in `fund_database.json`.

    Cost is now independent of how many funds are tracked. Both TEFAS
    endpoints return every fund when given no fund filter, so one run issues
    the same ~2 requests for 3 funds as for 300, and the response is grouped
    by fund code locally. The previous design sent 2 requests per fund with
    a 5-9s pause between funds, which for 30 funds meant 60 requests and
    ~200s of deliberate waiting, on top of the 429 throttling that a burst
    that size reliably provoked.

    Three further savings compound with that:

    - The date window is derived from what's already stored
      (`compute_scrape_window`) instead of always re-requesting 30 days.
    - Requests start on an unauthenticated session (`build_plain_session`),
      since these endpoints currently need no token; the Playwright
      handshake happens only if TEFAS answers 401/403.
    - A run that finds nothing new skips the distribution request and the
      database write entirely (`general_map_has_changes`), which is what
      most hourly refreshes do given TEFAS publishes once a day.

    Bulk is not used unconditionally: because its cost scales with the date
    window rather than the fund count, a wide backfill window for one or two
    funds is served far faster by targeted requests (see `should_use_bulk`).
    A fund missing from an otherwise successful bulk response falls back to
    the same targeted path, so bulk stays an optimization rather than a new
    dependency.

    Returns a dict keyed by (uppercased) fund code, e.g.:
        {
            "TLY": {"status": "success", "inserted": 2, "updated": 28},
            "XYZ": {"status": "error", "message": "No general info retrieved..."},
        }
    """
    fund_codes = [code.strip().upper() for code in fund_list if code and code.strip()]
    if not fund_codes:
        return {}

    print(f"[SYSTEM] Starting scrape run for: {', '.join(fund_codes)}")

    database = load_database()
    bas_tarih, bit_tarih, window_reason = compute_scrape_window(database, fund_codes, days_back)
    print(f"[SYSTEM] Tarih penceresi {bas_tarih} -> {bit_tarih} ({window_reason})")

    session = build_plain_session()
    results = {}
    run_started = time.perf_counter()

    window_days = window_length_days(bas_tarih, bit_tarih)
    if not should_use_bulk(len(fund_codes), window_days):
        reason = (
            f"{len(fund_codes)} fon icin {window_days} gunluk pencere: "
            "hedefli sorgu toplu cekimden ucuz"
        )
        results, session = scrape_funds_individually(
            session, database, fund_codes, bas_tarih, bit_tarih, reason
        )
        _finish_scrape_run(database, results, run_started)
        return results

    # --- One request: general info for every fund ----------------------------
    general_payload = build_general_info_payload(None, bas_tarih, bit_tarih)
    general_records, session = fetch_bulk_records(
        session, GENERAL_INFO_URL, general_payload, bas_tarih, bit_tarih, "genel bilgi"
    )
    general_by_fund = group_records_by_fund(general_records)

    if not general_by_fund:
        # The bulk request itself failed (rate limited, network error, WAF).
        # Deliberately does NOT fall back to per-fund requests here: that
        # would fire 2 requests per fund -- exactly the 60-request burst
        # bulk fetching exists to avoid -- against an endpoint that just
        # refused us. The next scheduled run retries with 2 requests, and
        # every fund keeps its old `last_scraped_date`, so stalest-first
        # ordering still puts them at the front.
        message = (
            "Toplu istek veri dondurmedi (istek limiti, ag hatasi veya WAF). "
            "Tek fon sorgularina DUSULMEDI; sonraki tur yeniden denenecek."
        )
        print(f"[ERROR] [BULK] {message}")
        return {code: {"status": "error", "message": message} for code in fund_codes}

    print(f"[BULK] Yanitta {len(general_by_fund)} fon var; {len(fund_codes)} tanesi takip ediliyor.")

    # A fund absent from a SUCCESSFUL bulk response is a different case: the
    # all-funds query worked, this fund just wasn't in it (e.g. a type the
    # `fonTipi: "YAT"` filter excludes), so a targeted query is worth it.
    served_by_bulk = [code for code in fund_codes if general_by_fund.get(code)]
    missing_from_bulk = [code for code in fund_codes if not general_by_fund.get(code)]

    general_maps = {
        code: build_general_info_map(general_by_fund[code]) for code in served_by_bulk
    }
    changed = [
        code for code in served_by_bulk
        if general_map_has_changes(database.get(code), general_maps[code])
    ]

    # --- Distribution (paged), only if something actually changed ------------
    if changed:
        distribution_records, session = fetch_bulk_distribution(session, bas_tarih, bit_tarih)
        distribution_by_fund = group_records_by_fund(distribution_records)
    else:
        distribution_by_fund = {}
        if served_by_bulk:
            print(
                f"[BULK] {len(served_by_bulk)} fonun verisi zaten guncel; "
                "dagilim istegi ve veritabani yazimi atlandi."
            )

    for fund_code in served_by_bulk:
        if fund_code not in changed:
            results[fund_code] = {"status": "success", "inserted": 0, "updated": 0}
            continue

        try:
            merged_records = merge_fund_data(
                general_maps[fund_code],
                build_distribution_map(distribution_by_fund.get(fund_code, [])),
            )
            if not merged_records:
                message = "Merge produced no usable records."
                print(f"[WARNING] [{fund_code}] {message} Skipping fund.")
                results[fund_code] = {"status": "error", "message": message}
                continue

            inserted, updated = store_fund_records(database, fund_code, merged_records)
            print(f"[SUCCESS] [{fund_code}] {inserted} inserted, {updated} updated.")
            results[fund_code] = {"status": "success", "inserted": inserted, "updated": updated}
        except Exception as exc:
            print(f"[CRITICAL] [{fund_code}] Unhandled exception: {exc}")
            results[fund_code] = {"status": "error", "message": str(exc)}

    if missing_from_bulk:
        fallback_results, session = scrape_funds_individually(
            session, database, missing_from_bulk, bas_tarih, bit_tarih,
            reason="fon toplu yanitta yok; hedefli sorguya dusuluyor",
        )
        results.update(fallback_results)

    _finish_scrape_run(database, results, run_started)
    return results


# --- Main ---------------------------------------------------------------------

if __name__ == "__main__":
    print("[SYSTEM] Initializing TEFAS API Data Scraper (hybrid mode)...")

    # Scrapes whatever is actually tracked in fund_database.json rather than
    # a hardcoded list. The previous hardcoded ["TLY", "PHE", "YAS"] silently
    # rotted as funds were added and removed through the UI: it refreshed
    # three funds (one of which, YAS, no longer existed in the database at
    # all) while leaving every other tracked fund untouched -- so a CLI/cron
    # run looked successful while most funds kept serving stale data.
    # A fund code passed on the command line overrides this, e.g.
    #     python data_scraper.py TLY PHE
    cli_fund_codes = [code.strip().upper() for code in sys.argv[1:] if code.strip()]
    fund_codes = cli_fund_codes or sorted(load_database().keys())

    if not fund_codes:
        print(f"[SYSTEM] {DATABASE_FILE} icinde takip edilen fon yok; yapilacak is bulunamadi.")
    else:
        source = "komut satiri" if cli_fund_codes else DATABASE_FILE
        print(f"[SYSTEM] {len(fund_codes)} fon {source} kaynagindan alindi: {', '.join(fund_codes)}")
        scrape_and_update(fund_codes)
