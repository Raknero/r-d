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
    from kap_delta_engine import KAPDeltaEngine

    baseline = {"SVGYO": 10000.0}  # from KAPPdfParser.parse_file(...)
    with KAPDeltaEngine(fon_kodu="TLY") as engine:
        updated, unresolved = engine.apply_delta(baseline, start_date="2026-06-01", end_date="2026-07-28")
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from kap_downloader import KAPPdfDownloader


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
            response = self.session.post(url, json=body, timeout=self.timeout)
            response.raise_for_status()
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
            response = self.session.get(
                url, timeout=self.timeout, headers={"Accept": "text/html,application/xhtml+xml"}
            )
            response.raise_for_status()
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
        """
        print(f"[SISTEM] [{self.fon_kodu}] Delta motoru calisiyor: {start_date} -> {end_date}...")

        disclosures = self._fetch_delta_disclosures(start_date, end_date)

        updated_data = dict(baseline_data)
        resolved: List[ResolvedDelta] = []
        unresolved: List[ParsedTransaction] = []

        if not disclosures:
            print(f"[SISTEM] [{self.fon_kodu}] Uygulanacak bildirim bulunamadi; baseline degistirilmedi.")
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
                transactions = []

            for txn in transactions:
                if txn.related_funds == [self.fon_kodu] and txn.net_nominal_tl is not None:
                    direction = (
                        "ALIM" if txn.net_nominal_tl > 0 else "SATIM" if txn.net_nominal_tl < 0 else "DEGISIM YOK"
                    )
                    for company in txn.traded_companies or ["BILINMEYEN"]:
                        updated_data[company] = updated_data.get(company, 0.0) + txn.net_nominal_tl
                        resolved.append(
                            ResolvedDelta(
                                disclosure_index=txn.disclosure_index,
                                transaction_date=txn.transaction_date,
                                ticker=company,
                                lot_amount=abs(txn.net_nominal_tl),
                                direction=direction,
                            )
                        )
                else:
                    unresolved.append(txn)

            if index != len(disclosures) - 1:
                time.sleep(self.request_delay)

        print(
            f"[SISTEM] [{self.fon_kodu}] Tamamlandi: {len(disclosures)} bildirim islendi -> "
            f"{len(resolved)} tek-fonlu (uygulandi), {len(unresolved)} cok-fonlu/belirsiz (uygulanmadi)."
        )
        return updated_data, resolved, unresolved



if __name__ == "__main__":
    from datetime import date

    from kap_pdf_parser import KAPPdfParser, export_to_html

    FON_KODU = "TLY"

    # Real baseline: the latest monthly "Portfoy Dagilim Raporu" already
    # downloaded into tly_pdfs/, parsed by KAPPdfParser (not a hardcoded
    # stand-in) -- this is the same parse_directory() call
    # kap_pdf_parser.py's own __main__ block uses.
    parser = KAPPdfParser()
    history = parser.parse_directory("tly_pdfs")

    if not history:
        raise SystemExit(
            "tly_pdfs/ icinde ayristirilabilir bir rapor bulunamadi; "
            "once kap_downloader.py ile en az bir PDF indirilmis olmali."
        )

    latest_period = max(history)
    baseline_data = history[latest_period]

    print("=== KAPDeltaEngine + KAPPdfParser Entegre Test ===")
    print(f"Baseline donemi: {latest_period}  ({len(baseline_data)} kod)\n")

    end = date.today()
    start = end - timedelta(days=30)

    with KAPDeltaEngine(fon_kodu=FON_KODU) as engine:
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

    print()
    export_to_html(
        history,
        output_filename="parser_kontrol_raporu.html",
        delta_report={
            "fon_kodu": FON_KODU,
            "baseline_period": latest_period,
            "resolved": resolved_plain,
            "unresolved": unresolved_plain,
            "updated_data": updated_data,
        },
    )
