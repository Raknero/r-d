"""
kap_downloader.py

Standalone, self-contained module for downloading a Turkish investment
fund's "Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF
attachments from KAP (Kamuyu Aydinlatma Platformu / Public Disclosure
Platform).

Cadence note (2026-09-14): these reports used to be strictly monthly, and
are now filed WEEKLY by a growing share of funds -- both cadences appear
side by side, per fund, distinguished by KAP's `period` code ("AB"
monthly / "HB" weekly). This module therefore identifies a report by
KAP's own period metadata (see `ReportPeriod`) and never converts it into
a month. A report's actual as-of date is not this module's business at
all; it is established from the document's contents by `report_dating`.

This module lives in its own sandbox and has no dependency on any other
part of the host project; it only needs the third-party `requests`
library. It is designed to be dropped into (imported by) a larger project
later on, so all state is encapsulated in the `KAPPdfDownloader` class
rather than module-level globals.

Usage:
    from kap_downloader import KAPPdfDownloader

    downloader = KAPPdfDownloader(fon_kodu="TLY")
    results = downloader.download_reports(days_back=365)

    # or, as a context manager (closes the underlying HTTP session for you):
    with KAPPdfDownloader(fon_kodu="TLY") as downloader:
        downloader.run(days_back=180)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable_http_error(exc: BaseException) -> bool:
    """True ONLY for the two classes of KAP/TEFAS failure that are likely a
    transient server-side hiccup rather than a genuine, permanent problem
    with the request itself: HTTP 429 (Too Many Requests -- we've been
    rate-limited) or a 50x server error (500/502/503/504 -- the far end is
    temporarily struggling). A 404 (Not Found) or 400 (Bad Request) is a
    PERMANENT failure -- the URL/payload is wrong and will still be wrong
    on the 2nd, 3rd, ... attempt -- so those are deliberately excluded
    here and propagate immediately, unretried, exactly as before this
    module had any retry logic at all.
    """
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    response = exc.response
    if response is None:
        return False
    return response.status_code == 429 or response.status_code in (500, 502, 503, 504)


def _log_retry_attempt(retry_state) -> None:
    """`tenacity`'s `before_sleep` hook for `_request_with_retry` below.

    Always prints a Turkish, human-readable console warning. Additionally,
    IF the call this retry belongs to was given an `execution_logs` list
    (either as that keyword argument, or -- for retries triggered from an
    instance method that calls `_request_with_retry` on `self`'s own
    behalf -- discoverable via `self.execution_logs`), appends one entry
    to it too, so a KAP/TEFAS rate-limit stall shows up in the HTML
    report's own "Adım Adım Çalışma Günlüğü" (Execution Trace), not just
    in the console where it would be lost after the process exits.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    status_code = getattr(getattr(exc, "response", None), "status_code", "?")
    wait_seconds = retry_state.next_action.sleep if retry_state.next_action else 0.0
    message = (
        f"KAP/TEFAS API limitine takıldı (HTTP {status_code}), {wait_seconds:.0f} saniye "
        f"beklenip tekrar deneniyor... (deneme {retry_state.attempt_number}/5)"
    )
    print(f"[UYARI] {message}")

    execution_logs = (retry_state.kwargs or {}).get("execution_logs")
    if execution_logs is None and retry_state.args:
        execution_logs = getattr(retry_state.args[0], "execution_logs", None)
    if execution_logs is not None:
        execution_logs.append({"time": time.strftime("%H:%M:%S"), "message": message})


@retry(
    retry=retry_if_exception(_is_retryable_http_error),
    wait=wait_exponential(multiplier=2, min=2, max=16),
    stop=stop_after_attempt(5),
    before_sleep=_log_retry_attempt,
    reraise=True,
)
def _request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    execution_logs: Optional[List[Dict[str, str]]] = None,
    **kwargs,
) -> requests.Response:
    """Thin, purely-additive protective wrapper around `session.request()`
    for every outbound KAP/TEFAS HTTP call in this sandbox (used by both
    this module and `kap_delta_engine.py`, which imports it): performs the
    request, calls `raise_for_status()`, and -- ONLY on a 429/50x (see
    `_is_retryable_http_error`) -- retries up to 5 total attempts with
    exponential backoff (2s, 4s, 8s, 16s between attempts). A 404/400/etc.,
    or the 5th consecutive 429/50x, is raised immediately as a normal
    `requests.exceptions.RequestException` -- every EXISTING call site's
    own `try/except requests.exceptions.RequestException` block keeps
    working completely unmodified, since that's exactly the exception
    type this still raises in the end (`reraise=True`). `execution_logs`
    is optional and purely for `_log_retry_attempt`'s narrative logging
    above; it changes no request/response behavior.
    """
    response = session.request(method, url, **kwargs)
    response.raise_for_status()
    return response


def _parse_kap_publish_date(publish_date: str) -> Optional[datetime]:
    """Parses KAP's `publishDate` ("02.09.2026 11:02:16", or just
    "02.09.2026") into a `datetime`. Returns None instead of raising for
    an empty/unrecognized value, since a missing publish date must never
    abort a whole disclosure list."""
    raw = (publish_date or "").strip()
    if not raw:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# KAP's own `period` codes on "Portfoy Dagilim Raporu" filings. This field
