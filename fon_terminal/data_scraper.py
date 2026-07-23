import json
import os
import re
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
    "vmtl": "Mevduat (TL)",
    "gykb": "Gayrimenkul Yatırım Fonları Katılma Payları",
    "yyf": "Yatırım Fonları Katılma Payları",
    "vint": "Vadeli İşlemler Nakit Teminatları",
    "bpp": "Borsa İstanbul Para Piyasası",
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

    Returns a tuple (auth_header, cookies_header) or (None, None) on failure.
    """
    trigger_url = (
        f"https://www.tefas.gov.tr/tr/fon-verileri?fundType=YAT&search=TLY"
        f"&startDate={basTarih_str}&endDate={bitTarih_str}"
    )

    print("[HANDSHAKE] Launching headless browser to solve Next.js auth challenge...")

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
        # for, since Playwright/Chromium sets it by default. Runs headless
        # now that the WAF challenge and correct endpoints/payloads have
        # been diagnosed via visual/recon debugging.
        browser = playwright.chromium.launch(
            headless=True,
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

        finally:
            browser.close()
            print("[HANDSHAKE] Headless browser closed.")

    if not captured["authorization"]:
        print("[ERROR] [HANDSHAKE] Failed to capture a Bearer Authorization token from any request.")
        return None, None

    return captured["authorization"], captured["cookie"]


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


def build_distribution_payload(fund_code, bas_tarih, bit_tarih):
    """Payload schema for `dagilimSiraliGetirT`, the portfolio distribution
    endpoint, captured via Playwright recon after manually triggering the
    "Portföy Dağılımı" / "Varlık Dağılımı" tab.
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
        "basSira": 1,
        "bitSira": 100,
        "fonTurAciklama": None,
        "dil": "TR",
        "kurucuKod": None,
        "sFonTurKod": "",
        "fonKod": None,
        "fonGrup": "",
        "fonUnvanTip": "",
    }


def fetch_endpoint_data(session, url, fund_code, payload):
    """Issues a POST request to a TEFAS API endpoint with the given payload
    and returns the list of records for the given fund, or an empty list on
    any failure.
    """
    try:
        response = session.post(url, json=payload, timeout=20)
    except requests.exceptions.RequestException as exc:
        print(f"[ERROR] [{fund_code}] Network error calling {url}: {exc}")
        return []

    if response.status_code in (401, 403):
        print(
            f"[ERROR] [{fund_code}] {url} returned HTTP {response.status_code} "
            "(session token likely expired or rejected)"
        )
        return []

    if response.status_code != 200:
        print(f"[ERROR] [{fund_code}] {url} returned HTTP {response.status_code}")
        return []

    try:
        payload_json = response.json()
    except ValueError:
        print(f"[ERROR] [{fund_code}] Invalid JSON response from {url}")
        return []

    records = extract_records(payload_json)
    if not records:
        print(f"[WARNING] [{fund_code}] No records found in response from {url}")

    return filter_by_fund_code(records, fund_code)


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


# --- Local database (fund_database.json) persistence ------------------------

def load_database():
    """Loads the master fund database, or returns an empty dict if missing."""
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    return {}


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
        database[fund_code] = []

    records = database[fund_code]
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

    database[fund_code] = sort_fund_records(records)
    return action


# --- Main -------------------------------------------------------------------

if __name__ == "__main__":
    print("[SYSTEM] Initializing TEFAS API Data Scraper (hybrid mode)...")

    target_funds = ["TLY", "PHE", "YAS"]

    end_date_obj = datetime.now()
    start_date_obj = end_date_obj - timedelta(days=30)
    bit_tarih = end_date_obj.strftime("%Y%m%d")
    bas_tarih = start_date_obj.strftime("%Y%m%d")

    print(f"[SYSTEM] Fetching last 30 days of data: {bas_tarih} -> {bit_tarih}")

    database = load_database()

    auth_header, cookie_header = acquire_session_credentials(bas_tarih, bit_tarih, recon_mode=False)
    if not auth_header:
        print("[CRITICAL] Could not obtain a valid session. Aborting run.")
        raise SystemExit(1)

    session = build_authenticated_session(auth_header, cookie_header)

    for fund_code in target_funds:
        print(f"\n[{fund_code}] Requesting general info and portfolio distribution...")

        try:
            general_payload = build_general_info_payload(fund_code, bas_tarih, bit_tarih)
            distribution_payload = build_distribution_payload(fund_code, bas_tarih, bit_tarih)

            general_records = fetch_endpoint_data(session, GENERAL_INFO_URL, fund_code, general_payload)
            distribution_records = fetch_endpoint_data(session, DISTRIBUTION_URL, fund_code, distribution_payload)

            if not distribution_records:
                # Qualified/restricted funds (e.g. YAS) legitimately don't
                # publish a portfolio distribution breakdown for some or all
                # of the queried period; this is expected, not an error.
                print(
                    f"[INFO] [{fund_code}] Varlık dağılım verisi boş/bulunamadı "
                    "(Nitelikli/Kapalı fon olabilir)."
                )

            if not general_records:
                print(f"[WARNING] [{fund_code}] No general info retrieved. Skipping fund.")
                continue

            general_map = build_general_info_map(general_records)
            distribution_map = build_distribution_map(distribution_records)
            merged_records = merge_fund_data(general_map, distribution_map)

            if not merged_records:
                print(f"[WARNING] [{fund_code}] Merge produced no usable records. Skipping fund.")
                continue

            if distribution_records:
                # Only flag individual dates here (fund-level gaps were
                # already reported above); avoids repeating the same
                # message once per day for a fund with zero distribution
                # data across the whole queried period.
                for record in merged_records:
                    if not record.get("Varliklar"):
                        print(
                            f"[INFO] [{fund_code}] {record['Tarih']} için varlık dağılım verisi "
                            "boş/bulunamadı (Nitelikli/Kapalı fon olabilir)."
                        )

            inserted_count = 0
            updated_count = 0
            for record in merged_records:
                action = upsert_fund_record(database, fund_code, record)
                print(f"  [{fund_code}] {record['Tarih']} -> {action}")
                if action == "inserted":
                    inserted_count += 1
                else:
                    updated_count += 1

            save_database(database)
            print(
                f"[SUCCESS] [{fund_code}] {inserted_count} inserted, {updated_count} updated. "
                f"Saved to {DATABASE_FILE}."
            )

        except Exception as exc:
            print(f"[CRITICAL] [{fund_code}] Unhandled exception: {exc}")

        if fund_code != target_funds[-1]:
            time.sleep(random.uniform(1.5, 3.5))

    print("\n[SYSTEM] All funds processed. Database is up to date.")
