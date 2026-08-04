"""
kap_delta_engine.py

Standalone, self-contained module that keeps a fund's holdings snapshot
"fresh" between two monthly "Portfoy Dagilim Raporu" (Portfolio Allocation
Report) publications, by layering KAP's intra-month "Pay Alim Satim
Bildirimi" (Shares Transaction Notification) disclosures on top of the
last known PDF-derived baseline.

Endpoint history / correction (2026-07-28):
    The first version of this module queried the same `FILTERYFBF`
    disclosure-filter endpoint `kap_downloader.KAPPdfDownloader` uses for
    the monthly report, filtered client-side by title keyword. That
    endpoint is scoped to the "Yatirim Fonu Bildirimleri" (fund report)
    category only and returned zero buy/sell notices for TLY -- this was
    WRONG, not a real absence of data. "Pay Alim Satim Bildirimi"
    disclosures are filed under a completely different category (`ODA`)
    by the fund's *portfolio management company*, via KAP's general
    member-disclosure query endpoint. Confirmed live for TLY on
    2026-07-28: 113 such disclosures exist in the last 12 months via
    `POST /tr/api/disclosure/members/byCriteria`. See `_fetch_delta_disclosures`.

IMPORTANT / UNRESOLVED DATA LIMITATION (verified live, not assumed):
    Every single one of TLY's 113 "Pay Alim Satim Bildirimi" disclosures
    is filed at the portfolio management company level ("Tera Portfoy
    Yonetimi A.S.") and covers MULTIPLE funds it manages simultaneously
    (e.g. one notice's "Ilgili Fonlar" = [T3B, TLY, TMV, TGI]). The
    disclosure's transaction table (buy/sell/net nominal TL, start/end-of-day
    holdings) is a SINGLE AGGREGATE figure for the manager's combined
    position across all of those funds -- KAP's data does not provide a
    per-fund breakdown anywhere in the disclosure. 0 of 113 checked
    disclosures name TLY alone.

    Because of this, `apply_delta()` currently only auto-applies a
    disclosure to the baseline when it names *exactly* the target fund
    and no others (an unambiguous case). Every multi-fund disclosure is
    parsed correctly and reported in full (see `unresolved` return value)
    but deliberately NOT merged into the baseline, since attributing the
    manager's combined trade to one fund would fabricate a number KAP
    never actually published. Do not change this without an explicit
    attribution rule from the fund itself (e.g. AUM-proportional split) --
    guessing here would silently corrupt financial data.

Usage:
    from kap_delta_engine import KAPDeltaEngine, baseline_period_to_delta_start
    from datetime import date

    baseline = {"SVGYO": 10000.0}  # from KAPPdfParser.parse_file(...) for 2026/06
    start = baseline_period_to_delta_start(2026, 6)  # -> 2026-07-01 (day AFTER PDF month ends)
    with KAPDeltaEngine(fon_kodu="TLY") as engine:
        updated, resolved, unresolved = engine.apply_delta(
            baseline, start_date=start.isoformat(), end_date=date.today().isoformat()
        )
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape as html_escape
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from kap_downloader import KAPPdfDownloader, _request_with_retry
from kap_pdf_parser import KAPPdfParser


def baseline_period_end_date(year: int, donem: int) -> date:
    """Last calendar day of a monthly KAP PDF baseline period
    (e.g. 2026/07 -> 2026-07-31), via `calendar.monthrange`."""
    last_day = calendar.monthrange(year, donem)[1]
    return date(year, donem, last_day)


def baseline_period_to_delta_start(year: int, donem: int) -> date:
    """Returns the first calendar day AFTER a monthly KAP PDF baseline
    period ends -- the earliest date whose buy/sell disclosures may be
    layered on top of that PDF without double-counting.

    A "Portföy Dağılım Raporu" for period `(year, donem)` (e.g. 2026/07)
    already reflects the fund's holdings as of the LAST day of that month
    (31.07.2026). Applying any intra-month trade whose İşlem Tarihi falls
    on or before that last day would re-apply lots the PDF already
    contains. So the delta window must open on day `last_day + 1`
    (01.08.2026 here), computed via `calendar.monthrange` so Feb/30-day
    months are handled correctly rather than hard-coding day 31.
    """
    return baseline_period_end_date(year, donem) + timedelta(days=1)


def _format_tr_date(value: date) -> str:
    """Formats a `date` as DD.MM.YYYY for audit-trail log lines."""
    return value.strftime("%d.%m.%Y")


def _format_tr_number(value: float, decimals: int = 2) -> str:
    """Turkish thousands/decimal separators for audit-trail numbers."""
    us = f"{value:,.{decimals}f}"
    return us.replace(",", "\u0001").replace(".", ",").replace("\u0001", ".")


def _log_step(execution_logs: Optional[List[Dict[str, str]]], message: str) -> None:
    """Appends one mechanical audit-trail entry to `execution_logs` for the
    HTML "Adım Adım Hesaplama ve Çalışma Günlüğü" section.

    Prefer concrete evidence (PDF filename, Belge No / disclosureIndex,
    ISO date windows, formula inputs/outputs) over PR-style summaries.
    Console `print(...)` stays the noisy technical stream; this list is
    the durable, parameter-level trail rendered in the HTML report.

    Never raises; no-op if `execution_logs` is None.
    """
    if execution_logs is None:
        return
    execution_logs.append({"time": datetime.now().strftime("%H:%M:%S"), "message": message})


@dataclass
class DeltaDisclosureRecord:
    """One "Pay Alim Satim Bildirimi" entry from KAP's general
    member-disclosure query API, trimmed down to only the fields this
    module needs to go fetch and parse its detail page."""

    disclosure_index: int
    publish_date: str  # raw "DD.MM.YYYY HH:MM:SS" string, as returned by KAP


@dataclass
class ResolvedDelta:
    """One unambiguous, single-fund "Pay Alim Satim Bildirimi" transaction
    that was successfully applied to the baseline holdings by
    `apply_delta` -- kept separately from the raw `ParsedTransaction` so
    reporting code (see `export_delta_report_to_html`) doesn't need to
    re-derive the applied ticker/sign from the underlying disclosure.

    Unit note (assumption, not independently verified): `lot_amount` is
    KAP's "Net Nominal Tutar (TL)" figure taken as-is. This equals the
    traded share/lot count only if the traded security's par value is
    1 TL/share, which is the common (but not universal) default for
    BIST-listed instruments. Cross-check against the next monthly PDF
    report before trusting this figure for a non-standard par value
    security."""

    disclosure_index: int
    transaction_date: str  # "DD/MM/YYYY"
    ticker: str
    lot_amount: float  # unsigned magnitude of the applied change
    direction: str  # "ALIM" | "SATIM" | "DEGISIM YOK"


@dataclass
class ProportionalResolution:
    """One multi-fund "Pay Alim Satim Bildirimi" transaction (originally
    an `unresolved` `ParsedTransaction`) that `KAPDeltaEngine.
    resolve_multi_fund_deltas` was able to attribute a share of to the
    target fund, by weighting the disclosure's aggregate `net_nominal_tl`
    proportionally to each co-filing fund's TEFAS "Aktif Guc" (active
    purchasing power) ON THE TRANSACTION DATE (see that method's
    docstring for the full "Zaman Cizelgesi" reasoning).

    This is an ESTIMATE, not a KAP-confirmed per-fund figure -- KAP itself
    never publishes one (see the module docstring's "UNRESOLVED DATA
    LIMITATION" note). Kept distinct from `ResolvedDelta` (which only
    covers unambiguous, KAP-confirmed single-fund transactions) so
    reporting code can clearly label estimated vs. confirmed figures.
    """

    disclosure_index: int
    transaction_date: str  # "DD/MM/YYYY"
    ticker: str
    related_funds: List[str]
    pool_total_tl: float  # sum of every related fund's Aktif Guc that day
    target_power_tl: float  # the target fund's own Aktif Guc that day
    target_weight_pct: float  # target_power_tl / pool_total_tl * 100
    net_lot_total: float  # the disclosure's own aggregate net_nominal_tl
    estimated_lot: float  # net_lot_total * (target_weight_pct / 100), signed
    direction: str  # "ALIM" | "SATIM" | "DEGISIM YOK"


@dataclass
class ParsedTransaction:
    """One row of a "Pay Alim Satim Bildirimi" disclosure's transaction
    table, as parsed from its detail page HTML -- an AGGREGATE figure for
    the filing portfolio management company's combined position across
    every fund listed in `related_funds`, not a single fund's individual
    trade (see module docstring)."""

    disclosure_index: int
    transaction_date: str  # "DD/MM/YYYY", as rendered by KAP
    traded_companies: List[str] = field(default_factory=list)  # "Ilgili Sirketler"
    related_funds: List[str] = field(default_factory=list)  # "Ilgili Fonlar"
    buy_nominal_tl: Optional[float] = None
    sell_nominal_tl: Optional[float] = None
    net_nominal_tl: Optional[float] = None
    start_of_day_nominal_tl: Optional[float] = None
    end_of_day_nominal_tl: Optional[float] = None


class KAPDeltaEngine:
    """Fetches and parses KAP's "Pay Alim Satim Bildirimi" disclosures for
    a fund's portfolio management company, to see what it bought/sold
    in between two monthly "Portfoy Dagilim Raporu" publications.

    Pipeline:

    1. `_fetch_delta_disclosures()` calls KAP's general member-disclosure
       query endpoint (`/tr/api/disclosure/members/byCriteria`), scoped to
       the fund's manager via `MANAGER_MKK_MEMBER_OID`, and keeps only
       entries whose `subject` is exactly `"Pay Alim Satim Bildirimi"` and
       whose `relatedStocks` field mentions the target fund code.
    2. `_parse_html_table()` fetches that disclosure's public detail page
       (`/tr/Bildirim/{disclosureIndex}`, same page `KAPPdfDownloader`
       reads for the monthly report's PDF link) and parses its inline
       "tbl_oda-10400_Shares-Transaction-Notification" HTML table with
       BeautifulSoup: a GWT-rendered taxonomy table with every field
       duplicated in Turkish then English, keyed off Turkish label text
       rather than fixed cell indices (which shift depending on which
       optional flag rows a given disclosure includes).
    3. `apply_delta()` only auto-merges disclosures that name the target
       fund exclusively; every disclosure naming multiple funds is parsed
       and returned in full but kept out of the merged baseline (see the
       "UNRESOLVED DATA LIMITATION" note in the module docstring).
    4. `discover_related_funds()` -- the "Kesif" (Discovery) stage --
       scans (3)'s multi-fund `unresolved` list and extracts every unique
       fund code these disclosures name alongside the target fund, into
       a single deduplicated array (e.g. `["TLY", "TMV", "T3B", "DOH",
       "THF"]`). This is how the module dynamically discovers which other
       funds share the target fund's portfolio manager, without hardcoding
       them, for future per-fund KAP PDF downloads / TEFAS data fetches.
    """

    BASE_URL = KAPPdfDownloader.BASE_URL
    DETAIL_PAGE = KAPPdfDownloader.DETAIL_PAGE
    USER_AGENT = KAPPdfDownloader.USER_AGENT

    BYCRITERIA_ENDPOINT = "/tr/api/disclosure/members/byCriteria"
    TARGET_SUBJECT = "Pay Alım Satım Bildirimi"

    # KAP's general disclosure query is scoped by `mkkMemberOid`, which
    # for a fund is its *portfolio management company*'s member OID, not
    # the fund's own `company_oid`/`member_oid` pair used by
    # KAPPdfDownloader's FILTERYFBF endpoint (those are a different ID
    # space). Captured from a live TLY monthly-report disclosure's own
    # `disclosureBasic.mkkMemberOid` field (verified 2026-07-28).
    MANAGER_MKK_MEMBER_OID: Dict[str, str] = {
        "TLY": "5553acdacf15471ba80c28eb45cdd9e7",  # Tera Portfoy Yonetimi A.S.
    }

    def __init__(
        self,
        fon_kodu: str = "TLY",
        request_delay: float = 0.5,
        timeout: int = 30,
        execution_logs: Optional[List[Dict[str, str]]] = None,
    ):
        fon_kodu = fon_kodu.strip().upper()
        if fon_kodu not in self.MANAGER_MKK_MEMBER_OID:
            supported = ", ".join(self.MANAGER_MKK_MEMBER_OID)
            raise ValueError(
                f"'{fon_kodu}' fonu icin portfoy yonetim sirketinin KAP mkkMemberOid bilgisi "
                f"tanimli degil. Desteklenen fonlar: {supported}. Yeni bir fon eklemek icin "
                "MANAGER_MKK_MEMBER_OID sozlugune kayit ekleyin."
            )

        self.fon_kodu = fon_kodu
        self.manager_mkk_member_oid = self.MANAGER_MKK_MEMBER_OID[fon_kodu]
        self.request_delay = request_delay
        self.timeout = timeout
        self.session = self._build_session()
        self.discovered_related_funds: List[str] = []  # populated by discover_related_funds()

        # "Execution Trace" narrative log (see `_log_step`'s docstring) --
        # shared, mutable list: pass one in (e.g. `execution_logs=[]`
        # created in `__main__`, before this engine even exists) so every
        # step across THIS instance's methods AND the module-level
        # `collect_global_baseline`/`build_tefas_power_matrix` functions
        # (which accept the same parameter) narrate into one single,
        # chronologically-ordered story. Defaults to a fresh private list
        # if the caller doesn't supply one, so existing call sites that
        # never pass `execution_logs` behave exactly as before.
        self.execution_logs: List[Dict[str, str]] = execution_logs if execution_logs is not None else []

    # --- Context manager support --------------------------------------------

    def __enter__(self) -> "KAPDeltaEngine":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Closes the underlying HTTP session/connection pool."""
        self.session.close()

    # --- Session setup -------------------------------------------------------

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": f"{self.BASE_URL}/tr/bildirim-sorgu",
                "Origin": self.BASE_URL,
                "Connection": "keep-alive",
            }
        )
        return session

    # --- Stage 1: fetch and filter the disclosure list ------------------------

    def _fetch_delta_disclosures(self, start_date: str, end_date: str) -> List[DeltaDisclosureRecord]:
        """Calls KAP's general member-disclosure query endpoint
        (`POST /tr/api/disclosure/members/byCriteria`), scoped to this
        fund's portfolio management company, and keeps only the entries
        whose `subject` is exactly `TARGET_SUBJECT` and whose
        `relatedStocks` field mentions this fund's code -- confirmed live
        (2026-07-28) to return real "Pay Alim Satim Bildirimi" entries for
        TLY (113 in the last 12 months), unlike the FILTERYFBF endpoint
        used by the original (incorrect) version of this module.

        `start_date`/`end_date` are inclusive "YYYY-MM-DD" strings and are
        sent directly as the API's own `fromDate`/`toDate` fields (this
        endpoint, unlike FILTERYFBF, takes an explicit date range).
        """
        url = self.BASE_URL + self.BYCRITERIA_ENDPOINT
        body = {
            "fromDate": start_date,
            "toDate": end_date,
            "mkkMemberOidList": [self.manager_mkk_member_oid],
            "subjectList": [],
        }

        try:
            response = _request_with_retry(
                self.session, "POST", url, json=body, timeout=self.timeout, execution_logs=self.execution_logs
            )
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] [{self.fon_kodu}] Bildirim listesi alinamadi: {exc}")
            return []

        try:
            payload = response.json()
        except ValueError:
            print(f"[HATA] [{self.fon_kodu}] Bildirim listesi JSON olarak ayristirilamadi.")
            return []

        if not isinstance(payload, list):
            print(f"[HATA] [{self.fon_kodu}] Beklenmeyen API yanit formati (liste degil).")
            return []

        matches: List[DeltaDisclosureRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("subject") != self.TARGET_SUBJECT:
                continue

            related_stocks_raw = item.get("relatedStocks") or ""
            related_codes = [code.strip().upper() for code in related_stocks_raw.split(",") if code.strip()]
            if self.fon_kodu not in related_codes:
                continue

            disclosure_index = item.get("disclosureIndex")
            publish_date_raw = item.get("publishDate") or ""
            if disclosure_index is None or not publish_date_raw:
                continue

            matches.append(DeltaDisclosureRecord(disclosure_index=disclosure_index, publish_date=publish_date_raw))

        matches.sort(key=lambda r: r.publish_date)
        print(
            f"[BILGI] [{self.fon_kodu}] {start_date} -> {end_date} araliginda {len(matches)} adet "
            f"'{self.TARGET_SUBJECT}' bildirimi bulundu (mkkMemberOid={self.manager_mkk_member_oid})."
        )
        return matches

    # --- Stage 2: parse the inline HTML transaction table ---------------------

    def _parse_html_table(self, disclosure_index: int) -> List[ParsedTransaction]:
        """Fetches a "Pay Alim Satim Bildirimi" disclosure's public detail
        page and parses its `tbl_oda-10400_Shares-Transaction-Notification`
        table into `ParsedTransaction` rows (usually exactly one row, but a
        disclosure can carry more than one date's transaction).

        Never raises: returns an empty list (with a console warning) if
        the page can't be fetched or the expected table isn't found.
        """
        url = self.BASE_URL + self.DETAIL_PAGE.format(disclosure_index=disclosure_index)
        try:
            response = _request_with_retry(
                self.session,
                "GET",
                url,
                timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
                execution_logs=self.execution_logs,
            )
        except requests.exceptions.RequestException as exc:
            print(f"[HATA] [{self.fon_kodu}] Detay sayfasi alinamadi (disclosureIndex={disclosure_index}): {exc}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", class_=lambda c: c and "tbl_oda" in c)
        if table is None:
            print(
                f"[UYARI] [{self.fon_kodu}] Beklenen 'tbl_oda' islem tablosu bulunamadi "
                f"(disclosureIndex={disclosure_index})."
            )
            return []

        rows = table.find_all("tr")
        traded_companies = self._extract_bracketed_list(rows, "İlgili Şirketler")
        related_funds = self._extract_bracketed_list(rows, "İlgili Fonlar")

        header_row_idx = self._find_row_index(rows, "İşlem Tarihi")
        if header_row_idx is None or header_row_idx + 1 >= len(rows):
            print(
                f"[UYARI] [{self.fon_kodu}] Islem tarihi/nominal tutar basligi bulunamadi "
                f"(disclosureIndex={disclosure_index})."
            )
            return []

        header_cells = [cell.get_text(" ", strip=True) for cell in rows[header_row_idx].find_all(["td", "th"])]
        column_count = self._turkish_column_count(header_cells)
        header_tr = [self._normalize(text) for text in header_cells[:column_count]]

        col = {
            "date": self._find_column_index(header_tr, ("işlem tarihi",)),
            "buy": self._find_column_index(header_tr, ("alım işlemine konu",)),
            "sell": self._find_column_index(header_tr, ("satım işlemine konu",)),
            "net": self._find_column_index(header_tr, ("net nominal",)),
            "day_start": self._find_column_index(header_tr, ("gün başı nominal",)),
            "day_end": self._find_column_index(header_tr, ("gün sonu nominal",)),
        }

        transactions: List[ParsedTransaction] = []
        for row in rows[header_row_idx + 1 :]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
            if len(cells) < column_count:
                continue
            cells = cells[:column_count]

            date_value = cells[col["date"]].strip() if col["date"] is not None else ""
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", date_value):
                # Not a genuine data row (e.g. a stray spacer/footer row).
                continue

            transactions.append(
                ParsedTransaction(
                    disclosure_index=disclosure_index,
                    transaction_date=date_value,
                    traded_companies=traded_companies,
                    related_funds=related_funds,
                    buy_nominal_tl=self._cell_to_float(cells, col["buy"]),
                    sell_nominal_tl=self._cell_to_float(cells, col["sell"]),
                    net_nominal_tl=self._cell_to_float(cells, col["net"]),
                    start_of_day_nominal_tl=self._cell_to_float(cells, col["day_start"]),
                    end_of_day_nominal_tl=self._cell_to_float(cells, col["day_end"]),
                )
            )

        if not transactions:
            print(
                f"[UYARI] [{self.fon_kodu}] Baslik bulundu ama veri satiri parse edilemedi "
                f"(disclosureIndex={disclosure_index})."
            )

        return transactions

    # --- HTML parsing helpers --------------------------------------------------

    @staticmethod
    def _find_row_index(rows: List, label_startswith: str) -> Optional[int]:
        for index, row in enumerate(rows):
            first_cell = row.find(["td", "th"])
            if first_cell is not None and first_cell.get_text(strip=True).startswith(label_startswith):
                return index
        return None

    def _extract_bracketed_list(self, rows: List, label_startswith: str) -> List[str]:
        """Finds the row whose first cell is `label_startswith` (e.g.
        "İlgili Şirketler") and extracts the "[A, B, C]"-formatted code
        list from its value cell."""
        row_idx = self._find_row_index(rows, label_startswith)
        if row_idx is None:
            return []
        cells = rows[row_idx].find_all(["td", "th"])
        if len(cells) < 2:
            return []
        value_text = cells[-1].get_text(" ", strip=True)
        match = re.search(r"\[([^\]]*)\]", value_text)
        if not match:
            return []
        return [code.strip().upper() for code in match.group(1).split(",") if code.strip()]

    @staticmethod
    def _turkish_column_count(header_cells: List[str]) -> int:
        """The transaction table's header (and data) rows duplicate every
        column twice -- Turkish label followed by its English
        translation, in that order -- with no separator between the two
        halves. This finds where the English half starts (first cell
        that case-insensitively equals a known English header, e.g.
        "Transaction Date") to determine how many of the leading cells
        are the real (Turkish) columns; falls back to an even split if
        that marker isn't found."""
        for index, text in enumerate(header_cells):
            if text.strip().lower() == "transaction date" and index > 0:
                return index
        return max(len(header_cells) // 2, 1)

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercases Turkish header text safely. Python's default
        `str.lower()` turns the Turkish dotted capital "İ" (U+0130) into
        "i" + a COMBINING DOT ABOVE (U+0307) rather than a plain ASCII
        "i" (a well-known Turkish-locale casing pitfall), which silently
        breaks substring matching against plain-ASCII keyword literals
        like "işlem tarihi". Mapping "İ"/"I" to a plain "i" before
        lowering avoids that."""
        return text.strip().replace("İ", "i").replace("I", "i").lower()

    @staticmethod
    def _find_column_index(headers: List[str], keywords: Tuple[str, ...]) -> Optional[int]:
        for index, header in enumerate(headers):
            if any(keyword in header for keyword in keywords):
                return index
        return None

    def _cell_to_float(self, cells: List[str], index: Optional[int]) -> Optional[float]:
        if index is None or index >= len(cells):
            return None
        raw = cells[index].strip()
        cleaned = raw.replace("%", "").strip()
        if not cleaned:
            return None
        try:
            return self._turkish_str_to_float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _turkish_str_to_float(value: str) -> float:
        """Converts a Turkish-formatted number string (e.g. "3.305.530"
        or "4,987524") to a float, same convention as
        `kap_pdf_parser.KAPPdfParser._turkish_str_to_float`."""
        cleaned = value.strip().replace(".", "").replace(",", ".")
        return float(cleaned)

    # --- Discovery phase: find funds that co-file with the target fund --------

    def discover_related_funds(self, unresolved: List[ParsedTransaction]) -> List[str]:
        """"Kesif" (Discovery) asamasi: `apply_delta()`'nin dondurdugu
        `unresolved` (cok-fonlu, cozulemeyen) islemlerin "Ilgili Fonlar"
        listelerini tarayarak, KAP'ta bu fonun (self.fon_kodu) portfoy
        yonetim sirketiyle ORTAK bildirime konu olan butun fon kodlarini
        tek, benzersiz (duplicate'siz) bir diziye toplar.

        Her `ParsedTransaction.related_funds` zaten `_extract_bracketed_list`
        tarafindan ayristirilmis temiz bir `List[str]`'dir, ama bu metot
        `_clean_fund_codes` uzerinden GECER YINE de savunmaci davranir:
        ham "DOH, T3B, TLY, TMV, THF" veya "[DOH, T3B, TLY]" formatinda
        serbest metin de girdi olarak kabul edilir ve parantez/tirnak/
        bosluk gibi kalinti karakterlerden arindirilir -- boylece ileride
        bu metodun farkli bir veri kaynagindan (ornegin ham HTML/log metni)
        beslenmesi gerekirse davranis degismez.

        Hedef fon (`self.fon_kodu`) diziye her zaman ilk eleman olarak
        eklenir (referans noktasi); geri kalan kodlar ilk gorulme sirasina
        gore, tekrarsiz sekilde eklenir. Sonuc hem return edilir hem de
        `self.discovered_related_funds` uzerinde saklanir, boylece bir
        sonraki adimlar (gelecekteki KAP PDF indirmeleri / bu fonlar icin
        TEFAS veri cekme islemleri) bu diziyi dogrudan tuketebilir.
        """
        discovered: List[str] = [self.fon_kodu]
        seen = {self.fon_kodu}

        for txn in unresolved:
            for code in self._clean_fund_codes(txn.related_funds):
                if code not in seen:
                    seen.add(code)
                    discovered.append(code)

        self.discovered_related_funds = discovered
        print(f"[KEŞİF] [{self.fon_kodu}] Keşif Aşaması Tamamlandı. Hedef Fonlar Dizisi: {discovered}")

        other_funds = [code for code in discovered if code != self.fon_kodu]
        _log_step(
            self.execution_logs,
            f"Keşif (discover_related_funds): {len(unresolved)} unresolved bildirimin "
            f"'İlgili Fonlar' alanları tarandı. Hedef={self.fon_kodu}. "
            f"Benzersiz fon kodları={discovered}. "
            f"Kardeş fon sayısı={len(other_funds)} ({', '.join(other_funds) or 'yok'}). "
            "Bu dizi global baseline + TEFAS güç matrisi girdilerine aktarılacak.",
        )
        return discovered

    @staticmethod
    def _clean_fund_codes(value) -> List[str]:
        """Yuksek hassasiyetli fon kodu ayristirici: girdi olarak hem
        zaten ayristirilmis bir `List[str]` hem de "DOH, T3B, TLY" /
        "[DOH, T3B, TLY]" formatinda ham, sinirlandirilmis (delimited)
        metin kabul eder. Her iki durumda da her token'in bastaki/sondaki
        bosluklari, koseli/normal parantezleri ve tirnak isaretlerini
        temizler, buyuk harfe cevirir, ve sonucta gecerli bir fon kodu
        gorunumune uymayan (bos, tek karakterlik, noktalama iceren vb.)
        kalinti degerleri sessizce eler -- boylece cikan dizide asla
        trailing space veya bozuk karakter kalmaz."""
        if not value:
            return []
        if isinstance(value, str):
            tokens = re.split(r"[,\s]+", value.strip("[]() \t\n\r"))
        else:
            tokens = list(value)

        cleaned: List[str] = []
        for token in tokens:
            code = str(token).strip().strip("[]()'\"").upper()
            if re.fullmatch(r"[A-ZÇĞİÖŞÜ0-9]{2,10}", code):
                cleaned.append(code)
        return cleaned

    # --- Public entry point ---------------------------------------------------

    def apply_delta(
        self, baseline_data: Dict[str, float], start_date: str, end_date: str
    ) -> Tuple[Dict[str, float], List[ResolvedDelta], List[ParsedTransaction]]:
        """Fetches every "Pay Alim Satim Bildirimi" disclosure published
        for this fund's manager between `start_date` and `end_date`
        (inclusive, "YYYY-MM-DD"), and returns a 3-tuple of:

        1. `updated_data` -- a NEW dict (baseline_data is never mutated)
           with the net nominal TL amount applied on top of
           `baseline_data[traded_company]`, but ONLY for disclosures that
           name this fund exclusively (`related_funds == [self.fon_kodu]`)
           -- an unambiguous case where the aggregate figure IS this
           fund's own trade.
        2. `resolved` -- one `ResolvedDelta` per ticker actually merged
           into `updated_data` above (same disclosures as (1), just
           reshaped for reporting).
        3. `unresolved` -- every `ParsedTransaction` that named more than
           one fund, in full, UNAPPLIED. See the "UNRESOLVED DATA
           LIMITATION" note in the module docstring for why these are
           deliberately not merged into `updated_data`.

        Double-counting guard: KAP's `byCriteria` endpoint filters by
        *publish* date (`fromDate`/`toDate`), not by the disclosure's
        own İşlem Tarihi. A notice published after `start_date` can still
        carry a transaction date that falls inside the baseline PDF
        month (already baked into that PDF). Any parsed row whose
        İşlem Tarihi is strictly before `start_date` is therefore
        skipped -- never applied to baseline, never added to
        `resolved`/`unresolved` -- so the monthly PDF and the daily
        deltas never overlap. Callers should set `start_date` via
        `baseline_period_to_delta_start(year, donem)` (the day AFTER the
        PDF month's last calendar day).
        """
        print(f"[SISTEM] [{self.fon_kodu}] Delta motoru calisiyor: {start_date} -> {end_date}...")
        try:
            baseline_end = date.fromisoformat(start_date) - timedelta(days=1)
            baseline_end_label = _format_tr_date(baseline_end)
        except ValueError:
            baseline_end_label = start_date

        _log_step(
            self.execution_logs,
            f"KAP API'ye delta işlemleri için POST /tr/api/disclosure/members/byCriteria "
            f"isteği atıldı: fromDate={start_date}, toDate={end_date}, "
            f"mkkMemberOid={self.manager_mkk_member_oid}, subject='{self.TARGET_SUBJECT}'. "
            f"Baseline geçerlilik sonu={baseline_end_label}; bu tarihten önceki/içindeki "
            "İşlem Tarihi satırları double-counting koruması ile reddedilecek.",
        )

        disclosures = self._fetch_delta_disclosures(start_date, end_date)

        _log_step(
            self.execution_logs,
            f"KAP API yanıtı: [{start_date} – {end_date}] aralığında toplam "
            f"{len(disclosures)} adet '{self.TARGET_SUBJECT}' bildirimi döndü "
            f"(fon filtresi={self.fon_kodu}).",
        )

        updated_data = dict(baseline_data)
        resolved: List[ResolvedDelta] = []
        unresolved: List[ParsedTransaction] = []
        skipped_pre_baseline = 0

        if not disclosures:
            print(f"[SISTEM] [{self.fon_kodu}] Uygulanacak bildirim bulunamadi; baseline degistirilmedi.")
            _log_step(
                self.execution_logs,
                f"Adım 1 sonucu: [{start_date} – {end_date}] aralığında 0 bildirim; "
                f"baseline holdings ({len(baseline_data)} kod) değiştirilmeden korundu.",
            )
            return updated_data, resolved, unresolved

        for index, record in enumerate(disclosures):
            print(f"  -> {record.publish_date}  disclosureIndex={record.disclosure_index}")
            try:
                transactions = self._parse_html_table(record.disclosure_index)
            except Exception as exc:  # noqa: BLE001 - one bad disclosure must never abort the run
                print(
                    f"[KRITIK HATA] [{self.fon_kodu}] disclosureIndex={record.disclosure_index} "
                    f"islenirken beklenmeyen hata: {exc}"
                )
                _log_step(
                    self.execution_logs,
                    f"Belge No: {record.disclosure_index} (yayın={record.publish_date}) "
                    f"HTML parse sırasında hata verdi, atlandı: {exc}",
                )
                transactions = []

            for txn in transactions:
                iso_txn_date = self._transaction_date_to_iso(txn.transaction_date)
                if iso_txn_date is not None and iso_txn_date < start_date:
                    # Publish date is in-window, but the trade itself
                    # predates (or falls inside) the baseline PDF month --
                    # already reflected in that PDF. Skip to avoid
                    # double-counting.
                    skipped_pre_baseline += 1
                    print(
                        f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index} "
                        f"islem tarihi {txn.transaction_date} baseline baslangicindan "
                        f"({start_date}) once; cift sayim riski nedeniyle atlaniyor."
                    )
                    _log_step(
                        self.execution_logs,
                        f"{txn.transaction_date} tarihli [Belge No: {txn.disclosure_index}] "
                        f"işlemi (hisse={txn.traded_companies or ['?']}, "
                        f"net={txn.net_nominal_tl}, fonlar={txn.related_funds}), "
                        f"taban tarihinden ({baseline_end_label}) önce/içinde olduğu için "
                        "double-counting koruması gereği reddedildi.",
                    )
                    continue

                if txn.related_funds == [self.fon_kodu] and txn.net_nominal_tl is not None:
                    direction = (
                        "ALIM" if txn.net_nominal_tl > 0 else "SATIM" if txn.net_nominal_tl < 0 else "DEGISIM YOK"
                    )
                    for company in txn.traded_companies or ["BILINMEYEN"]:
                        prev_lot = updated_data.get(company, 0.0)
                        updated_data[company] = prev_lot + txn.net_nominal_tl
                        resolved.append(
                            ResolvedDelta(
                                disclosure_index=txn.disclosure_index,
                                transaction_date=txn.transaction_date,
                                ticker=company,
                                lot_amount=abs(txn.net_nominal_tl),
                                direction=direction,
                            )
                        )
                        _log_step(
                            self.execution_logs,
                            f"KESİNLEŞEN DELTA uygulandı: Belge No={txn.disclosure_index}, "
                            f"İşlem Tarihi={txn.transaction_date}, Hisse={company}, "
                            f"Yön={direction}, Lot={_format_tr_number(abs(txn.net_nominal_tl))}, "
                            f"Önceki={_format_tr_number(prev_lot)} -> "
                            f"Sonraki={_format_tr_number(updated_data[company])} "
                            f"(related_funds={txn.related_funds}).",
                        )
                else:
                    unresolved.append(txn)
                    _log_step(
                        self.execution_logs,
                        f"ÇOKLU-FON / ÇÖZÜLEMEYEN olarak sınıflandırıldı: Belge No={txn.disclosure_index}, "
                        f"İşlem Tarihi={txn.transaction_date}, Hisse={txn.traded_companies}, "
                        f"İlgili Fonlar={txn.related_funds}, Net Lot={txn.net_nominal_tl}. "
                        f"KAP fon bazlı kırılım vermediği için baseline'a uygulanmadı "
                        f"(oransal dağıtıma aday).",
                    )

            if index != len(disclosures) - 1:
                time.sleep(self.request_delay)

        print(
            f"[SISTEM] [{self.fon_kodu}] Tamamlandi: {len(disclosures)} bildirim islendi -> "
            f"{len(resolved)} tek-fonlu (uygulandi), {len(unresolved)} cok-fonlu/belirsiz (uygulanmadi)"
            + (f", {skipped_pre_baseline} baseline-oncesi islem atlaniyor" if skipped_pre_baseline else "")
            + "."
        )

        _log_step(
            self.execution_logs,
            f"Adım 1 özet sayaçları: API'den {len(disclosures)} bildirim; "
            f"kesinleşen (uygulanan)={len(resolved)}; çoklu-fon/çözülemeyen={len(unresolved)}; "
            f"double-counting reddi={skipped_pre_baseline}; "
            f"baseline geçerlilik sonu={baseline_end_label}; delta penceresi=[{start_date} – {end_date}].",
        )
        return updated_data, resolved, unresolved

    # --- Step 4: proportional resolution of multi-fund transactions -----------

    @staticmethod
    def _transaction_date_to_iso(transaction_date: str) -> Optional[str]:
        """Converts a KAP transaction date string ("DD/MM/YYYY", as parsed
        by `_parse_html_table`) into "YYYY-MM-DD" so it can be looked up
        directly in a `tefas_power_matrix` (see `build_tefas_power_matrix`,
        which keys its per-fund dict by that same ISO format). Returns
        None (never raises) for anything that isn't a clean DD/MM/YYYY
        string.
        """
        if not transaction_date or not isinstance(transaction_date, str):
            return None
        parts = transaction_date.split("/")
        if len(parts) != 3:
            return None
        day, month, year = parts
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            return None

    def resolve_multi_fund_deltas(
        self,
        unresolved: List[ParsedTransaction],
        tefas_power_matrix: Dict[str, Dict[str, float]],
        updated_data: Dict[str, float],
    ) -> Tuple[Dict[str, float], List[ProportionalResolution]]:
        """"Zaman Cizelgesi" (Chronological Ledger) asamasi: `apply_delta()`
        tarafindan KAP verisinde fon bazli kirilimi olmadigi icin
        (bkz. modul docstring'indeki "UNRESOLVED DATA LIMITATION")
        uygulanamayan `unresolved` (cok-fonlu) islemleri, KAP'in hic
        yayinlamadigi bu kirilimi TAHMIN ETMEK icin, o gunku TEFAS "Aktif
        Guc" (bkz. `build_tefas_power_matrix`) degerleriyle ORANTILI olarak
        dagitir.

        Her `ParsedTransaction` icin mantik:

        A) Zamanlama: KAP'in kendi "Bildirim Tarihi" (disclosure'un
           yayinlandigi an, islemden GUNLER SONRA olabilir) DEGIL,
           bildirimin kendi icindeki "Islem Tarihi" (`transaction_date`)
           kullanilir. O gunun TEFAS verisi (`Tarih`), bir onceki gecenin
           kapanisini -- yani ilgili fonun islem sabahindaki gercek
           "muhimmatini" -- yansitir; bu yuzden dogru referans budur.
        B) Havuz: Islemin `related_funds` listesindeki HER fonun o gunku
           Aktif Gucu toplanarak "Toplam Guc Havuzu" olusturulur.
        C) Hesaplama: Hedef fonun (self.fon_kodu) bu havuzdaki agirligi
           `hedef_guc / toplam_havuz` olarak bulunur; islemin toplam
           `net_nominal_tl` (KAP'in tek, birlesik TL rakami) degeri bu
           agirlikla carpilarak hedef fona ait TAHMINI net lot bulunur.
        D) Kayit: Tahmini miktar, girdi olarak alinan `updated_data`
           sozlugunun (baseline + apply_delta'nin tek-fonlu `resolved`
           sonuclari) UZERINE eklenir (input mutate edilmez, yeni bir dict
           dondurulur) ve raporlama icin ayrica bir `ProportionalResolution`
           listesi olarak da dondurulur.

        Hicbir zaman crash etmez / ZeroDivisionError riski tasimaz: bir
        islemin `related_funds` listesindeki fonlardan HERHANGI BIRI icin o
        gune ait TEFAS Aktif Gucu bulunamazsa (eksik veri, hafta sonu,
        henuz taranmamis fon vb.), ya da bulunan gucler toplaninca "Toplam
        Havuz" 0 (veya negatif) cikarsa, o TEK islem acikca Turkce bir
        "[UYARI]" log'u ile atlanir ve diger islemlere devam edilir --
        eksik bir fon icin sessizce 0 varsayip havuzu kucultmek, diger
        fonlarin payini yapay olarak sisirir, bu yuzden boyle bir islem
        HICBIR SEKILDE kismi/tahmini olarak dagitilmaz.
        """
        result = dict(updated_data)
        proportionally_resolved: List[ProportionalResolution] = []
        skipped_proportional = 0

        _log_step(
            self.execution_logs,
            f"Oransal dağıtım başlıyor: girdi={len(unresolved)} unresolved bildirim. "
            f"Formül: Havuz_Payı = Aktif_Güç({self.fon_kodu}, İşlem_Tarihi) / "
            f"Σ Aktif_Güç(İlgili_Fonlar, İşlem_Tarihi); "
            f"Tahmini_Lot = Net_Lot × Havuz_Payı. "
            "Referans gün = İşlem Tarihi (Bildirim Tarihi değil).",
        )

        for txn in unresolved:
            iso_date = self._transaction_date_to_iso(txn.transaction_date)
            if not iso_date:
                print(
                    f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index}: "
                    f"gecersiz/eksik islem tarihi ({txn.transaction_date!r}), islem atlaniyor."
                )
                skipped_proportional += 1
                _log_step(
                    self.execution_logs,
                    f"ORANSAL RED: Belge No={txn.disclosure_index}, İşlem Tarihi={txn.transaction_date!r} "
                    "geçersiz/parse edilemedi; tahmin üretilmedi.",
                )
                continue

            related_funds = txn.related_funds or []
            if self.fon_kodu not in related_funds:
                print(
                    f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index}: hedef fon "
                    f"'İlgili Fonlar' listesinde yok ({related_funds}), islem atlaniyor."
                )
                skipped_proportional += 1
                _log_step(
                    self.execution_logs,
                    f"ORANSAL RED: Belge No={txn.disclosure_index}, hedef fon={self.fon_kodu} "
                    f"'İlgili Fonlar'={related_funds} listesinde yok; tahmin üretilmedi.",
                )
                continue

            fund_powers: Dict[str, float] = {}
            missing_funds: List[str] = []
            for fund_code in related_funds:
                power = (tefas_power_matrix.get(fund_code) or {}).get(iso_date)
                if power is None:
                    missing_funds.append(fund_code)
                else:
                    fund_powers[fund_code] = power

            if missing_funds:
                print(
                    f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index} ({iso_date}): "
                    f"su fon(lar) icin TEFAS Aktif Guc verisi yok: {missing_funds}; Toplam Guc Havuzu "
                    "guvenilir sekilde hesaplanamadigi icin islem atlaniyor."
                )
                skipped_proportional += 1
                _log_step(
                    self.execution_logs,
                    f"ORANSAL RED: Belge No={txn.disclosure_index}, İşlem Tarihi={txn.transaction_date} "
                    f"(ISO={iso_date}). TEFAS Aktif Güç eksik fonlar={missing_funds}; "
                    "havuz kısmi hesaplanamayacağı için tahmin üretilmedi.",
                )
                continue

            pool_total = sum(fund_powers.values())
            if pool_total <= 0:
                print(
                    f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index} ({iso_date}): "
                    f"Toplam Guc Havuzu {pool_total:,.2f} TL (sifir/negatif) cikti, "
                    "ZeroDivisionError riski nedeniyle islem atlaniyor."
                )
                skipped_proportional += 1
                _log_step(
                    self.execution_logs,
                    f"ORANSAL RED: Belge No={txn.disclosure_index}, İşlem Tarihi={iso_date}, "
                    f"Toplam_Güç_Havuzu={_format_tr_number(pool_total)} TL (<=0); bölme yapılmadı.",
                )
                continue

            if txn.net_nominal_tl is None:
                print(
                    f"[UYARI] [{self.fon_kodu}] disclosureIndex={txn.disclosure_index} ({iso_date}): "
                    "net nominal tutar (net_nominal_tl) eksik, islem atlaniyor."
                )
                skipped_proportional += 1
                _log_step(
                    self.execution_logs,
                    f"ORANSAL RED: Belge No={txn.disclosure_index}, net_nominal_tl=None; tahmin üretilmedi.",
                )
                continue

            target_power = fund_powers[self.fon_kodu]
            target_weight = target_power / pool_total
            estimated_lot = txn.net_nominal_tl * target_weight
            direction = "ALIM" if estimated_lot > 0 else "SATIM" if estimated_lot < 0 else "DEGISIM YOK"
            powers_snapshot = ", ".join(
                f"{code}={_format_tr_number(power)} TL" for code, power in sorted(fund_powers.items())
            )

            for company in txn.traded_companies or ["BILINMEYEN"]:
                prev_lot = result.get(company, 0.0)
                result[company] = prev_lot + estimated_lot
                proportionally_resolved.append(
                    ProportionalResolution(
                        disclosure_index=txn.disclosure_index,
                        transaction_date=txn.transaction_date,
                        ticker=company,
                        related_funds=related_funds,
                        pool_total_tl=pool_total,
                        target_power_tl=target_power,
                        target_weight_pct=target_weight * 100.0,
                        net_lot_total=txn.net_nominal_tl,
                        estimated_lot=estimated_lot,
                        direction=direction,
                    )
                )
                _log_step(
                    self.execution_logs,
                    f"ORANSAL UYGULAMA: Belge No={txn.disclosure_index}, Hisse={company}, "
                    f"İşlem Tarihi={txn.transaction_date}, İlgili Fonlar={related_funds}, "
                    f"Aktif Güçler=[{powers_snapshot}], "
                    f"Havuz={_format_tr_number(pool_total)} TL, "
                    f"{self.fon_kodu}_Güç={_format_tr_number(target_power)} TL, "
                    f"Havuz_Payı=%{_format_tr_number(target_weight * 100.0)}, "
                    f"Net_Lot_Havuz={_format_tr_number(txn.net_nominal_tl)}, "
                    f"Tahmini_Lot({self.fon_kodu})={_format_tr_number(estimated_lot)} ({direction}), "
                    f"Önceki={_format_tr_number(prev_lot)} -> Sonraki={_format_tr_number(result[company])}.",
                )

        print(
            f"[SISTEM] [{self.fon_kodu}] Oransal Dagitim Tamamlandi: {len(unresolved)} cozulemeyen "
            f"islemden {len(proportionally_resolved)} kayit orantili olarak dagitildi."
        )
        _log_step(
            self.execution_logs,
            f"Oransal dağıtım özet sayaçları: unresolved_girdi={len(unresolved)}, "
            f"oransal_uygulanan_satır={len(proportionally_resolved)}, "
            f"reddedilen_bildirim={skipped_proportional}.",
        )
        return result, proportionally_resolved



# --- Step 2: collect a same-period baseline for every discovered fund -------


def collect_global_baseline(
    related_funds: List[str],
    days_back: int = 365,
    execution_logs: Optional[List[Dict[str, str]]] = None,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Tuple[int, int]]]:
    """Orchestrates `kap_downloader.KAPPdfDownloader` + `kap_pdf_parser.
    KAPPdfParser` across every fund code in `related_funds` (typically
    `KAPDeltaEngine.discover_related_funds()`'s output) to build a single
    "Golge Portfoy" global baseline snapshot:

        global_baseline = {
            "TLY": {"ALKLC": 731256.0, ...},
            "DOH": {"ALKLC": 150000.0, ...},
            ...
        }

    "Date Lag" fix (2026-07-31): each fund now uses `KAPPdfDownloader.
    download_latest_report()` to find and download ITS OWN most recently
    published monthly "Portfoy Dagilim Raporu" -- there is no shared,
    externally-supplied `baseline_period` anymore. Funds legitimately
    publish on different schedules (e.g. TLY's latest report might be
    June 2026 while DOH's is May 2026), so forcing every fund onto the
    SAME period (as this function used to do, taking it from whatever the
    caller passed in) silently misses months for any fund whose true
    latest report is newer than that shared period -- which is exactly
    what happened when the caller derived that period from a stale local
    `tly_pdfs/` folder instead of asking KAP directly. See the second
    return value below for exactly which period ended up being used per
    fund.

    Each fund's PDF is downloaded into its own `{fon_kodu_lower}_pdfs/`
    folder (e.g. `doh_pdfs/`, `t3b_pdfs/`), kept separate from `tly_pdfs/`
    and from each other; any older-month PDF already sitting there from a
    previous run is deleted first (see `download_latest_report`'s own
    `clean_old_files` behavior), so a stale file can never be parsed
    alongside the fresh one.

    `days_back` defaults to 365 and should not be raised past that: KAP's
    FILTERYFBF endpoint silently returns an empty list (HTTP 200, `[]`,
    no error) for any `days_back` value of 366 or higher -- verified live
    (2026-07-30) by probing 30/90/180/365/366/400, where 365 returned 13
    TLY disclosures and 366+ returned 0.

    Returns a 2-tuple `(global_baseline, baseline_periods)`, where
    `baseline_periods` maps each successfully baselined fund code to the
    `(year, donem)` tuple of the report that was actually used, e.g.
    `{"TLY": (2026, 6), "DOH": (2026, 5)}` -- funds can legitimately
    differ here, by design.

    Never raises and never aborts the loop: a fund missing from
    `KAPPdfDownloader.KNOWN_FUNDS` (no registered KAP OID -- true today
    for every discovered fund except TLY itself), a download failure, or
    an empty/missing parse result for its latest period are all caught,
    logged as "[UYARI]", and treated as "skip this fund, keep going" --
    the returned dicts simply omit that fund's entry rather than crashing
    or fabricating data for it.

    `execution_logs` (optional, default None): a shared list (see
    `_log_step`) that this function appends short narrative entries to --
    one per fund, plus a start/end summary -- for the HTML "Execution
    Trace" section. Purely additive: omitting it changes nothing about
    this function's return value or behavior.
    """
    global_baseline: Dict[str, Dict[str, float]] = {}
    baseline_periods: Dict[str, Tuple[int, int]] = {}

    print(
        f"\n[SISTEM] Global Baseline Toplama Basladi: {len(related_funds)} fon "
        "(her fon KENDI en guncel raporunu kullanacak)."
    )
    _log_step(
        execution_logs,
        f"Global baseline toplama başlıyor: hedef_fon_listesi={related_funds}, "
        f"fon_sayısı={len(related_funds)}, KAP days_back={days_back}, "
        "strateji=her fon için download_latest_report() (kendi en güncel PDF'i).",
    )

    for fon_kodu in related_funds:
        output_dir = f"{fon_kodu.lower()}_pdfs"

        try:
            with KAPPdfDownloader(fon_kodu=fon_kodu, output_dir=output_dir) as downloader:
                download_result = downloader.download_latest_report(days_back=days_back)
        except ValueError as exc:
            # Fund not registered in KNOWN_FUNDS (no KAP company/member OID
            # captured for it yet) -- an honest gap, not a bug; never guess
            # the OID, just skip and move on.
            print(f"[UYARI] [{fon_kodu}] KAP OID bilgisi tanimli degil, atlaniyor: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad fund must never abort the loop
            print(f"[UYARI] [{fon_kodu}] PDF indirme sirasinda beklenmeyen hata, atlaniyor: {exc}")
            continue

        if not download_result or download_result.get("status") != "success":
            print(f"[UYARI] [{fon_kodu}] KAP'ta en guncel rapor bulunamadi/indirilemedi, atlaniyor.")
            _log_step(
                execution_logs,
                f"Global baseline RED: fon={fon_kodu}, output_dir={output_dir}, "
                f"download_latest_report sonucu={download_result!r}; holdings eklenmedi.",
            )
            continue

        period_key = f"{download_result['year']}_{download_result['donem']:02d}"
        pdf_name = f"{fon_kodu}_{download_result['year']}_{download_result['donem']:02d}.pdf"
        validity = baseline_period_end_date(int(download_result["year"]), int(download_result["donem"]))

        try:
            history = KAPPdfParser().parse_directory(output_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"[UYARI] [{fon_kodu}] PDF parse edilirken beklenmeyen hata, atlaniyor: {exc}")
            _log_step(
                execution_logs,
                f"Global baseline RED: fon={fon_kodu}, PDF={pdf_name}, parse hatası={exc}.",
            )
            continue

        holdings = history.get(period_key) or {}
        if not holdings:
            print(f"[UYARI] [{fon_kodu}] {period_key} donemi icin ayristirilmis veri bos donuyor, atlaniyor.")
            _log_step(
                execution_logs,
                f"Global baseline RED: fon={fon_kodu}, PDF={pdf_name}, period_key={period_key}, "
                "parse sonucu boş holdings.",
            )
            continue

        global_baseline[fon_kodu] = holdings
        baseline_periods[fon_kodu] = (download_result["year"], download_result["donem"])
        print(f"[BASARILI] [{fon_kodu}] {period_key} (EN GUNCEL) baseline'i toplandi ({len(holdings)} hisse kodu).")
        _log_step(
            execution_logs,
            f"Global baseline OK: fon={fon_kodu}, PDF='{pdf_name}', dönem={period_key}, "
            f"geçerlilik_sonu={_format_tr_date(validity)}, hisse_kodu_sayısı={len(holdings)}, "
            f"örnek_kodlar={sorted(holdings)[:8]}.",
        )

    all_tickers = {ticker for holdings in global_baseline.values() for ticker in holdings}
    print(
        f"\n[SISTEM] Global Baseline Toplama Tamamlandi: {len(global_baseline)}/{len(related_funds)} fon "
        f"basariyla toplandi, {len(all_tickers)} benzersiz hisse kodu bulundu."
    )
    _log_step(
        execution_logs,
        f"Global baseline özet: başarılı={len(global_baseline)}/{len(related_funds)}, "
        f"fonlar={sorted(global_baseline)}, benzersiz_hisse={len(all_tickers)}, "
        f"atlanan={ [c for c in related_funds if c not in global_baseline] }.",
    )
    return global_baseline, baseline_periods


# --- Step 3: daily TEFAS "buying power" (equity TL exposure) per fund -------


def _tarih_ddmmyyyy_to_iso(tarih: Optional[str]) -> Optional[str]:
    """Converts data_scraper.py's stored "DD.MM.YYYY" date string into
    "YYYY-MM-DD" (the key format `build_tefas_power_matrix` uses, matching
    the task's own `tefas_power_matrix` example). Returns None (rather
    than raising) for anything that isn't a clean DD.MM.YYYY string."""
    if not tarih or not isinstance(tarih, str):
        return None
    parts = tarih.split(".")
    if len(parts) != 3:
        return None
    day, month, year = parts
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return None


LIQUIDITY_ASSET_KEYWORDS: Tuple[str, ...] = ("repo", "para piyasası", "mevduat")


def _tr_lower(text) -> str:
    """Same Turkish-casing-safe lowercasing as `KAPDeltaEngine._normalize`
    (plain "İ"/"I" -> "i" before `.lower()`), used here for
    keyword-matching Varliklar asset names rather than HTML table headers.
    """
    return str(text).replace("İ", "i").replace("I", "i").lower()


def _liquidity_ratio_pct(varliklar: Dict[str, float]) -> float:
    """Sums every `Varliklar` percentage whose asset name matches a known
    liquidity/cash-equivalent category, for the "Likidite Orani" term of
    the Aktif Guc formula (see `build_tefas_power_matrix`):

    - "repo"          -> matches BOTH "Repo" and "Ters-Repo" ("Repo-Trepo"
                         combined, as a single fon-industry concept), added
                         with their own sign (a fund that borrowed via repo
                         has a NEGATIVE "Repo" %, which correctly reduces
                         net liquidity rather than being ignored).
    - "para piyasası"  -> matches money-market lines regardless of naming
                         era: TEFAS_DISTRIBUTION_MAP currently emits
                         "Borsa İstanbul Para Piyasası" for both the "bpp"
                         and "tpp" abbreviations, which is the successor
                         name to what used to be called "Takasbank Para
                         Piyasası" -- matching on the shared "Para
                         Piyasası" substring is robust to either naming.
    - "mevduat"        -> matches any deposit line (e.g. "Mevduat (TL)").

    Returns 0.0 (never raises, and never treated as "missing/unknown") if
    none of these are present in `varliklar` -- per spec, absent liquidity
    components simply mean 0% liquidity contribution, unlike a wholly
    missing `Varliklar` dict (handled by the caller before this is ever
    invoked).
    """
    total = 0.0
    for asset_name, pct in varliklar.items():
        if not any(keyword in _tr_lower(asset_name) for keyword in LIQUIDITY_ASSET_KEYWORDS):
            continue
        try:
            total += float(pct)
        except (TypeError, ValueError):
            continue
    return total


def build_tefas_power_matrix(
    fund_codes: List[str],
    days_back: int = 30,
    execution_logs: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Dict[str, float]]:
    """Adim 3 of the Shadow Portfolio pipeline: for every fund code passed
    in (typically the FULL `discover_related_funds()` output, not just the
    subset that also happened to have a KAP PDF baseline -- a fund like
    T3B/TGI can have daily TEFAS data even with no monthly report), pulls
    the last `days_back` days of TEFAS AUM ("Toplam Deger") and portfolio
    distribution ("Hisse Senedi" %, plus liquidity lines) via the
    project's existing, already-working TEFAS scraper
    (`fon_terminal/data_scraper.py`, one directory up from this sandbox --
    see the "Bridging to data_scraper.py" note below), and turns that into
    a daily "Aktif Guc" (active purchasing power) figure per fund -- not
    just the equity portion, but equity PLUS cash-like liquidity, since
    both are capital a fund can actually deploy into a shared pool of
    stock being bought/sold by its manager:

        Aktif_Guc_TL = Toplam_AUM * (Hisse_Senedi_Orani + Likidite_Orani) / 100

    where Likidite_Orani is the sum of every liquidity-category percentage
    in that day's `Varliklar` (see `_liquidity_ratio_pct`: "Repo"/"Ters-
    Repo", any "... Para Piyasası" money-market line, and "Mevduat").

    Returns a nested dict keyed by fund code then ISO date:

        {"TLY": {"2026-07-23": 187837027636.22, ...}, "THF": {...}, ...}

    Bridging to data_scraper.py: this is the one place in the
    kap_pdf_downloader sandbox that intentionally breaks its own "no
    dependency on fon_terminal's own modules" rule (see this folder's
    README) -- TEFAS AUM/distribution data only exists in that module, and
    duplicating its Playwright WAF-bypass logic here would be a
    maintenance hazard, not an improvement. The import is done lazily
    (only when this function actually runs) via a `sys.path` bridge to the
    parent `fon_terminal/` directory, so nothing else in this sandbox
    requires `playwright` to be installed. `data_scraper.DATABASE_FILE` is
    also redirected to a dedicated `tefas_cache.json` inside THIS folder
    (never the app's real `fon_terminal/fund_database.json`) so this
    exploratory pipeline can never add an unrequested fund tab to the live
    dashboard or otherwise mutate production data as a side effect.

    Never raises: a total TEFAS handshake failure (`data_scraper.
    scrape_and_update` can raise `RuntimeError` if Playwright can't get a
    session at all), a specific fund's scrape failing (e.g. an invalid
    TEFAS code such as a fund that only exists at KAP), a fund with no
    stored records, a malformed/missing date, a missing AUM figure, or an
    unparseable percentage are all caught individually, logged as
    "[UYARI]", and result in that fund/day being skipped -- never a crash.

    Note on a missing "Hisse Senedi" key specifically (as opposed to a
    wholly missing/empty "Varliklar" dict): the former means the fund sold
    off its entire equity position that day, so 0% is treated as the
    correct value (not missing data); the latter means TEFAS returned no
    distribution breakdown at all for that day (e.g. a restricted/
    qualified fund) and is treated as genuinely unknown, so that day is
    skipped rather than assuming 0% -- same distinction already documented
    in `fon_terminal/README.md`'s "Handling Incomplete Financial Data".
    Missing liquidity components, unlike missing equity data, are always
    treated as 0% (see `_liquidity_ratio_pct`), per this feature's spec.

    `execution_logs` (optional, default None): a shared list (see
    `_log_step`) that this function appends short narrative entries to
    for the HTML "Execution Trace" section. Purely additive: omitting it
    changes nothing about this function's return value or behavior.
    """
    import os
    import sys

    sandbox_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(sandbox_dir)  # fon_terminal/
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        import data_scraper
    except ImportError as exc:
        print(f"[HATA] TEFAS veri modulu (fon_terminal/data_scraper.py) import edilemedi: {exc}")
        return {}

    # Redirect to a sandbox-local cache file -- see docstring above for why
    # this must never point at the live app's fund_database.json.
    data_scraper.DATABASE_FILE = os.path.join(sandbox_dir, "tefas_cache.json")

    print(
        f"\n[SISTEM] TEFAS Gunluk Aktif Guc Matrisi hesaplaniyor: "
        f"{len(fund_codes)} fon, son {days_back} gun."
    )
    _log_step(
        execution_logs,
        f"TEFAS scrape başlıyor: data_scraper.scrape_and_update(fund_list={fund_codes}, "
        f"days_back={days_back}), cache_file={data_scraper.DATABASE_FILE}. "
        "Formül: Aktif_Güç_TL = ToplamDeger × (Hisse_Senedi_% + Likidite_%) / 100; "
        "Likidite_% = Repo/Ters-Repo + Para Piyasası + Mevduat.",
    )

    try:
        scrape_results = data_scraper.scrape_and_update(fund_codes, days_back=days_back)
    except Exception as exc:  # noqa: BLE001 - a total TEFAS handshake failure must never abort the caller
        print(f"[UYARI] TEFAS verisi cekilemedi (tum fonlar icin, oturum acilamadi olabilir): {exc}")
        _log_step(
            execution_logs,
            f"TEFAS scrape RED: Playwright/handshake hatası, hiç fon işlenmedi. Hata={exc}.",
        )
        return {}

    database = data_scraper.load_database()

    tefas_power_matrix: Dict[str, Dict[str, float]] = {}
    successful_funds = 0

    for fund_code in fund_codes:
        fund_code = fund_code.strip().upper()
        status = (scrape_results.get(fund_code) or {}).get("status")
        if status != "success":
            message = (scrape_results.get(fund_code) or {}).get("message", "bilinmeyen hata")
            print(f"[UYARI] [{fund_code}] TEFAS verisi cekilemedi ({message}), atlaniyor.")
            continue

        records = ((database.get(fund_code) or {}).get("records")) or []
        if not records:
            print(f"[UYARI] [{fund_code}] TEFAS onbelleginde kayit bulunamadi, atlaniyor.")
            continue

        gunluk_aktif_guc: Dict[str, float] = {}
        for record in records:
            iso_date = _tarih_ddmmyyyy_to_iso(record.get("Tarih"))
            if not iso_date:
                print(f"[UYARI] [{fund_code}] gecersiz/eksik tarih, bu gun atlaniyor: {record.get('Tarih')!r}")
                continue

            varliklar = record.get("Varliklar")
            if not varliklar:
                # Wholly missing distribution data (TEFAS weekend/holiday
                # gap, or a restricted/qualified fund) -- genuinely
                # unknown, never assumed to be 0%.
                print(f"[UYARI] [{fund_code}] {iso_date}: portfoy dagilim verisi yok, gun atlaniyor.")
                continue

            aum = record.get("ToplamDeger")
            if aum is None:
                print(f"[UYARI] [{fund_code}] {iso_date}: ToplamDeger (AUM) eksik, gun atlaniyor.")
                continue

            # Present Varliklar dict but no "Hisse Senedi" key specifically
            # means the fund held 0% equities that day -- a real value,
            # not missing data (see docstring). Missing liquidity lines are
            # always 0% (see _liquidity_ratio_pct's own docstring).
            hisse_yuzdesi = varliklar.get("Hisse Senedi", 0.0)
            likidite_yuzdesi = _liquidity_ratio_pct(varliklar)

            try:
                toplam_oran = float(hisse_yuzdesi) + float(likidite_yuzdesi)
                gunluk_aktif_guc[iso_date] = float(aum) * (toplam_oran / 100.0)
            except (TypeError, ValueError) as exc:
                print(f"[UYARI] [{fund_code}] {iso_date}: aktif guc hesaplanamadi ({exc}), gun atlaniyor.")
                continue

        if not gunluk_aktif_guc:
            print(f"[UYARI] [{fund_code}] kullanilabilir hicbir gun bulunamadi, atlaniyor.")
            continue

        tefas_power_matrix[fund_code] = gunluk_aktif_guc
        successful_funds += 1
        latest_day = max(gunluk_aktif_guc)
        print(f"[BASARILI] [{fund_code}] {len(gunluk_aktif_guc)} gunluk aktif guc (TL) hesaplandi.")
        _log_step(
            execution_logs,
            f"TEFAS Aktif Güç OK: fon={fund_code}, gün_sayısı={len(gunluk_aktif_guc)}, "
            f"ilk_gün={min(gunluk_aktif_guc)}, son_gün={latest_day}, "
            f"son_gün_Aktif_Güç={_format_tr_number(gunluk_aktif_guc[latest_day])} TL.",
        )

    all_days = {day for daily in tefas_power_matrix.values() for day in daily}
    total_fund_days = sum(len(daily) for daily in tefas_power_matrix.values())
    print(
        f"\n[SISTEM] TEFAS Gunluk Aktif Guc Matrisi Tamamlandi: "
        f"{successful_funds}/{len(fund_codes)} fon basariyla islendi, "
        f"{len(all_days)} farkli gun kapsandi (toplam {total_fund_days} fon-gun kaydi)."
    )
    _log_step(
        execution_logs,
        f"TEFAS güç matrisi özet: başarılı_fon={successful_funds}/{len(fund_codes)}, "
        f"fonlar={sorted(tefas_power_matrix)}, benzersiz_gün={len(all_days)}, "
        f"fon_gün_kaydı={total_fund_days}, "
        f"atlanan={[c for c in fund_codes if c.strip().upper() not in tefas_power_matrix]}.",
    )
    return tefas_power_matrix


# --- Step 6: live BIST prices + target fund AUM, for a "% of portfolio" weight -----


def fetch_bist_prices(tickers: List[str]) -> Dict[str, Optional[float]]:
    """Fetches the latest available closing price (TL) for a batch of
    BIST-listed tickers via `yfinance`, for the portfolio evolution
    table's "Güncel Ağırlık (%)" column: `Hisse Pozisyon Büyüklüğü =
    Güncel Tahmini Lot * Güncel Fiyat`, then that position size as a
    percentage of the target fund's total AUM (see `get_latest_aum_for_fund`).

    Yahoo Finance requires a ".IS" suffix for Istanbul-listed symbols
    (e.g. "PEKGY" -> "PEKGY.IS") -- added here, transparently, so callers
    keep working with this project's own bare ticker codes everywhere
    else.

    Issues ONE batched network request for the WHOLE ticker list (via
    `yfinance.download(..., group_by="ticker")`), not one request per
    ticker -- important for a 20-40 ticker portfolio, where a per-ticker
    request pattern would be both slow and far more likely to trip
    Yahoo Finance's own rate limiting.

    Never raises, and never fabricates a price: a missing/delisted/
    newly-IPO'd ticker, a `yfinance` import failure (library not
    installed), a total network/API failure, or any per-ticker parsing
    error all result in that ticker mapping to `None` in the returned
    dict -- NOT to `0.0`, which would be silently indistinguishable from
    a real zero price and would corrupt the weight calculation (a `None`
    price must make the caller skip that ticker's weight entirely, per
    this feature's spec).
    """
    tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    prices: Dict[str, Optional[float]] = {ticker: None for ticker in tickers}
    if not tickers:
        return prices

    try:
        import yfinance as yf
    except ImportError:
        print(
            "[UYARI] 'yfinance' kütüphanesi kurulu değil; güncel BIST fiyatları çekilemedi "
            "(pip install yfinance)."
        )
        return prices

    symbol_map = {f"{ticker}.IS": ticker for ticker in tickers}
    symbols = list(symbol_map.keys())

    print(f"[SISTEM] {len(symbols)} BIST hissesi icin guncel fiyat cekiliyor (yfinance, tek toplu istek)...")
    try:
        data = yf.download(
            tickers=symbols,
            period="5d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001 - a total yfinance/network failure must never crash the caller
        print(f"[UYARI] BIST fiyatlari cekilirken beklenmeyen hata (yfinance): {exc}")
        return prices

    if data is None or data.empty:
        print("[UYARI] yfinance hicbir fiyat verisi dondurmedi (tum semboller icin).")
        return prices

    for symbol, ticker in symbol_map.items():
        try:
            try:
                close_series = data[symbol]["Close"]
            except (KeyError, TypeError):
                # A single-symbol request doesn't always come back with the
                # same MultiIndex-per-symbol column shape as a multi-symbol
                # one across yfinance versions -- fall back to a flat
                # "Close" column in that case.
                close_series = data["Close"]
            close_series = close_series.dropna()
            if close_series.empty:
                continue
            prices[ticker] = float(close_series.iloc[-1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue

    found = sum(1 for price in prices.values() if price is not None)
    missing = [ticker for ticker, price in prices.items() if price is None]
    print(f"[SISTEM] BIST fiyat cekme tamamlandi: {found}/{len(tickers)} hisse icin fiyat bulundu.")
    if missing:
        print(
            f"[UYARI] Su hisseler icin BIST fiyati bulunamadi (delist/yeni halka arz/hatali kod olabilir): {missing}"
        )

    return prices


def get_latest_aum_for_fund(fon_kodu: str) -> Optional[Tuple[str, float]]:
    """Returns `(iso_date, ToplamDeger)` for `fon_kodu`'s most recent
    TEFAS record in this sandbox's local cache (`tefas_cache.json`) --
    the RAW total AUM figure, deliberately NOT the "Aktif Guc"
    (equity+liquidity) figure `build_tefas_power_matrix` computes, since
    the evolution table's "% of portfolio" weight needs the fund's TRUE
    total size as its denominator, not a purchasing-power subset of it.

    Must be called AFTER `build_tefas_power_matrix` has run at least once
    for this fund in the current process -- that call is what populates
    `tefas_cache.json` via `data_scraper.scrape_and_update`. This function
    does not scrape on its own; it only reads whatever is already cached,
    keeping it a fast, no-network lookup.

    Never raises: a missing cache file, a fund with no cached records, or
    a record with an unparseable date/AUM all result in `None` rather
    than a crash or a fabricated figure.
    """
    import json
    import os

    sandbox_dir = os.path.dirname(os.path.abspath(__file__))
    cache_file = os.path.join(sandbox_dir, "tefas_cache.json")
    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file, "r", encoding="utf-8") as file:
            database = json.load(file)
    except (OSError, ValueError):
        return None

    records = ((database.get(fon_kodu.strip().upper()) or {}).get("records")) or []
    if not records:
        return None

    latest_iso: Optional[str] = None
    latest_aum: Optional[float] = None
    for record in records:
        iso_date = _tarih_ddmmyyyy_to_iso(record.get("Tarih"))
        raw_aum = record.get("ToplamDeger")
        if iso_date is None or raw_aum is None:
            continue
        if latest_iso is not None and iso_date <= latest_iso:
            continue
        try:
            latest_aum = float(raw_aum)
        except (TypeError, ValueError):
            continue
        latest_iso = iso_date

    if latest_iso is None or latest_aum is None:
        return None
    return latest_iso, latest_aum


if __name__ == "__main__":
    from kap_pdf_parser import export_to_html

    FON_KODU = "TLY"

    # "Execution Trace" narrative log (see `_log_step`) -- one single
    # shared list, created BEFORE anything else runs, that every step of
    # this pipeline (this __main__ block itself, KAPDeltaEngine's methods,
    # and the module-level collect_global_baseline/build_tefas_power_matrix
    # functions) appends short, human-readable story entries into. Passed
    # into `export_to_html`'s `delta_report` at the very end so the HTML
    # report can render the FULL step-by-step reasoning, not just the
    # final tables -- see kap_pdf_parser._render_execution_log.
    execution_logs: List[Dict[str, str]] = []

    # "Date Lag" fix (2026-07-31): simply parsing whatever PDFs already
    # happened to sit in tly_pdfs/ from a previous run (`max(history)`)
    # silently locked the baseline to a stale month (e.g. March) even
    # though KAP had since published newer reports. TLY's own EN GUNCEL
    # ("most recent") report is now explicitly (re)downloaded here via
    # KAPPdfDownloader.download_latest_report() -- which also deletes any
    # older cached PDF for this fund -- before ever parsing tly_pdfs/, so
    # the baseline always reflects what KAP has published TODAY, not
    # whatever happened to be on disk.
    _log_step(
        execution_logs,
        f"Adım 0: KAP'tan {FON_KODU} için en güncel 'Portföy Dağılım Raporu' soruluyor "
        f"(KAPPdfDownloader.download_latest_report, days_back varsayılan). Diskteki eski "
        "PDF'lere güvenilmiyor.",
    )
    with KAPPdfDownloader(fon_kodu=FON_KODU) as tly_downloader:
        tly_latest = tly_downloader.download_latest_report()

    if not tly_latest or tly_latest.get("status") != "success":
        raise SystemExit(
            f"'{FON_KODU}' icin KAP'ta en guncel 'Portfoy Dagilim Raporu' bulunamadi/indirilemedi; "
            "durduruluyor."
        )

    parser = KAPPdfParser()
    history = parser.parse_directory("tly_pdfs")

    latest_period = f"{tly_latest['year']}_{tly_latest['donem']:02d}"
    baseline_data = history.get(latest_period) or {}
    baseline_pdf_name = f"{FON_KODU}_{tly_latest['year']}_{int(tly_latest['donem']):02d}.pdf"

    if not baseline_data:
        raise SystemExit(
            f"'{latest_period}' donemi indirildi ama ayristirilan veri bos donuyor; durduruluyor."
        )

    baseline_year = int(tly_latest["year"])
    baseline_donem = int(tly_latest["donem"])
    baseline_end = baseline_period_end_date(baseline_year, baseline_donem)

    _log_step(
        execution_logs,
        f"Taban veri '{baseline_pdf_name}' olarak tespit edildi. Dönem={latest_period}, "
        f"geçerlilik tarihi ayın son günü olan {_format_tr_date(baseline_end)} olarak atandı "
        f"(calendar.monthrange({baseline_year}, {baseline_donem})). "
        f"Parse edilen hisse kodu sayısı={len(baseline_data)}. "
        f"Örnek kodlar={sorted(baseline_data)[:10]}.",
    )

    print("=== KAPDeltaEngine + KAPPdfParser Entegre Test ===")
    print(f"Baseline donemi: {latest_period}  ({len(baseline_data)} kod)\n")

    # Double-counting fix: the monthly PDF for (year, donem) already
    # contains holdings as of that month's LAST day. Delta must open on
    # the next calendar day -- NOT "today - 30 days", which previously
    # re-applied trades already baked into the baseline PDF.
    start = baseline_period_to_delta_start(baseline_year, baseline_donem)
    end = date.today()
    if start > end:
        raise SystemExit(
            f"Baseline donemi {baseline_donem:02d}/{baseline_year} henuz bitmemis "
            f"(delta baslangici {start.isoformat()} bugunden ({end.isoformat()}) sonra); "
            "delta araligi bos -- once sonraki ayin basini bekleyin ya da daha eski bir "
            "baseline donemi kullanin."
        )
    print(
        f"[SISTEM] Delta araligi: {start.isoformat()} -> {end.isoformat()} "
        f"(baseline PDF {baseline_donem:02d}/{baseline_year} ayinin son gununden ertesi gun)\n"
    )
    _log_step(
        execution_logs,
        f"Delta penceresi hesaplandı: start_date={start.isoformat()} "
        f"({_format_tr_date(start)}), end_date={end.isoformat()} ({_format_tr_date(end)}). "
        f"Kural: start_date = baseline_period_end({_format_tr_date(baseline_end)}) + 1 gün. "
        f"'{baseline_pdf_name}' içindeki {_format_tr_date(baseline_end)} ve öncesi "
        "günlük bildirimler işleme ALINMAYACAK (double-counting koruması).",
    )

    with KAPDeltaEngine(fon_kodu=FON_KODU, execution_logs=execution_logs) as engine:
        updated_data, resolved, unresolved = engine.apply_delta(
            baseline_data,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )

    print("\n=== Sonuc (Onceki -> Delta -> Sonraki) ===")
    all_codes = sorted(set(baseline_data) | set(updated_data))
    for code in all_codes:
        before = baseline_data.get(code, 0.0)
        after = updated_data.get(code, 0.0)
        delta = after - before
        print(f"{code:8s}  Onceki: {before:>15,.2f}   Delta: {delta:>+15,.2f}   Sonraki: {after:>15,.2f}")

    print(f"\n=== Kesinlesen deltalar: {len(resolved)} ===")
    for r in resolved:
        print(f"  {r.transaction_date}  {r.ticker:<8}  {r.lot_amount:>15,.2f}  {r.direction}")

    print(f"\n=== Cozulemeyen (cok-fonlu) bildirimler: {len(unresolved)} ===")
    for txn in unresolved[:10]:
        print(
            f"  disclosureIndex={txn.disclosure_index}  tarih={txn.transaction_date}  "
            f"sirket(ler)={txn.traded_companies}  ilgili_fonlar={txn.related_funds}  "
            f"alim={txn.buy_nominal_tl}  satim={txn.sell_nominal_tl}  net={txn.net_nominal_tl}"
        )
    if len(unresolved) > 10:
        print(f"  ... ve {len(unresolved) - 10} tane daha.")

    print("\n=== Keşif (Discovery) Aşaması ===")
    related_funds_target_array = engine.discover_related_funds(unresolved)

    print("\n=== Adım 2: Global Baseline Toplama ===")
    # Her fon artik KENDI en guncel raporunu kullaniyor (bkz.
    # collect_global_baseline'in guncellenmis docstring'i) -- tek, ortak
    # bir baseline_period ZORLANMIYOR.
    global_baseline, baseline_periods = collect_global_baseline(
        related_funds_target_array, execution_logs=execution_logs
    )
    print("\n=== Global Baseline Özeti ===")
    for fund_code in related_funds_target_array:
        holdings = global_baseline.get(fund_code)
        if holdings is None:
            print(f"  {fund_code:8s}  -> VERI YOK (atlandi)")
        else:
            period = baseline_periods.get(fund_code)
            period_label = f"{period[1]:02d}/{period[0]}" if period else "?"
            print(f"  {fund_code:8s}  -> {len(holdings)} hisse kodu  (donem: {period_label})")

    print("\n=== Adım 3: TEFAS Günlük Aktif Güç Matrisi ===")
    # BUG FIX (2026-07-30): Adım 2'de KAP PDF baseline'ı bulunamayan fonlar
    # (T3B, TGI) TEFAS'ta hala gecerli, gunluk veri yayinlayan fonlar
    # olabilir -- sadece global_baseline.keys() (PDF'i olanlar) yerine,
    # kesif asamasinin TAM listesi (related_funds_target_array) gonderilir,
    # boylece TEFAS kapsamı KAP PDF kapsamiyla gereksiz yere sinirlanmaz.
    tefas_power_matrix = build_tefas_power_matrix(
        related_funds_target_array, days_back=30, execution_logs=execution_logs
    )

    print("\n=== TEFAS Aktif Güç Matrisi Özeti ===")
    for fund_code in related_funds_target_array:
        gunluk_aktif_guc = tefas_power_matrix.get(fund_code)
        if not gunluk_aktif_guc:
            print(f"  {fund_code:8s}  -> VERI YOK (atlandi)")
            continue
        latest_day = max(gunluk_aktif_guc)
        print(
            f"  {fund_code:8s}  -> {len(gunluk_aktif_guc)} gun  "
            f"(son gun {latest_day}: {gunluk_aktif_guc[latest_day]:,.2f} TL Aktif Guc)"
        )

    print("\n=== Adım 4: Çoklu Fon İşlemlerinin Oransal Dağıtımı (Zaman Çizelgesi) ===")
    # `engine`'in (yukarida `with` bloğu ile kapatılmış) requests.Session'ı
    # burada gerekmiyor -- resolve_multi_fund_deltas tamamen zaten elde
    # edilmiş `unresolved`/`tefas_power_matrix` verisi üzerinde çalışan saf
    # bir hesaplama metodu, ağ erişimi yapmaz -- bu yüzden yeni bir
    # KAPDeltaEngine açmak yerine aynı `engine` nesnesi yeniden kullanılır.
    updated_data, proportionally_resolved = engine.resolve_multi_fund_deltas(
        unresolved, tefas_power_matrix, updated_data
    )

    print(f"\n=== Oransal olarak dağıtılan kayıtlar: {len(proportionally_resolved)} ===")
    for item in proportionally_resolved[:10]:
        print(
            f"  {item.transaction_date}  {item.ticker:<8}  havuz_payi=%{item.target_weight_pct:>6.2f}  "
            f"tahmini_lot={item.estimated_lot:>+15,.2f}  {item.direction}  "
            f"ilgili_fonlar={item.related_funds}"
        )
    if len(proportionally_resolved) > 10:
        print(f"  ... ve {len(proportionally_resolved) - 10} tane daha.")

    print("\n=== Nihai Portföy (Baseline + Tek-Fonlu + Oransal Dağıtım) ===")
    all_codes = sorted(set(baseline_data) | set(updated_data))
    for code in all_codes:
        before = baseline_data.get(code, 0.0)
        after = updated_data.get(code, 0.0)
        delta = after - before
        print(f"{code:8s}  Onceki: {before:>15,.2f}   Delta: {delta:>+15,.2f}   Sonraki: {after:>15,.2f}")

    print("\n=== Adım 6: Güncel BIST Fiyatları ve Portföy Ağırlığı (%) ===")
    yf_symbols = [f"{code}.IS" for code in all_codes]
    _log_step(
        execution_logs,
        f"yfinance toplu fiyat isteği: tickers={yf_symbols}, period='5d', "
        f"group_by='ticker', ham_kod_sayısı={len(all_codes)}.",
    )
    current_prices = fetch_bist_prices(all_codes)
    found_price_count = sum(1 for price in current_prices.values() if price is not None)
    missing_price_codes = [code for code, price in current_prices.items() if price is None]
    _log_step(
        execution_logs,
        f"yfinance yanıtı: fiyat_bulunan={found_price_count}/{len(all_codes)}, "
        f"fiyat_yok={missing_price_codes or '[]'}.",
    )

    aum_info = get_latest_aum_for_fund(FON_KODU)
    if aum_info is None:
        print(f"[UYARI] {FON_KODU} icin tefas_cache.json'da guncel ToplamDeger (AUM) bulunamadi; agirlik (%) hesabi atlanacak.")
        _log_step(
            execution_logs,
            f"AUM okuma RED: get_latest_aum_for_fund('{FON_KODU}') -> None "
            "(tefas_cache.json'da ToplamDeger yok). Güncel Ağırlık (%) hesaplanmadı.",
        )
        current_aum: Optional[float] = None
        current_aum_date: Optional[str] = None
    else:
        current_aum_date, current_aum = aum_info
        print(f"[SISTEM] {FON_KODU} guncel Toplam AUM: {current_aum:,.2f} TL (tarih: {current_aum_date})")
        _log_step(
            execution_logs,
            f"AUM okuma OK: fon={FON_KODU}, kaynak=tefas_cache.json, alan=ToplamDeger (ham AUM, "
            f"Aktif Güç değil), tarih={current_aum_date}, "
            f"değer={_format_tr_number(current_aum)} TL. Bu değer ağırlık paydasıdır.",
        )

    # Mechanical per-ticker weight audit (same formula the HTML table uses).
    if current_aum:
        weight_rows = []
        for code in all_codes:
            lot = float(updated_data.get(code, 0.0))
            price = current_prices.get(code)
            if price is None:
                _log_step(
                    execution_logs,
                    f"{code}: yfinance fiyatı yok ({code}.IS); Güncel Ağırlık (%) atlandı "
                    f"(lot={_format_tr_number(lot)}).",
                )
                continue
            position_tl = lot * float(price)
            weight_pct = (position_tl / current_aum) * 100.0
            weight_rows.append((code, price, lot, position_tl, weight_pct))
            _log_step(
                execution_logs,
                f"{code} hissesi için yfinance'den [{_format_tr_number(float(price))} TL] anlık "
                f"fiyat çekildi. Pozisyon={_format_tr_number(lot)} lot × "
                f"{_format_tr_number(float(price))} TL = {_format_tr_number(position_tl)} TL. "
                f"Güncel Ağırlık [Pozisyon / {_format_tr_number(current_aum)} TL] formülüyle "
                f"%{_format_tr_number(weight_pct)} olarak hesaplandı.",
            )
        weight_rows.sort(key=lambda row: row[4], reverse=True)
        if weight_rows:
            top = weight_rows[0]
            covered = sum(row[3] for row in weight_rows)
            _log_step(
                execution_logs,
                f"Ağırlık özeti: fiyatı olan hisse={len(weight_rows)}, "
                f"en_büyük_pozisyon={top[0]} (%{_format_tr_number(top[4])}), "
                f"fiyatı_olanların_toplam_pozisyon_AUM_payı="
                f"%{_format_tr_number((covered / current_aum) * 100.0)}.",
            )

    # Reshape into the plain dict/list shapes kap_pdf_parser.export_to_html
    # expects, so that module keeps zero hard dependency on this one's
    # dataclasses (see its own docstring: sandbox-isolated, pdfplumber only).
    resolved_plain = [
        {"date": r.transaction_date, "ticker": r.ticker, "lot": r.lot_amount, "direction": r.direction}
        for r in resolved
    ]
    unresolved_plain = [
        {
            "date": txn.transaction_date,
            "related_funds": txn.related_funds,
            "companies": txn.traded_companies,
            "net_lot": txn.net_nominal_tl,
        }
        for txn in unresolved
    ]
    proportional_plain = [
        {
            "date": item.transaction_date,
            "ticker": item.ticker,
            "related_funds": item.related_funds,
            "pool_total": item.pool_total_tl,
            "target_power": item.target_power_tl,
            "weight_pct": item.target_weight_pct,
            "net_lot_total": item.net_lot_total,
            "estimated_lot": item.estimated_lot,
            "direction": item.direction,
        }
        for item in proportionally_resolved
    ]

    print()
    export_to_html(
        history,
        output_filename="parser_kontrol_raporu.html",
        delta_report={
            "fon_kodu": FON_KODU,
            "baseline_period": latest_period,
            "baseline_data": baseline_data,
            "resolved": resolved_plain,
            "unresolved": unresolved_plain,
            "proportionally_resolved": proportional_plain,
            "updated_data": updated_data,
            "tefas_power_matrix": tefas_power_matrix,
            "current_prices": current_prices,
            "current_aum": current_aum,
            "current_aum_date": current_aum_date,
            "execution_logs": execution_logs,
        },
    )