# -- not `donem` -- is what actually states a report's cadence, and it is
# the one piece of KAP period metadata that has never been observed wrong.
PERIOD_CODE_MONTHLY = "AB"  # Aylık Bildirim
PERIOD_CODE_WEEKLY = "HB"   # Haftalık Bildirim

CADENCE_BY_PERIOD_CODE = {
    PERIOD_CODE_MONTHLY: "AYLIK",
    PERIOD_CODE_WEEKLY: "HAFTALIK",
}

# Stand-in code for a filing that carries no `period` at all, so a period
# slug can still be formed without inventing a cadence.
PERIOD_CODE_UNKNOWN = "XX"


@dataclass(frozen=True)
class ReportPeriod:
    """A disclosure's period EXACTLY as KAP filed it -- deliberately not
    converted into a month.

    Why this replaced the old month-normalizing logic (2026-09-14): funds
    moved to WEEKLY portfolio disclosure, so `donem` is now a week number
    whenever `period` is "HB" (TLY filed `donem=34` for the report
    published 02.09.2026 and `donem=35` for 09.09.2026). The previous
    `normalize_report_period()` forced such a tag into a month by taking
    "publish month - 1", which produced August for BOTH -- collapsing two
    distinct weekly reports onto one period, one dedup key and one local
    filename, so the older of the two was silently discarded and the two
    files overwrote each other on disk.

    Worse, that fabricated month then became the baseline's validity date
    (month end, 31.08.2026) even though the reports' holdings are from 04
    September -- see `report_dating`, which is now the ONLY thing allowed
    to determine a report's date, and does so from the document's data
    rather than from any label.

    So this type has one job: identify WHICH disclosure a file came from,
    uniquely and without fabrication. `slug` is that identity (e.g.
    "2026_HB35", "2026_AB07") and is used for local filenames and parser
    keys. It is NOT a date and must never be treated as one.
    """

    year: int
    ordinal: int      # KAP's raw `donem`: a month when code is "AB", a week number when "HB"
    code: str         # KAP's raw `period` code, upper-cased
    cadence: Optional[str] = None   # "AYLIK"/"HAFTALIK" when the code is recognized

    @property
    def slug(self) -> str:
        """Filename/dict-key-safe identity, e.g. "2026_HB35"."""
        return f"{self.year}_{self.code}{self.ordinal:02d}"

    @property
    def label(self) -> str:
        """Human-readable form for logs, e.g. "2026 / HB 35".

        The ordinal is KAP's filing number, not an ISO week, so the label
        does not say "hafta".
        """
        if self.code == PERIOD_CODE_WEEKLY:
            return f"{self.year} / HB {self.ordinal}"
        if self.code == PERIOD_CODE_MONTHLY:
            return f"{self.year} / {self.ordinal:02d}. ay (AB)"
        return f"{self.year} / dönem {self.ordinal} ({self.code})"


def describe_report_period(
    year: Optional[int],
    donem: Optional[int],
    period_code: Optional[str] = None,
) -> Optional[ReportPeriod]:
    """Wraps KAP's raw `(year, donem, period)` metadata in a
    `ReportPeriod` without interpreting `donem` as a month (see that
    class for why interpreting it was the bug).

    Returns None only when `year`/`donem` are missing or non-numeric, so
    the caller can skip that disclosure with a warning. Unlike the
    function this replaced, it can never raise and never needs
    `publish_date` to guess a month -- an unrecognized future `period`
    code simply yields `cadence=None`, which callers treat as "cadence
    unknown" rather than as an error.
    """
    if year is None or donem is None:
        return None
    try:
        year_int, ordinal_int = int(year), int(donem)
    except (TypeError, ValueError):
        return None
    if ordinal_int < 0:
        return None

    code = (period_code or "").strip().upper() or PERIOD_CODE_UNKNOWN
    if not code.isalpha() or len(code) > 3:
        code = PERIOD_CODE_UNKNOWN

    return ReportPeriod(
        year=year_int,
        ordinal=ordinal_int,
        code=code,
        cadence=CADENCE_BY_PERIOD_CODE.get(code),
    )


@dataclass
class DisclosureRecord:
    """One "Portfoy Dagilim Raporu" entry from KAP's disclosure filter
    API, trimmed down to only the fields this module needs.

    `period` carries KAP's own period metadata verbatim (see
    `ReportPeriod`); there is deliberately no month field, because a
    weekly filing has no month to speak of. `published_at` is the parsed
    `publish_date`, which is what orders records -- it is the only
    period-independent ordering signal KAP exposes that has not been
    observed mislabelled.
    """

    disclosure_id: str
    disclosure_index: int
    period: ReportPeriod
    attachment_count: int
    publish_date: str
    published_at: Optional[datetime] = None

    @property
    def sort_key(self) -> Tuple[datetime, int]:
        """Newest-first ordering key: publication instant, then
        `disclosure_index` to break ties deterministically."""
        return (self.published_at or datetime.min, self.disclosure_index)


class KAPPdfDownloader:
    """Downloads "Portfoy Dagilim Raporu" (Portfolio Allocation Report)
    PDF attachments for a tracked KAP-listed fund, on whichever cadence
    the fund files them (see this module's docstring).

    The pipeline is a 2-stage process against KAP's internal (undocumented)
    backend API:

    1. `_fetch_disclosure_list()` calls the disclosure filter endpoint to
       get every "Portfoy Dagilim Raporu" published for the fund in the
       requested window, each carrying a `disclosureIndex` and an
       `attachmentCount`.
    2. `_resolve_attachment()` fetches that disclosure's public detail
       page (`/tr/Bildirim/{disclosureIndex}`) and extracts the *real*
       attachment file ID from its HTML: KAP's PDF file IDs are NOT the
       same as `disclosureId` (they diverge in their last several hex
       characters), and there is no separate JSON "detail" API that
       exposes them -- the ID is only ever rendered directly into the
       disclosure detail page's server-side HTML, next to the attachment's
       display filename (e.g. "TLY_2026.06.pdf"). A plain `requests.get`
       against that page is enough; no browser/JS execution is needed
       because the page is fully server-rendered.

    KAP's disclosure filter endpoint is scoped to a specific fund via two
    opaque GUIDs (a "company"/fund OID and a "member" OID) rather than the
    human-readable fund code. Fund identity resolution (`__init__`) tries
    two sources, in order:

    1. `KNOWN_FUNDS` -- a small, hand-verified static override (kept for
       funds worth pinning explicitly / documenting a manager's own
       `unvan`). Checked first so it never pays a network round-trip.
    2. `build_dynamic_fund_directory()` -- scrapes EVERY fund's `company_oid`
       from KAP's public "Yatirim Fonlari" listing page (see that method's
       docstring), so ANY fund code KAP tracks works out of the box, not
       just the handful registered above.

    Endpoint discovery note (2026-07-30): the FILTER_ENDPOINT's "member_oid"
    path segment was originally assumed to be unique per fund/manager (it
    was captured from TLY's own network traffic). Live probing proved this
    wrong -- it is NOT fund- or manager-specific: reusing TLY's own value
    against completely unrelated funds (Is Portfoy's IHK, plus a random
    sample of 12 other fund codes spanning several different portfolio
    management companies) correctly returned that OTHER fund's own
    "Portfoy Dagilim Raporu" disclosures in every case where the fund
    actually publishes one. So this single constant (`GENERIC_MEMBER_OID`)
    can safely be reused for any fund's `company_oid`, which is what makes
    dynamic, code-only fund resolution possible at all.
    """

    BASE_URL = "https://kap.org.tr"
    FILTER_ENDPOINT = "/tr/api/disclosure/filter/FILTERYFBF/{company_oid}/{member_oid}/{days_back}"
    DETAIL_PAGE = "/tr/Bildirim/{disclosure_index}"
    DOWNLOAD_ENDPOINT = "/tr/api/file/download/{attachment_id}"
    FUND_DIRECTORY_URL = "https://kap.org.tr/tr/YatirimFonlari/ALL"

    REPORT_TITLE = "Portfoy Dagilim Raporu"

    # See "Endpoint discovery note" in the class docstring above: verified
    # (2026-07-30) to work as a generic, fund-independent path segment for
    # FILTER_ENDPOINT, not just for TLY.
    GENERIC_MEMBER_OID = "8aca490d502e34b801502e380044002b"

    # Small, hand-verified static override, checked before the dynamic
    # directory (see class docstring). NOT the only supported funds
    # anymore -- add an entry here only if you want to pin/document a
    # specific fund manually; everything else is resolved dynamically.
    KNOWN_FUNDS: Dict[str, Dict[str, str]] = {
        "TLY": {
            "company_oid": "4028328c7812c9c301781bc5fe843290",
            "member_oid": "8aca490d502e34b801502e380044002b",
            "unvan": "TERA PORTFOY BIRINCI SERBEST FON",
        },
    }

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # Regex over the raw, backslash-escaped JSON embedded in
    # FUND_DIRECTORY_URL's server-rendered Next.js payload (inside a
    # <script> tag -- this is NOT real DOM/table markup, so BeautifulSoup
    # is used only to pull out <script> text, and this pattern parses the
    # embedded data itself). Matches one `fundPermaLinks[]` entry at a
    # time; field order (mkkMemberOid, kapMemberOid, permaLink, title,
    # fundCode, fundOid) was verified stable across all 4483 entries found
    # on the page on 2026-07-30. There is no public JSON/REST API for this
    # listing -- this page is the only available source.
    _FUND_DIRECTORY_ENTRY_PATTERN = re.compile(
        r'\\"mkkMemberOid\\":(?:null|\\"(?P<mkk>[0-9a-fA-F]+)\\"),'
        r'\\"kapMemberOid\\":\\"(?P<kap>[0-9A-Fa-f]+)\\",'
        r'\\"permaLink\\":\\"(?P<permalink>[^"\\]*)\\",'
        r'\\"title\\":\\"(?P<title>[^"\\]*)\\",'
        r'\\"fundCode\\":\\"(?P<code>[A-Z0-9]{2,10})\\",'
        r'\\"fundOid\\":\\"(?P<oid>[0-9A-Fa-f]+)\\"'
    )

    # Class-level cache: the dynamic directory is the same for every fund,
    # so it's fetched at most once per process (see
    # `build_dynamic_fund_directory`'s `force_refresh` to bypass this).
    _dynamic_fund_directory_cache: Optional[Dict[str, Dict[str, str]]] = None

    # The attachment download endpoint responds with `Content-Type:
    # application/pdf` but the body is actually a Java-serialized `byte[]`
    # (a leftover of the legacy Java backend), not a raw PDF stream. The
    # real PDF bytes start at the first "%PDF" magic marker and run
    # through to the end of the response with no further encoding, so
    # stripping everything before that marker recovers the exact original
    # file byte-for-byte (verified against the serialized array's own
    # length prefix).
    PDF_MAGIC = b"%PDF"

    def __init__(
        self,
        fon_kodu: str = "TLY",
        output_dir: Optional[str] = None,
        request_delay: float = 0.5,
        timeout: int = 30,
        use_dynamic_directory: bool = True,
    ):
        fon_kodu = fon_kodu.strip().upper()

        fund_config = self.KNOWN_FUNDS.get(fon_kodu)
        source = "statik (KNOWN_FUNDS)" if fund_config else None

        if fund_config is None and use_dynamic_directory:
            dynamic_directory = self.build_dynamic_fund_directory(timeout=timeout)
            fund_config = dynamic_directory.get(fon_kodu)
            source = "dinamik (KAP fon dizini)" if fund_config else None

        if fund_config is None:
            supported = ", ".join(self.KNOWN_FUNDS)
            raise ValueError(
                f"'{fon_kodu}' fonu ne statik KNOWN_FUNDS sozlugunde ({supported}) ne de "
                f"KAP'in dinamik fon dizininde ({self.FUND_DIRECTORY_URL}) bulunamadi. "
                "Fon kodunu kontrol edin ya da (opsiyonel) KNOWN_FUNDS'a manuel kayit ekleyin."
            )

        self.fon_kodu = fon_kodu
        self.fund_config = fund_config
        self.output_dir = output_dir or f"{fon_kodu.lower()}_pdfs"
        self.request_delay = request_delay
        self.timeout = timeout
        self.session = self._build_session()

        os.makedirs(self.output_dir, exist_ok=True)
        print(f"[BILGI] [{fon_kodu}] Fon kimligi kaynagi: {source}.")

    # --- Context manager support --------------------------------------------

    def __enter__(self) -> "KAPPdfDownloader":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Closes the underlying HTTP session/connection pool."""
        self.session.close()

    # --- Dynamic fund directory (replaces hard-coded KNOWN_FUNDS) -------------

    @classmethod
    def build_dynamic_fund_directory(
        cls, force_refresh: bool = False, timeout: int = 30
    ) -> Dict[str, Dict[str, str]]:
        """Scrapes KAP's public "Yatirim Fonlari" listing page
        (`FUND_DIRECTORY_URL`) and returns a dynamic, code-keyed directory
        for EVERY fund KAP tracks (verified 4483 entries on 2026-07-30) --
        a drop-in dynamic replacement for the hand-maintained `KNOWN_FUNDS`
        dict:

            {
                "TLY": {
                    "company_oid": "4028328c7812c9c301781bc5fe843290",
                    "member_oid": GENERIC_MEMBER_OID,
                    "mkk_member_oid": "5553acdacf15471ba80c28eb45cdd9e7",
                    "permalink": "tly-tera-portfoy-birinci-serbest-fon",
                    "unvan": "TERA PORTFÖY BİRİNCİ SERBEST FON",
                },
                ...
            }

        The page is a Next.js app whose fund list is embedded as
        backslash-escaped JSON inside an inline `<script>` tag rather than
        as real DOM/table markup (there is no separate public JSON/REST
        endpoint for it), so BeautifulSoup is used only to pull out every
        `<script>` tag's raw text, which `_FUND_DIRECTORY_ENTRY_PATTERN`
        then parses directly (see that pattern's docstring for why regex
        is used here instead of `json.loads`).

        Cached at the class level after the first successful call --
        pass `force_refresh=True` to bypass/refresh it. `timeout` bounds
        the one HTTP request this makes (KAP can be slow to respond).

        Never raises: any network error, non-2xx response, or a page whose
        structure no longer matches `_FUND_DIRECTORY_ENTRY_PATTERN` is
        caught, logged as "[HATA]"/"[UYARI]", and results in an EMPTY dict
        being returned -- callers (see `__init__`) then naturally fall
        back to whatever is in the static `KNOWN_FUNDS` registry instead
        of crashing.
        """
        if cls._dynamic_fund_directory_cache is not None and not force_refresh:
            return cls._dynamic_fund_directory_cache

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": cls.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )

        try:
            response = _request_with_retry(session, "GET", cls.FUND_DIRECTORY_URL, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] Dinamik fon dizini alinamadi ({cls.FUND_DIRECTORY_URL}): {exc}")
            return {}
        finally:
            session.close()

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            script_text = "\n".join(tag.get_text() for tag in soup.find_all("script"))
        except Exception as exc:  # noqa: BLE001 - malformed HTML must never crash the caller
            print(f"[HATA] Fon dizini sayfasi (HTML) ayristirilamadi: {exc}")
            return {}

        if not script_text.strip():
            # Defensive fallback in case BeautifulSoup's <script> extraction
            # comes up empty for some reason -- the regex below is applied
            # directly against the raw response body instead.
            script_text = response.text

        directory: Dict[str, Dict[str, str]] = {}
        for match in cls._FUND_DIRECTORY_ENTRY_PATTERN.finditer(script_text):
            code = (match.group("code") or "").strip().upper()
            if not code or code in directory:
                continue
            directory[code] = {
                "company_oid": match.group("oid"),
                "member_oid": cls.GENERIC_MEMBER_OID,
                "mkk_member_oid": match.group("mkk") or "",
                "permalink": match.group("permalink") or "",
                "unvan": match.group("title") or "",
            }

        if not directory:
            print(
                f"[UYARI] Dinamik fon dizini bos dondu; KAP sayfa yapisi degismis olabilir "
                f"({cls.FUND_DIRECTORY_URL})."
            )
        else:
            print(f"[BILGI] Dinamik fon dizini olusturuldu: {len(directory)} fon bulundu.")

        cls._dynamic_fund_directory_cache = directory
        return directory

    # --- Session setup -------------------------------------------------------

    def _build_session(self) -> requests.Session:
        """Builds a requests.Session with realistic browser-like headers
        (rather than requests' default User-Agent) so KAP's bot detection
        doesn't reject scripted requests, and reuses the same TCP
        connection across every call in a run.
        """
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": f"{self.BASE_URL}/tr/",
                "Origin": self.BASE_URL,
                "Connection": "keep-alive",
            }
        )
        return session

    # --- Stage 1: fetch the disclosure list -----------------------------------

    def _fetch_disclosure_list(self, days_back: int) -> List[DisclosureRecord]:
        """Calls KAP's disclosure filter API and returns every
        "Portfoy Dagilim Raporu" entry that has at least one attachment,
        deduplicated so only the most-recently-published filing per
        PERIOD SLUG is kept (KAP occasionally republishes a corrected
        report for a period it already reported).

        Deduplicating on `period.slug` -- which includes KAP's `period`
        code and raw `donem` -- rather than on a derived (year, month) is
        what keeps consecutive WEEKLY reports separate: TLY's 02.09.2026
        and 09.09.2026 filings are `2026_HB34` and `2026_HB35`, but both
        used to normalize to (2026, 8), so the earlier one was silently
        dropped here and never seen again (verified 2026-09-14: KAP
        returned 5 disclosures, this method emitted 4).
        """
        url = self.BASE_URL + self.FILTER_ENDPOINT.format(
            company_oid=self.fund_config["company_oid"],
            member_oid=self.fund_config["member_oid"],
            days_back=days_back,
        )

        try:
            response = _request_with_retry(self.session, "GET", url, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] [{self.fon_kodu}] Rapor listesi alinamadi: {exc}")
            return []

        try:
            payload = response.json()
        except ValueError:
            print(f"[HATA] [{self.fon_kodu}] Rapor listesi JSON olarak ayristirilamadi.")
            return []

        if not isinstance(payload, list):
            print(f"[HATA] [{self.fon_kodu}] Beklenmeyen API yanit formati (liste degil).")
            return []

        best_by_period: Dict[str, DisclosureRecord] = {}
        for item in payload:
            basic = item.get("disclosureBasic") if isinstance(item, dict) else None
            if not basic:
                continue

            attachment_count = basic.get("attachmentCount") or 0
            if attachment_count <= 0:
                # No PDF to fetch for this disclosure; skip silently, it's
                # not an error condition (e.g. a text-only correction note).
                continue

            year, donem, disclosure_index = basic.get("year"), basic.get("donem"), basic.get("disclosureIndex")
            if disclosure_index is None:
                continue

            publish_date = basic.get("publishDate") or ""
            period_code = basic.get("period")

            period = describe_report_period(year=year, donem=donem, period_code=period_code)
            if period is None:
                print(
                    f"[UYARI] [{self.fon_kodu}] Dönem bilgisi okunamadi "
                    f"(disclosureIndex={disclosure_index}, year={year!r}, donem={donem!r}, "
                    f"period={period_code!r}); bu bildirim atlaniyor."
                )
                continue

            record = DisclosureRecord(
                disclosure_id=basic.get("disclosureId"),
                disclosure_index=disclosure_index,
                period=period,
                attachment_count=attachment_count,
                publish_date=publish_date,
                published_at=_parse_kap_publish_date(publish_date),
            )

            if period.cadence is None:
                print(
                    f"[UYARI] [{self.fon_kodu}] KAP dönem kodu taninmiyor "
                    f"(period={period_code!r}, donem={donem!r}, disclosureIndex={disclosure_index}); "
                    f"ritim bilinmiyor olarak isaretlendi. Rapor tarihi yine de icerikten "
                    f"belirlenecek (report_dating), bu yuzden islem durmuyor."
                )

            existing = best_by_period.get(period.slug)
            if existing is None or record.sort_key > existing.sort_key:
                best_by_period[period.slug] = record

        # Sorted NEWEST-FIRST by publication instant. KAP's API documents
        # no ordering/pagination guarantee, and no period field can be
        # used for this anymore: comparing a weekly `donem` (a week
        # number) against a monthly one (a month) is meaningless, and both
        # cadences appear in the same list during the transition.
        records = sorted(best_by_period.values(), key=lambda r: r.sort_key, reverse=True)
        cadence_summary = ", ".join(
            f"{r.period.slug}({r.publish_date[:10]})" for r in records[:6]
        )
        print(
            f"[BILGI] [{self.fon_kodu}] {len(records)} adet '{self.REPORT_TITLE}' bulundu "
            f"(son {days_back} gun icinde){': ' + cadence_summary if cadence_summary else ''}."
        )
        return records

    def find_latest_report(self, days_back: int = 365) -> Optional[DisclosureRecord]:
        """Returns the SINGLE most recently published "Portfoy Dagilim
        Raporu" for this fund, chosen by publication instant (see
        `DisclosureRecord.sort_key`).

        Publication order is used because no period field can order these
        records anymore: during the monthly-to-weekly transition a fund's
        list mixes "AB" filings (whose `donem` is a month) with "HB" ones
        (whose `donem` is a week number), and the two scales are not
        comparable. The previous implementation built `date(year, donem,
        1)` from a fabricated month, which both crashed on week tags and
        ranked two different weekly reports as identical.

        Note this deliberately answers "most recently PUBLISHED", not
        "most recent DATA" -- those can differ if a filer republishes an
        old period. The downloaded report's real as-of date is established
        separately and from its contents (`report_dating`), so a
        surprising date surfaces there as a dated baseline rather than as
        a silent one.

        Returns None (never raises) if no report was found at all within
        `days_back` days.
        """
        records = self._fetch_disclosure_list(days_back)
        if not records:
            return None
        return max(records, key=lambda r: r.sort_key)

    def _clean_old_reports(self, keep_slug: str) -> None:
        """Deletes every other "{fon_kodu}_*.pdf" already sitting in
        `self.output_dir` whose period slug is NOT `keep_slug`, so a stale
        report left over from a previous run can never linger alongside
        (or be mistaken for) the freshly downloaded latest one -- this is
        what `KAPPdfParser.parse_directory` would otherwise pick up right
        alongside the new file.

        Matches any slug shape, which also cleans up files written by the
        pre-2026-09-14 "{fon_kodu}_YYYY_MM.pdf" scheme -- those are
        actively harmful to leave behind, since a legacy "TLY_2026_08.pdf"
        is a WEEKLY report misfiled under a month.

        Never raises: a file that can't be removed is logged as "[UYARI]"
        and left in place rather than aborting the run.
        """
        if not os.path.isdir(self.output_dir):
            return

        pattern = re.compile(rf"^{re.escape(self.fon_kodu)}_(?P<slug>.+)\.pdf$", re.IGNORECASE)
        for filename in os.listdir(self.output_dir):
            match = pattern.match(filename)
            if not match or match.group("slug").upper() == keep_slug.upper():
                continue

            filepath = os.path.join(self.output_dir, filename)
            try:
                os.remove(filepath)
                print(f"[TEMİZLİK] [{self.fon_kodu}] Eski (guncel olmayan) rapor silindi: {filename}")
            except OSError as exc:
                print(f"[UYARI] [{self.fon_kodu}] Eski rapor silinemedi ({filename}): {exc}")

    # Matches the "{YYYY}{sep}{MM}" period an attachment's own display
    # filename carries, e.g. "TLY_2026.08.pdf".
    _ATTACHMENT_PERIOD_PATTERN = re.compile(r"(20\d{2})[._\-](0[1-9]|1[0-2])(?!\d)")

    def _note_attachment_period(self, record: DisclosureRecord, remote_filename: str) -> None:
        """Logs -- for the audit trail only -- the period the attachment's
        own display filename claims.

        This used to OVERRIDE the disclosure's period on the theory that
        the filer names the document more carefully than it tags the
        filing. That theory is now disproved: both of TLY's weekly
        reports (`2026_HB34`, published 02.09.2026, and `2026_HB35`,
        published 09.09.2026) ship as "TLY_2026.08.pdf", so the filename
        is not merely wrong about the month -- it cannot even tell two
        different reports apart. It is kept as an observation because a
        mismatch is a useful signal that a filer is still labelling
        weekly reports with monthly names.
        """
        match = self._ATTACHMENT_PERIOD_PATTERN.search(remote_filename or "")
        if not match:
            return

        file_year, file_month = int(match.group(1)), int(match.group(2))
        if record.period.code == PERIOD_CODE_MONTHLY and (file_year, file_month) == (
            record.period.year,
            record.period.ordinal,
        ):
            return

        print(
            f"[BILGI] [{self.fon_kodu}] Ek dosya adi '{remote_filename}' "
            f"{file_month:02d}/{file_year} dönemini ima ediyor, KAP metadata ise "
            f"{record.period.label}. Dosya adi BAGLAYICI DEGIL; raporun gercek tarihi "
            f"icerikten belirlenecek (report_dating)."
        )

    def download_latest_report(self, days_back: int = 365, clean_old_files: bool = True) -> Optional[dict]:
        """Downloads ONLY the single most recently published "Portfoy
        Dagilim Raporu" for this fund (see `find_latest_report`), instead
        of every report found in the window.

        This fixes the "Date Lag" bug (2026-07-31): `kap_delta_engine.
        collect_global_baseline` used to infer a fund's baseline period
        from whatever PDFs happened to already be sitting in its local
        `{fon_kodu}_pdfs/` folder (e.g. a stale "2026_03" left over from
        an earlier test run) instead of asking KAP what the fund's TRUE
        latest published report is -- silently losing every month in
        between (April/May/June...) as if they never existed.

        If `clean_old_files` is True (the default), every OTHER
        "{fon_kodu}_*.pdf" already in `self.output_dir` is deleted first
        (see `_clean_old_reports`), so `KAPPdfParser.parse_directory()`
        can never accidentally pick up a stale month alongside (or
        instead of) the fresh one.

        The local filename is `{fon_kodu}_{period.slug}.pdf` (e.g.
        "TLY_2026_HB35.pdf"), i.e. KAP's own period identity rather than a
        month derived from it. This is what stops two consecutive WEEKLY
        reports -- both of which KAP labels August and names
        "TLY_2026.08.pdf" -- from overwriting each other on disk.

        Note what this method deliberately does NOT do: it does not report
        the period as a date. A slug identifies a disclosure; the report's
        real as-of date comes from `report_dating.resolve_as_of_date`,
        which reads it out of the document's own figures. Callers needing
        the baseline's validity date must go through that.

        Returns a result dict `{"period_slug", "period_label", "year",
        "donem", "period_code", "cadence", "publish_date", "status",
        "file"}`, or None if no report could be found at all (logged,
        never raised -- one fund's missing report must never abort a
        caller looping over several funds).
        """
        print(f"[SISTEM] [{self.fon_kodu}] KAP'taki EN GUNCEL '{self.REPORT_TITLE}' araniyor...")

        latest = self.find_latest_report(days_back=days_back)
        if latest is None:
            print(f"[SISTEM] [{self.fon_kodu}] Hicbir donem icin rapor bulunamadi.")
            return None

        print(
            f"\n[{self.fon_kodu}] {latest.period.label} donemi (EN GUNCEL, yayin "
            f"{latest.publish_date}) isleniyor (disclosureIndex={latest.disclosure_index})..."
        )

        def describe(status: str, **extra) -> dict:
            """Every return path reports the same period fields, so a
            caller never has to reconstruct them from a filename."""
            return {
                "period_slug": latest.period.slug,
                "period_label": latest.period.label,
                "period_code": latest.period.code,
                "cadence": latest.period.cadence,
                "year": latest.period.year,
                "donem": latest.period.ordinal,
                "publish_date": latest.publish_date,
                "disclosure_index": latest.disclosure_index,
                "status": status,
                **extra,
            }

        try:
            resolved = self._resolve_attachment(latest.disclosure_index)
            if not resolved:
                print(f"[HATA] [{self.fon_kodu}] En guncel rapor icin PDF eki bulunamadi.")
                return describe("error", file=None, message="PDF eki bulunamadi.")

            attachment_id, remote_filename = resolved
            self._note_attachment_period(latest, remote_filename)

            if clean_old_files:
                self._clean_old_reports(keep_slug=latest.period.slug)

            local_filename = f"{self.fon_kodu}_{latest.period.slug}.pdf"
            success = self._download_pdf(attachment_id, local_filename)
        except Exception as exc:  # noqa: BLE001 - never let one fund's failure crash a caller's loop
            print(f"[KRITIK HATA] [{self.fon_kodu}] En guncel rapor indirilirken beklenmeyen hata: {exc}")
            return describe("error", file=None, message=str(exc))

        if not success:
            return describe("error", file=None)

        print(
            f"[SISTEM] [{self.fon_kodu}] En guncel baseline raporu bulundu: "
            f"{latest.period.label} -> {local_filename} "
            f"(gercek veri tarihi PDF iceriginden belirlenecek)"
        )
        return describe("success", file=local_filename)

    # --- Stage 2: resolve the real attachment ID, then download --------------

    def _resolve_attachment(self, disclosure_index: int) -> Optional[Tuple[str, str]]:
        """Fetches the disclosure's public detail page and extracts the
        real attachment file ID + display filename for its PDF.

        Returns (attachment_id, filename), or None if no PDF attachment
        could be found on the page.
        """
        url = self.BASE_URL + self.DETAIL_PAGE.format(disclosure_index=disclosure_index)
        try:
            response = _request_with_retry(
                self.session, "GET", url, timeout=self.timeout, headers={"Accept": "text/html,application/xhtml+xml"}
            )
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] [{self.fon_kodu}] Detay sayfasi alinamadi (disclosureIndex={disclosure_index}): {exc}")
            return None

        match = re.search(
            r'api/file/download/([a-f0-9]+)">([^<]+?\.pdf)</a>',
            response.text,
            re.IGNORECASE,
        )
        if not match:
            print(
                f"[UYARI] [{self.fon_kodu}] Detay sayfasinda PDF eki bulunamadi "
                f"(disclosureIndex={disclosure_index})."
            )
            return None

        attachment_id, remote_filename = match.group(1), match.group(2)
        return attachment_id, remote_filename

    def _download_pdf(self, attachment_id: str, local_filename: str) -> bool:
        """Downloads a single PDF attachment and saves it under
        `self.output_dir`. Returns True on success, False on any failure.
        """
        url = self.BASE_URL + self.DOWNLOAD_ENDPOINT.format(attachment_id=attachment_id)
        local_path = os.path.join(self.output_dir, local_filename)

        try:
            response = _request_with_retry(
                self.session, "GET", url, timeout=self.timeout, headers={"Accept": "application/pdf,*/*"}
            )
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] [{self.fon_kodu}] {local_filename} indirilemedi: {exc}")
            return False

        pdf_bytes = self._unwrap_pdf_bytes(response.content)
        if pdf_bytes is None:
            print(
                f"[HATA] [{self.fon_kodu}] {local_filename}: yanit icinde gecerli bir PDF "
                "(%PDF imzasi) bulunamadi; dosya kaydedilmedi."
            )
            return False

        try:
            with open(local_path, "wb") as file:
                file.write(pdf_bytes)
        except OSError as exc:
            print(f"[HATA] [{self.fon_kodu}] {local_filename} diske yazilamadi: {exc}")
            return False

        print(f"[BASARILI] [{self.fon_kodu}] {local_filename} indirildi ({len(pdf_bytes):,} bayt).")
        return True

    def _unwrap_pdf_bytes(self, raw_content: bytes) -> Optional[bytes]:
        """Strips the Java-serialization `byte[]` envelope that KAP's
        download endpoint wraps every PDF in (see `PDF_MAGIC` docstring
        above) and returns the real PDF bytes, or None if no PDF marker
        is present at all (e.g. KAP returned an error page/JSON instead).
        """
        marker_index = raw_content.find(self.PDF_MAGIC)
        if marker_index == -1:
            return None
        return raw_content[marker_index:]

    # --- Public entry point ---------------------------------------------------

    def download_reports(
        self,
        days_back: int = 365,
        published_since: Optional[date] = None,
        published_until: Optional[date] = None,
    ) -> List[dict]:
        """Downloads every available "Portfoy Dagilim Raporu" PDF for this
        fund published in the last `days_back` days, saving each as
        "{FON_KODU}_{PERIOD_SLUG}.pdf" (e.g. "TLY_2026_HB35.pdf") inside
        `self.output_dir`.

        `published_since` / `published_until` are optional inclusive
        PUBLICATION-DATE bounds for narrowing the result down without
        changing how far back KAP itself is queried (`days_back` must
        still cover the requested range, since it controls what KAP's API
        returns in the first place).

        These used to be `(year, donem)` period bounds. That comparison
        stopped meaning anything once funds began filing weekly: a weekly
        `donem` is a week number, so `(2026, 34) <= (2026, 8)` compares a
        week against a month and silently excludes the wrong records.
        Publication date is well-defined across both cadences.

        Returns a list of per-report result dicts, e.g.:
            [{"period_slug": "2026_HB35", "status": "success",
              "file": "TLY_2026_HB35.pdf", ...}, ...]

        Never raises: every per-report failure is caught, logged to the
        console, and recorded in the returned results so one bad report
        doesn't abort the whole run.
        """
        print(f"[SISTEM] [{self.fon_kodu}] KAP {self.REPORT_TITLE} indirme islemi basliyor...")

        records = self._fetch_disclosure_list(days_back)
        if published_since is not None:
            records = [
                r for r in records if r.published_at and r.published_at.date() >= published_since
            ]
        if published_until is not None:
            records = [
                r for r in records if r.published_at and r.published_at.date() <= published_until
            ]

        if not records:
            print(f"[SISTEM] [{self.fon_kodu}] Indirilecek rapor bulunamadi.")
            return []

        results: List[dict] = []
        for index, record in enumerate(records):
            local_filename = f"{self.fon_kodu}_{record.period.slug}.pdf"
            base = {
                "period_slug": record.period.slug,
                "period_label": record.period.label,
                "period_code": record.period.code,
                "cadence": record.period.cadence,
                "year": record.period.year,
                "donem": record.period.ordinal,
                "publish_date": record.publish_date,
                "disclosure_index": record.disclosure_index,
            }
            print(
                f"\n[{self.fon_kodu}] {record.period.label} donemi isleniyor "
                f"(disclosureIndex={record.disclosure_index})..."
            )

            try:
                resolved = self._resolve_attachment(record.disclosure_index)
                if not resolved:
                    results.append({**base, "status": "error", "file": None, "message": "PDF eki bulunamadi."})
                    continue

                attachment_id, _remote_filename = resolved
                success = self._download_pdf(attachment_id, local_filename)
                results.append(
                    {
                        **base,
                        "status": "success" if success else "error",
                        "file": local_filename if success else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - a single bad report must never abort the run
                print(
                    f"[KRITIK HATA] [{self.fon_kodu}] {record.period.label} islenirken "
                    f"beklenmeyen hata: {exc}"
                )
                results.append({**base, "status": "error", "file": None, "message": str(exc)})

            if index != len(records) - 1:
                time.sleep(self.request_delay)

        success_count = sum(1 for r in results if r["status"] == "success")
        print(
            f"\n[SISTEM] [{self.fon_kodu}] Tamamlandi: {success_count}/{len(results)} rapor basariyla "
            f"indirildi -> '{self.output_dir}/'"
        )
        return results

    # Alias so callers can use whichever name feels more natural.
    run = download_reports


if __name__ == "__main__":
    with KAPPdfDownloader(fon_kodu="TLY") as downloader:
        downloader.download_reports(days_back=365)
