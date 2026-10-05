"""
kap_pdf_parser.py

Standalone, self-contained module for parsing a fund's
"Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF -- weekly or
monthly, as downloaded by `kap_downloader.py` -- into a clean
{hisse_kodu: lot_miktari} dictionary for the "HISSE SENETLERI"
(equities) section only. The filename slug identifies WHICH filing the
PDF is; WHEN its holdings were valued is established separately from
the document's own figures (`extract_fingerprint` + `report_dating`).

This module lives in the same isolated sandbox as `kap_downloader.py` and
has no dependency on any other part of the host project; it only needs the
third-party `pdfplumber` library.

Usage:
    from kap_pdf_parser import KAPPdfParser

    parser = KAPPdfParser()
    holdings = parser.parse_file("tly_pdfs/TLY_2026_03.pdf")
    # {"ALKLC": 731256.0, "CWENE": 3000000.0, ...}

    history = parser.parse_directory("tly_pdfs")
    # {"2026_AB06": {...}, "2026_AB07": {...}, "2026_HB35": {...}}

    fingerprint = parser.extract_fingerprint("tly_pdfs/TLY_2026_HB35.pdf")
    # ReportFingerprint(cadence="HAFTALIK", total_value=247632508542.43, ...)
    # -- the evidence `report_dating` uses to date the report, since the
    # document's own period label is not reliable (see ReportFingerprint).
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pdfplumber


# Cadence labels used by `ReportFingerprint.cadence` -- derived from the
# report's OWN wording ("Haftalık Pay Fiyatı" vs "Aylık Pay Fiyatı"),
# independently of whatever cadence KAP's `period` metadata claims.
CADENCE_WEEKLY = "HAFTALIK"
CADENCE_MONTHLY = "AYLIK"


@dataclass
class ReportFingerprint:
    """Everything a Portfoy Dagilim Raporu PDF says about WHEN it is from
    -- collected in one pass so the report's true as-of date can be
    established without trusting its (demonstrably wrong) period label.

    Why this exists (2026-09-14): funds moved to WEEKLY portfolio
    disclosure, but the documents kept their monthly-era labelling. TLY's
    report published 09.09.2026 is headed "Ağustos-2026", its KAP
    attachment is named "TLY_2026.08.pdf", and its `donem` metadata is a
    week number (35) -- yet its holdings are valued at the 04.09.2026
    BIST close and include positions bought on 01-04 September. Every
    label on the document points at August; every piece of DATA in it
    points at September. So dating a report now means reading its data,
    not its label (see `report_dating.resolve_as_of_date`).

    The three header figures are the load-bearing ones: `total_value`,
    `share_count` and `unit_price` appear verbatim in TEFAS's own daily
    series for the same fund, so matching all three pins the report to a
    single TEFAS day exactly. `newest_purchase_date` is an independent
    cross-check -- a position bought on date D proves the snapshot is
    from D or later -- and `valuation_prices` are the per-stock prices
    the report itself used, which match that day's real BIST closes.

    Every field is Optional: a PDF that omits (or renders unparseably)
    any of them yields None for that field rather than raising, and the
    caller decides whether what's left is enough to date the report.
    """

    source_path: str
    cadence: Optional[str] = None            # CADENCE_WEEKLY / CADENCE_MONTHLY
    declared_period_label: Optional[str] = None   # e.g. "Ağustos-2026" (unreliable)
    total_value: Optional[float] = None     # "Toplam Değer/Net Varlık Değeri"
    share_count: Optional[float] = None     # "Katılma Payı Sayısı"
    unit_price: Optional[float] = None      # "Haftalık/Aylık Pay Fiyatı (TL)"
    previous_unit_price: Optional[float] = None   # "Önceki Hafta/Ay Pay Fiyatı (TL)"
    newest_purchase_date: Optional[date] = None
    valuation_prices: Dict[str, float] = field(default_factory=dict)

    @property
    def header_figures(self) -> Dict[str, float]:
        """The header figures that are actually present, keyed by the
        TEFAS field each one corresponds to -- this mapping is what
        `report_dating` matches against TEFAS's daily records."""
        pairs = {
            "ToplamDeger": self.total_value,
            "Pay": self.share_count,
            "Fiyat": self.unit_price,
        }
        return {key: value for key, value in pairs.items() if value is not None}


class KAPPdfParser:
    """Extracts the "HISSE SENETLERI" (equities) holdings table out of a
    KAP Portfoy Dagilim Raporu PDF (weekly or monthly).

    Why this needs more than a naive `page.extract_tables()` call:

    - The report has NO visible ruling/grid lines, so pdfplumber's default
      line-based table strategy finds zero tables on the pages that
      actually contain the holdings data. A text-position-based strategy
      (`vertical_strategy="text", horizontal_strategy="text"`) is used
      instead, cropped to just the portfolio table region (anchored on the
      repeating "VADEYE" header word) so the unrelated prose section above
      it on page 1 doesn't distort the column boundaries.
    - Even with that, pdfplumber's column boundaries are NOT stable: the
      same logical "NOMINAL DEGER" (lot count) column lands at a different
      cell index on almost every page, because column splits are inferred
      purely from text x-positions, which shift slightly page to page.
      Rather than trust a fixed column index, each row's cells (after the
      ticker) are scanned left-to-right for the first cell that matches a
      strict Turkish thousands-grouped number pattern -- this is reliably
      the "NOMINAL DEGER" field, since it's always the first true quantity
      column for an equity row (ISIN codes contain letters, and adjacent
      empty faiz/odeme columns for stocks contain nothing, so they're
      naturally skipped).
    - Section headers ("HISSE SENETLERI", "BORCLANMA SENETLERI") are also
      frequently split across multiple cells by the same column-boundary
      guessing (e.g. "HISSE SENETL" + "ERI"), so detection joins every
      cell in a row together before searching for the marker text.
    """

    # Sub-heading of interest; everything between this and the first
    # terminating marker below is treated as equity holdings data.
    SECTION_START_MARKER = "HİSSE SENETLERİ"

    # Either of these appearing (in the same "joined row" sense) after we
    # started collecting equities means the equities block has ended --
    # "BORÇLANMA" is the next major asset class (debt instruments), and
    # "GRUP TOPLAMI" is the equities section's own closing subtotal line.
    SECTION_END_MARKERS = ("BORÇLANMA", "GRUP TOPLAMI")

    # The portfolio table's repeating page header always starts with this
    # word; cropping the page from here down removes the unrelated prose
    # section (fund info, performance stats) that otherwise pollutes
    # pdfplumber's text-based column detection.
    HEADER_ANCHOR_WORD = "VADEYE"

    TABLE_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text"}

    # Strict Turkish thousands-grouped number: an optional leading minus,
    # 1-3 leading digits, zero or more ".XXX" groups, and an optional
    # ",XXX" decimal tail. Deliberately strict (rather than "any digits")
    # so plain integer codes with no separators at all -- e.g. a borsa
    # kodu like "80100511" -- never get mistaken for the Nominal Deger
    # quantity, since real amounts in this report are always rendered
    # with proper TR grouping.
    NUMBER_PATTERN = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$")

    # Matches the period slug in a filename produced by
    # `KAPPdfDownloader`, so `parse_directory` can key its results without
    # depending on any particular fund code prefix:
    #
    #   "TLY_2026_HB35.pdf" -> "2026_HB35"   (weekly, KAP period="HB")
    #   "THF_2026_AB08.pdf" -> "2026_AB08"   (monthly, KAP period="AB")
    #   "TLY_2026_08.pdf"   -> "2026_08"     (legacy, pre-2026-09-14 files)
    #
    # The optional letter prefix is what keeps two WEEKLY reports from the
    # same calendar month distinguishable -- under the old bare-"YYYY_MM"
    # scheme they collided on one key and one filename (see
    # `ReportFingerprint`).
    PERIOD_FILENAME_PATTERN = re.compile(r"(\d{4})_([A-Z]{0,2}\d{2})")

    # --- Report dating (see `ReportFingerprint`) -----------------------------

    # Unambiguous page-1 header figures, e.g.
    # "D-)Toplam Değer/Net Varlık Değeri : 247.632.508.542,43".
    _HEADER_FIGURE_PATTERNS = {
        "total_value": re.compile(r"Toplam\s+De[ğg]er\s*/\s*Net\s+Varl[ıi]k\s+De[ğg]eri\s*:\s*([\d.,]+)"),
        "share_count": re.compile(r"Kat[ıi]lma\s+Pay[ıi]\s+Say[ıi]s[ıi]\s*:\s*([\d.,]+)"),
    }

    # The fund's unit price, whose LABEL also states the report's cadence:
    #
    #   weekly : "A-)Haftalık Pay Fiyatı (TL) : 9.495,638007"
    #   monthly: "A-)Ay Sonu Pay Fiyatı (TL) : 2,724426"
    #
    # Note the monthly wording -- "Ay Sonu" (month END) -- which is the
    # document itself confirming that a monthly report is a month-end
    # snapshot, whereas a weekly one is simply "Haftalık" with no such
    # anchor. `(TL)` is required because USD-quoted funds (e.g. TMM) print
    # a second, dot-decimal "(USD)" line right underneath.
    _UNIT_PRICE_PATTERN = re.compile(
        r"(Haftal[ıi]k|Ay\s+Sonu|Ayl[ıi]k)\s+Pay\s+Fiyat[ıi]\s*\(TL\)\s*:\s*([\d.,]+)"
    )
    _PREVIOUS_PRICE_PATTERN = re.compile(
        r"[ÖO]nceki\s+(?:Hafta|Ay)\s+Pay\s+Fiyat[ıi]?\s*\(TL\)\s*:\s*([\d.,]+)"
    )

    # Cadence fallback for a report whose price line can't be read at all:
    # the "... Ortalama Portföydeki Menkul Kıymetler Yüzdesi" heading is
    # worded per cadence too.
    _CADENCE_FALLBACK_PATTERN = re.compile(r"(Haftal[ıi]k|Ayl[ıi]k)\s+Ortalama\s+Portf[öo]y")

    # One equity holding row, e.g.
    #   "AKBNK TL AKBANK TRAAKBNK91N6 3.639.400,00 66,953631 14/08/26 80100511 71,850000 261.490.890,00 ..."
    #    ticker      issuer   ISIN     nominal      birim alış  SATIN ALIŞ  hesap   BORSA FİYATI  toplam değer
    #
    # Only rows whose `nominal x borsa fiyatı == toplam değer` are accepted
    # (see `_scan_equity_rows`), which is what makes this text-position
    # regex safe on a borderless PDF: an accidental column mis-read fails
    # the arithmetic and is discarded rather than silently producing a
    # wrong date or price.
    _EQUITY_ROW_PATTERN = re.compile(
        r"^([A-Z]{4,6})\s+TL\s+.*?"
        r"([\d.]+,\d+)\s+"              # nominal değer (lot)
        r"[\d.]+,\d+\s+"                # birim alış fiyatı
        r"(\d{2})/(\d{2})/(\d{2})\s+"   # satın alış tarihi
        r"\d+\s+"                       # hesap / portföy kodu
        r"([\d.]+,\d+)\s+"              # BORSA FİYATI (değerleme fiyatı)
        r"(-?[\d.]+,\d+)"               # toplam değer
    )

    def __init__(self, verbose: bool = True):
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    # --- Public entry points -------------------------------------------------

    def parse_file(self, filepath: str) -> Dict[str, float]:
        """Parses a single Portfoy Dagilim Raporu PDF and returns a
        {hisse_kodu: toplam_lot} dictionary built from the "HISSE
        SENETLERI" section only. If the same ticker appears on multiple
        rows (bought at different prices), its lot amounts are summed.

        Returns an empty dict (with a console warning) if the file can't
        be opened or the equities section can't be found -- this method
        never raises for malformed/unexpected PDF content.
        """
        holdings: Dict[str, float] = {}

        try:
            with pdfplumber.open(filepath) as pdf:
                collecting = False
                finished = False

                for page in pdf.pages:
                    if finished:
                        break

                    for row in self._extract_rows(page):
                        joined = "".join(cell or "" for cell in row).strip()

                        if not collecting:
                            if self.SECTION_START_MARKER in joined:
                                collecting = True
                            continue

                        if any(marker in joined for marker in self.SECTION_END_MARKERS):
                            collecting = False
                            finished = True
                            break

                        self._accumulate_row(row, holdings)

        except FileNotFoundError:
            self._log(f"[HATA] Dosya bulunamadı: {filepath}")
            return {}
        except Exception as exc:  # noqa: BLE001 - a single bad PDF must never crash a batch run
            self._log(f"[HATA] '{filepath}' işlenirken beklenmeyen hata: {exc}")
            return {}

        if not holdings:
            self._log(f"[UYARI] '{filepath}' içinde '{self.SECTION_START_MARKER}' verisi bulunamadı.")
        else:
            self._log(f"[BİLGİ] '{os.path.basename(filepath)}': {len(holdings)} hisse kodu ayrıştırıldı.")

        return holdings

    def parse_directory(self, dirpath: str) -> Dict[str, Dict[str, float]]:
        """Parses every "*.pdf" file in `dirpath` and returns a nested
        dictionary keyed by the period slug parsed out of each filename
        (see `PERIOD_FILENAME_PATTERN`), e.g.:

            {"2026_AB07": {"ALKLC": 731256.0, ...}, "2026_HB35": {...}}

        A period slug identifies WHICH DISCLOSURE a file came from; it is
        not a date and must never be treated as one. The report's actual
        as-of date is established from its contents instead, by
        `report_dating.resolve_as_of_date`.

        Files whose name carries no recognizable slug (as produced by
        `KAPPdfDownloader`) are skipped with a console warning rather than
        raising.
        """
        if not os.path.isdir(dirpath):
            self._log(f"[HATA] Klasör bulunamadı: {dirpath}")
            return {}

        results: Dict[str, Dict[str, float]] = {}
        pdf_filenames = sorted(f for f in os.listdir(dirpath) if f.lower().endswith(".pdf"))

        if not pdf_filenames:
            self._log(f"[UYARI] '{dirpath}' içinde hiç PDF dosyası bulunamadı.")
            return {}

        for filename in pdf_filenames:
            period_key = self._extract_period_key(filename)
            if period_key is None:
                self._log(f"[UYARI] '{filename}' dosya adından dönem kimliği (YYYY_HB35 / YYYY_AB08) çıkarılamadı, atlanıyor.")
                continue

            filepath = os.path.join(dirpath, filename)
            results[period_key] = self.parse_file(filepath)

        return results

    # --- Report dating ---------------------------------------------------------

    def extract_fingerprint(self, filepath: str) -> ReportFingerprint:
        """Reads everything the PDF says about its own date into a
        `ReportFingerprint` (see that dataclass for why label-based dating
        no longer works at all).

        Deliberately uses raw `page.extract_text()` lines rather than the
        table-extraction machinery `parse_file` needs: the header figures
        live in the prose block ABOVE the holdings table (which
        `_extract_rows` crops away on purpose), and the equity rows are
        matched by a self-validating regex instead of by column position
        (see `_EQUITY_ROW_PATTERN`).

        Never raises: an unreadable file, a missing header line, or a
        holdings table whose columns don't parse all leave the
        corresponding field as None, and the caller decides whether the
        remainder is enough to date the report.
        """
        fingerprint = ReportFingerprint(source_path=filepath)

        try:
            with pdfplumber.open(filepath) as pdf:
                pages_text = [page.extract_text() or "" for page in pdf.pages]
        except FileNotFoundError:
            self._log(f"[HATA] Dosya bulunamadı: {filepath}")
            return fingerprint
        except Exception as exc:  # noqa: BLE001 - a single bad PDF must never crash a batch run
            self._log(f"[HATA] '{filepath}' parmak izi okunurken beklenmeyen hata: {exc}")
            return fingerprint

        if not pages_text:
            return fingerprint

        first_page = pages_text[0]
        self._read_header_figures(first_page, fingerprint)
        self._scan_equity_rows(pages_text, fingerprint)
        return fingerprint

    def _read_header_figures(self, first_page: str, fingerprint: ReportFingerprint) -> None:
        """Fills in the page-1 header fields (cadence, declared period
        label and the three TEFAS-matchable figures) on `fingerprint`."""
        lines = [line.strip() for line in first_page.split("\n") if line.strip()]

        # The declared period is always the 2nd line ("Ağustos-2026").
        # Recorded for the audit trail ONLY -- it is the very label that
        # proved untrustworthy, never an input to any date calculation.
        if len(lines) > 1:
            fingerprint.declared_period_label = lines[1]

        for field_name, pattern in self._HEADER_FIGURE_PATTERNS.items():
            match = pattern.search(first_page)
            if not match:
                continue
            try:
                setattr(fingerprint, field_name, self._turkish_str_to_float(match.group(1)))
            except ValueError:
                continue

        # The price lines are matched per LINE rather than across the whole
        # page, so that "Önceki Ay Pay Fiyatı" (the PREVIOUS period's
        # price) and "Aylık Pay Fiyatı Artış Oranı" (a percentage, not a
        # price) can never be mistaken for the current unit price.
        for line in lines:
            if "Fiyat" not in line:
                continue

            if fingerprint.previous_unit_price is None:
                previous = self._PREVIOUS_PRICE_PATTERN.search(line)
                if previous:
                    try:
                        fingerprint.previous_unit_price = self._turkish_str_to_float(previous.group(1))
                    except ValueError:
                        pass
                    continue

            if fingerprint.unit_price is not None:
                continue
            if "Önceki" in line or "Artış" in line or "Artis" in line:
                continue
            current = self._UNIT_PRICE_PATTERN.search(line)
            if not current:
                continue
            try:
                fingerprint.unit_price = self._turkish_str_to_float(current.group(2))
            except ValueError:
                continue
            fingerprint.cadence = (
                CADENCE_WEEKLY if current.group(1).lower().startswith("haftal") else CADENCE_MONTHLY
            )

        if fingerprint.cadence is None:
            fallback = self._CADENCE_FALLBACK_PATTERN.search(first_page)
            if fallback:
                fingerprint.cadence = (
                    CADENCE_WEEKLY if fallback.group(1).lower().startswith("haftal") else CADENCE_MONTHLY
                )

    def _scan_equity_rows(self, pages_text: List[str], fingerprint: ReportFingerprint) -> None:
        """Collects the newest "Satın Alış Tarihi" and the per-stock
        valuation prices ("Borsa Fiyatı") across every equity row.

        A row only counts if `nominal x borsa fiyatı == toplam değer`
        holds -- on a borderless PDF the column split can drift, and this
        arithmetic check is what distinguishes a correctly-read row from a
        mis-read one. Silently dropping the mis-reads is safe here because
        both outputs are corroborating evidence: the newest purchase date
        is a LOWER bound on the snapshot date, so missing a row can only
        ever make the bound weaker, never wrong.
        """
        for page_text in pages_text:
            for line in page_text.split("\n"):
                match = self._EQUITY_ROW_PATTERN.match(line.strip())
                if not match:
                    continue

                ticker, raw_nominal, day, month, year, raw_price, raw_total = match.groups()
                try:
                    nominal = self._turkish_str_to_float(raw_nominal)
                    price = self._turkish_str_to_float(raw_price)
                    total = self._turkish_str_to_float(raw_total)
                except ValueError:
                    continue

                # Columns read correctly only if the row's own arithmetic holds.
                if abs(nominal * price - total) > max(abs(total) * 1e-6, 1.0):
                    continue

                fingerprint.valuation_prices.setdefault(ticker, price)

                try:
                    purchased = date(2000 + int(year), int(month), int(day))
                except ValueError:
                    continue
                if fingerprint.newest_purchase_date is None or purchased > fingerprint.newest_purchase_date:
                    fingerprint.newest_purchase_date = purchased

    # --- Row extraction --------------------------------------------------------

    def _extract_rows(self, page) -> List[list]:
        """Crops the page down to the portfolio table region (see
        `HEADER_ANCHOR_WORD`) and returns every row from every table
        pdfplumber finds there, using the text-position-based strategy
        this report's borderless layout requires. Falls back to the
        un-cropped page if the anchor word isn't found (defensive; every
        page observed in practice repeats the table header).
        """
        try:
            words = page.extract_words()
        except Exception:
            words = []

        anchor = next((w for w in words if w.get("text") == self.HEADER_ANCHOR_WORD), None)
        region = page.crop((0, anchor["top"], page.width, page.height)) if anchor else page

        try:
            tables = region.extract_tables(table_settings=self.TABLE_SETTINGS)
        except Exception as exc:
            self._log(f"[UYARI] Sayfa tablo olarak ayrıştırılamadı: {exc}")
            return []

        rows: List[list] = []
        for table in tables:
            rows.extend(table)
        return rows

    # --- Row -> holdings accumulation -------------------------------------------

    def _accumulate_row(self, row: list, holdings: Dict[str, float]) -> None:
        """Attempts to interpret one table row as a single equity holding
        line and, if successful, adds its lot amount into `holdings`
        (summing if the ticker already has an entry). Any row that isn't a
        genuine data row -- a wrapped issuer-name continuation line, a
        "Hisse Turk"/"Hisse Yabanci" sub-heading, a blank spacer row, or
        anything unexpected -- is safely skipped via try/except, exactly
        as if it were empty/NaN.
        """
        try:
            ticker = (row[0] or "").strip()
            if not ticker:
                return

            nominal_lot = self._find_nominal_value(row[1:])
            if nominal_lot is None:
                return

            holdings[ticker] = holdings.get(ticker, 0.0) + nominal_lot
        except (IndexError, TypeError, ValueError):
            return

    def _find_nominal_value(self, cells: List[str]) -> Optional[float]:
        """Scans the given cells left-to-right and returns the first one
        that parses as a Turkish-formatted number (see `NUMBER_PATTERN`)
        -- for an equity row, this is always the "NOMINAL DEGER" (lot
        count) field, since it's the first genuine quantity column after
        the ticker/currency/issuer/ISIN text (which never match the strict
        numeric pattern). Returns None if no cell matches.
        """
        for cell in cells:
            cell = (cell or "").strip()
            if not cell or not self.NUMBER_PATTERN.match(cell):
                continue
            try:
                return self._turkish_str_to_float(cell)
            except ValueError:
                continue
        return None

    @staticmethod
    def _turkish_str_to_float(value: str) -> float:
        """Converts a Turkish-formatted number string (e.g. "3.110.498,00"
        or "-200.000,00") to a float by dropping the "." thousands
        separators and converting the "," decimal separator to ".".
        """
        cleaned = value.strip().replace(".", "").replace(",", ".")
        return float(cleaned)

    def _extract_period_key(self, filename: str) -> Optional[str]:
        """Pulls the period slug out of a filename, e.g.
        "TLY_2026_HB35.pdf" -> "2026_HB35" (and the legacy
        "TLY_2026_03.pdf" -> "2026_03"). Returns None if no such pattern
        is present. See `PERIOD_FILENAME_PATTERN`.
        """
        match = self.PERIOD_FILENAME_PATTERN.search(filename)
        if not match:
            return None
        return f"{match.group(1)}_{match.group(2)}"


# --- Manual verification export ---------------------------------------------
#
# `KAPPdfParser` never raises for bad input and always prints a summary to
# the console, but financial data shouldn't be trusted on terminal output
# alone -- `export_to_html` is a pure, additive export step (it doesn't
# touch any parsing logic above) that renders the same data as a clean,
# eyeball-checkable standalone HTML report.

def _format_turkish_number(value: float) -> str:
    """Formats a float as a Turkish-style number string, e.g. 731256.0 ->
    "731.256,00" (dot as thousands separator, comma as decimal point).
    """
    # Format with US grouping first ("731,256.00"), then swap separators.
    us_formatted = f"{value:,.2f}"
    return us_formatted.replace(",", "\u0001").replace(".", ",").replace("\u0001", ".")


def _format_tl_compact(value: float) -> str:
    """Formats a TL amount in a human-scannable compact form for the TEFAS
    Aktif Güç matrix, where raw values routinely run into the hundreds of
    billions and a full thousands-grouped number would be hard to eyeball
    across a wide multi-fund table:

        231802010766.38  -> "231,80 Milyar TL"
        454324080.94     -> "454,32 Milyon TL"
        850000.0         -> "850.000,00 TL"

    Falls back to the full Turkish-formatted number (+ " TL") below one
    million, where a "Milyon"/"Milyar" suffix would lose too much
    precision to be useful.
    """
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{_format_turkish_number(value / 1_000_000_000)} Milyar TL"
    if abs_value >= 1_000_000:
        return f"{_format_turkish_number(value / 1_000_000)} Milyon TL"
    return f"{_format_turkish_number(value)} TL"


def _format_signed_number(value: float) -> str:
    """Formats a lot delta with an explicit leading sign, e.g. 42469924.0
    -> "+42.469.924,00", -12000000.0 -> "-12.000.000,00", 0.0 ->
    "0,00" (no sign for an exact zero -- there's no meaningful direction
    to show). Used for the "Kesinleşen Delta Lot" / "Oransal Tahmini Delta
    Lot" columns of the portfolio evolution table, so a reader can tell a
    buy from a sell at a glance without also reading a separate direction
    column.
    """
    if value > 0:
        return f"+{_format_turkish_number(value)}"
    if value < 0:
        return f"-{_format_turkish_number(abs(value))}"
    return _format_turkish_number(0.0)


def _format_pct_signed(value: float) -> str:
    """Formats a percentage change with an explicit leading "+" for
    positive values (negative values already carry their own "-" through
    `_format_turkish_number`), e.g. 12.4532 -> "+12,45%", -8.3 ->
    "-8,30%".
    """
    formatted = _format_turkish_number(value)
    return f"+{formatted}%" if value > 0 else f"{formatted}%"


def _date_sort_key(date_str: Optional[str]) -> Tuple[int, int, int]:
    """Parses a "DD/MM/YYYY" (KAP's own separator) or "DD.MM.YYYY"
    (this module's display separator, see `_format_display_date`) string
    into a `(year, month, day)` tuple so transaction histories can be
    sorted chronologically regardless of which separator the caller used.

    Never raises: anything that isn't a clean 3-part date (missing,
    malformed, non-numeric parts) sorts as `(0, 0, 0)` -- i.e. first,
    never crashing the sort and never silently dropping the entry.
    """
    if not date_str:
        return (0, 0, 0)
    parts = re.split(r"[/.]", str(date_str).strip())
    if len(parts) != 3:
        return (0, 0, 0)
    try:
        day, month, year = parts
        return (int(year), int(month), int(day))
    except ValueError:
        return (0, 0, 0)


def _format_display_date(date_str: Optional[str]) -> str:
    """Normalizes a "DD/MM/YYYY" date (KAP's own separator, as stored in
    `resolved`/`proportionally_resolved` items) to "DD.MM.YYYY" for
    display in the transaction-history list, matching this report's
    other Turkish-formatted values. Passed through as-is (never raises)
    if it doesn't look like a plain slash-separated date."""
    return str(date_str or "").replace("/", ".")


def _format_baseline_as_of(value) -> Optional[str]:
    """Formats the baseline report's MEASURED valuation date (see
    `kap_delta_engine.date_baseline_report`) as "DD.MM.YYYY" for the
    report's descriptive text, accepting either a `date`/`datetime` or an
    ISO "YYYY-MM-DD" string so the caller can pass whatever it has.

    Returns `None` -- not a fabricated date -- when the value is missing
    or unparseable, so the surrounding copy can fall back to naming the
    baseline PDF instead of stating a date the pipeline never established.
    """
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return None


def _aggregate_signed_lot_deltas(
    resolved: List[dict], proportional: List[dict]
) -> "tuple[Dict[str, float], Dict[str, float], Dict[str, List[dict]]]":
    """Reduces the "Kesinleşen Deltalar" (`resolved`) and "Oransal Olarak
    Dağıtılan" (`proportional`) lists -- each potentially containing
    several disclosures/dates for the SAME ticker -- into:

    1. `resolved_by_ticker` / `proportional_by_ticker`: one signed,
       per-ticker NET total for each, for the portfolio evolution table's
       cumulative columns (unchanged behavior from before this function
       also tracked history).
    2. `transaction_history`: per-ticker, a chronologically-sorted
       (oldest first) list of EVERY individual entry that fed into those
       totals -- `{"date": "23/07/2026", "lot": 42469924.0, "type":
       "Kesinleşen"}` -- so the report can show not just the net result
       but the day-by-day story behind it (see `_render_portfolio_
       evolution_table`'s "İşlem Tarihçesi" column).

    `resolved` items store an UNSIGNED `lot` magnitude plus a separate
    `direction` string (see `kap_delta_engine.ResolvedDelta`'s own
    docstring for why), so the sign has to be reconstructed here:
    "ALIM" -> +, "SATIM" -> -, anything else (e.g. "DEGISIM YOK") -> 0.
    `proportional` items store an already-SIGNED `estimated_lot` (see
    `kap_delta_engine.ProportionalResolution`) and are used as-is.

    Never raises on a malformed entry -- a missing/non-numeric `lot` or
    `estimated_lot`, or a ticker-less item, is simply skipped for that one
    entry rather than aborting the whole aggregation.
    """
    resolved_by_ticker: Dict[str, float] = {}
    transaction_history: Dict[str, List[dict]] = {}

    for item in resolved or []:
        ticker = str(item.get("ticker") or "").strip()
        if not ticker:
            continue
        try:
            lot = float(item.get("lot") or 0.0)
        except (TypeError, ValueError):
            continue
        direction = item.get("direction")
        signed_lot = lot if direction == "ALIM" else -lot if direction == "SATIM" else 0.0
        resolved_by_ticker[ticker] = resolved_by_ticker.get(ticker, 0.0) + signed_lot
        transaction_history.setdefault(ticker, []).append(
            {"date": item.get("date"), "lot": signed_lot, "type": "Kesinleşen"}
        )

    proportional_by_ticker: Dict[str, float] = {}
    for item in proportional or []:
        ticker = str(item.get("ticker") or "").strip()
        if not ticker:
            continue
        try:
            estimated_lot = float(item.get("estimated_lot") or 0.0)
        except (TypeError, ValueError):
            continue
        proportional_by_ticker[ticker] = proportional_by_ticker.get(ticker, 0.0) + estimated_lot
        transaction_history.setdefault(ticker, []).append(
            {"date": item.get("date"), "lot": estimated_lot, "type": "Oransal"}
        )

    for ticker_history in transaction_history.values():
        ticker_history.sort(key=lambda entry: _date_sort_key(entry.get("date")))

    return resolved_by_ticker, proportional_by_ticker, transaction_history


def _render_transaction_history_cell(history: List[dict]) -> str:
    """Renders one ticker's `transaction_history` entries (see
    `_aggregate_signed_lot_deltas`) as a collapsed, click-to-expand
    `<details>`/`<summary>` disclosure for the evolution table's "İşlem
    Tarihçesi" column -- so the cumulative delta columns next to it stay
    scannable while the full day-by-day story is still one click away,
    never bloating the table by default.

    Returns a plain "İşlem Yok" text (no `<details>` element at all) for
    a ticker with an empty history -- e.g. `SELEC`, whose Kesinleşen AND
    Oransal deltas both happened to be zero -- so there's nothing
    misleadingly "expandable" with zero content inside.
    """
    if not history:
        return '<span class="no-history">İşlem Yok</span>'

    items_html = "".join(
        f"""
              <li>{html.escape(_format_display_date(entry.get('date')))}: """
        f"""{html.escape(_format_signed_number(entry.get('lot') or 0.0))} """
        f"""({html.escape(str(entry.get('type', '')))})</li>"""
        for entry in history
    )

    count = len(history)
    label = f"{count} İşlem Göster"
    return f"""<details class="history-details">
              <summary>{html.escape(label)}</summary>
              <ul class="history-list">{items_html}
              </ul>
            </details>"""


def _build_evolution_chart_payload(rows: List[tuple]) -> dict:
    """Builds the Chart.js payload for the portfolio-evolution section
    from the same row tuples `_render_portfolio_evolution_table` already
    computed -- purely presentational; no engine recalculation.

    Each row is
    `(ticker, baseline_lot, resolved_delta, proportional_delta, current_lot, price, weight_pct)`.

    - `weights`: top-10 tickers by Güncel Ağırlık (%), remainder collapsed
      into a single "Diğerleri" slice (only rows with a known weight).
    - `deltas`: finite, non-zero "Taban Tarihinden Beri Lot Değişimi (%)"
      values only (baseline_lot > 0 and pct != 0). Empty list means the
      bar chart should show the no-delta placeholder instead.
    """
    weight_rows = [
        (ticker, float(weight_pct))
        for ticker, _b, _r, _p, _c, _price, weight_pct in rows
        if weight_pct is not None
    ]
    weight_rows.sort(key=lambda item: item[1], reverse=True)

    weight_labels: List[str] = []
    weight_values: List[float] = []
    if weight_rows:
        top = weight_rows[:10]
        rest = weight_rows[10:]
        weight_labels = [ticker for ticker, _ in top]
        weight_values = [round(value, 4) for _, value in top]
        if rest:
            weight_labels.append("Diğerleri")
            weight_values.append(round(sum(value for _, value in rest), 4))

    delta_labels: List[str] = []
    delta_values: List[float] = []
    for ticker, baseline_lot, _r, _p, current_lot, _price, _w in rows:
        if not baseline_lot:
            continue
        pct = ((current_lot - baseline_lot) / baseline_lot) * 100.0
        if pct == 0:
            continue
        delta_labels.append(ticker)
        delta_values.append(round(pct, 4))

    # Largest absolute movers first -- easier to scan than table order.
    if delta_labels:
        paired = sorted(zip(delta_labels, delta_values), key=lambda item: abs(item[1]), reverse=True)
        delta_labels = [ticker for ticker, _ in paired]
        delta_values = [value for _, value in paired]

    return {
        "weights": {"labels": weight_labels, "values": weight_values},
        "deltas": {"labels": delta_labels, "values": delta_values},
    }


def _render_evolution_charts(chart_payload: dict) -> str:
    """Renders the two Chart.js canvases (weight donut + since-baseline
    lot-change bars) above the portfolio evolution table. Injects
    `chartData` via `json.dumps` so the browser-side script needs no
    further Python. If every lot-change % is zero, the bar chart is
    replaced by an explicit HTML placeholder (no empty canvas).
    """
    chart_json = json.dumps(chart_payload, ensure_ascii=False)
    has_deltas = bool(chart_payload.get("deltas", {}).get("labels"))
    has_weights = bool(chart_payload.get("weights", {}).get("labels"))

    weight_body = (
        '<canvas id="weightChart" aria-label="Güncel Ağırlık Dağılımı"></canvas>'
        if has_weights
        else '<p class="chart-placeholder">Güncel ağırlık verisi bulunamadı (fiyat veya AUM eksik).</p>'
    )
    delta_body = (
        '<canvas id="deltaChart" aria-label="Taban Tarihinden Beri Lot Değişimi"></canvas>'
        if has_deltas
        else '<p class="chart-placeholder">Taban raporunun veri tarihinden beri yeni işlem (delta) bulunmamaktadır.</p>'
    )

    return f"""
      <div class="charts-row">
        <div class="chart-card">
          <h3>Güncel Ağırlık Dağılımı</h3>
          <div class="chart-canvas-wrap">{weight_body}</div>
        </div>
        <div class="chart-card">
          <h3>Taban Tarihinden Beri Lot Değişimi (%)</h3>
          <div class="chart-canvas-wrap">{delta_body}</div>
        </div>
      </div>
      <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
      <script>
        const chartData = {chart_json};
        (function () {{
          const weight = chartData.weights || {{ labels: [], values: [] }};
          const deltas = chartData.deltas || {{ labels: [], values: [] }};
          const weightCanvas = document.getElementById("weightChart");
          if (weightCanvas && weight.labels.length) {{
            new Chart(weightCanvas, {{
              type: "doughnut",
              data: {{
                labels: weight.labels,
                datasets: [{{
                  data: weight.values,
                  backgroundColor: [
                    "#3b82f6", "#14b8a6", "#d97706", "#8b5cf6", "#f87171",
                    "#0ea5e9", "#10b981", "#a78bfa", "#f59e0b", "#64748b",
                    "#475569"
                  ],
                  borderWidth: 2,
                  borderColor: "#1e293b"
                }}]
              }},
              options: {{
                responsive: true,
                maintainAspectRatio: false,
                color: "#cbd5e1",
                plugins: {{
                  legend: {{
                    position: "bottom",
                    labels: {{
                      boxWidth: 12,
                      font: {{ size: 11 }},
                      color: "#94a3b8",
                      padding: 12
                    }}
                  }},
                  title: {{ color: "#cbd5e1" }},
                  tooltip: {{
                    backgroundColor: "rgba(15, 23, 42, 0.92)",
                    titleColor: "#e2e8f0",
                    bodyColor: "#cbd5e1",
                    borderColor: "#334155",
                    borderWidth: 1,
                    callbacks: {{
                      label: function (ctx) {{
                        const v = ctx.parsed;
                        return ctx.label + ": %" + (typeof v === "number" ? v.toFixed(2) : v);
                      }}
                    }}
                  }}
                }}
              }}
            }});
          }}
          const deltaCanvas = document.getElementById("deltaChart");
          if (deltaCanvas && deltas.labels.length) {{
            // Mat semantic fills: emerald-400 / red-400 (no neon)
            const colors = deltas.values.map(function (v) {{
              return v > 0 ? "rgba(52, 211, 153, 0.75)" : "rgba(248, 113, 113, 0.75)";
            }});
            const borders = deltas.values.map(function (v) {{
              return v > 0 ? "#34d399" : "#f87171";
            }});
            new Chart(deltaCanvas, {{
              type: "bar",
              data: {{
                labels: deltas.labels,
                datasets: [{{
                  label: "Taban Tarihinden Beri Lot Değişimi (%)",
                  data: deltas.values,
                  backgroundColor: colors,
                  borderColor: borders,
                  borderWidth: 1
                }}]
              }},
              options: {{
                responsive: true,
                maintainAspectRatio: false,
                color: "#cbd5e1",
                plugins: {{
                  legend: {{
                    display: false,
                    labels: {{ color: "#94a3b8" }}
                  }},
                  title: {{ color: "#cbd5e1" }},
                  tooltip: {{
                    backgroundColor: "rgba(15, 23, 42, 0.92)",
                    titleColor: "#e2e8f0",
                    bodyColor: "#cbd5e1",
                    borderColor: "#334155",
                    borderWidth: 1,
                    callbacks: {{
                      label: function (ctx) {{
                        const v = ctx.parsed.y;
                        const sign = v > 0 ? "+" : "";
                        return sign + v.toFixed(2) + "%";
                      }}
                    }}
                  }}
                }},
                scales: {{
                  x: {{
                    ticks: {{ maxRotation: 45, minRotation: 0, font: {{ size: 11 }}, color: "#94a3b8" }},
                    grid: {{ color: "rgba(148, 163, 184, 0.12)" }},
                    border: {{ color: "rgba(148, 163, 184, 0.25)" }}
                  }},
                  y: {{
                    ticks: {{
                      color: "#94a3b8",
                      callback: function (value) {{ return value + "%"; }}
                    }},
                    grid: {{ color: "rgba(148, 163, 184, 0.12)" }},
                    border: {{ color: "rgba(148, 163, 184, 0.25)" }}
                  }}
                }}
              }}
            }});
          }}
        }})();
      </script>
"""


def _render_portfolio_evolution_table(
    baseline_data: Dict[str, float],
    resolved: List[dict],
    proportional: List[dict],
    updated_data: Dict[str, float],
    current_prices: Optional[Dict[str, float]] = None,
    current_aum: Optional[float] = None,
) -> str:
    """Renders the "Hisse Bazlı Portföy Evrimi (Lot Değişim Özeti)" table:
    one row per ticker showing its FULL journey from the KAP PDF baseline
    to the current estimated holding -- Başlangıç Lot, Kesinleşen Delta
    Lot (net, signed), Oransal Tahmini Delta Lot (net, signed), Güncel
    Tahmini Lot, and a "Taban Tarihinden Beri Lot Değişimi (%)" that puts
    every other column into context (a "-69 milyon lot" delta means
    nothing on its own; "-69 milyon lot, başlangıcın %35'i" does). This
    percentage is intentionally measured only from the baseline PDF's own
    valuation date onward -- NOT a long-term trend (see the bold warning
    rendered above the table). Since KAP moved these reports to a WEEKLY
    cadence (2026-09) that window can be as short as a few days, which is
    why neither the column nor the warning says "ay" anymore.

    `Güncel Tahmini Lot` is computed directly as `Başlangıç + Kesinleşen +
    Oransal` (not read from `updated_data`) so the table is internally
    self-consistent and the formula is transparent; in practice this
    always matches `updated_data[ticker]` since that's exactly how
    `apply_delta`/`resolve_multi_fund_deltas` built it. `updated_data` is
    still accepted as a parameter so a ticker that ended up there through
    some other path (defensive) is never silently dropped from this table.

    A ticker absent from `baseline_data` (Başlangıç Lot == 0) is a
    genuinely different case from a small existing position shrinking to
    near-zero -- computing a percentage against a zero baseline is
    mathematically meaningless (division by zero), not just a rounding
    edge case. Rendered as "YENİ HİSSE" instead of a percentage when its
    current estimated lot is non-zero, or a plain "-" if it nets out to
    exactly zero (i.e. it was only ever mentioned in disclosures that
    canceled out, never a real new position).

    Between "Oransal Tahmini Delta Lot" and "Güncel Tahmini Lot" sits an
    "İşlem Tarihçesi" column (see `_render_transaction_history_cell`): a
    collapsed `<details>`/`<summary>` disclosure ("3 İşlem Göster") that,
    when clicked, lists every individual dated entry (both Kesinleşen and
    Oransal) behind that ticker's cumulative delta, oldest first -- so a
    "-69 milyon lot" total is never presented as if it happened all at
    once when it was really, say, four separate transactions spread
    across three weeks. This is PURELY additive: it never changes the
    cumulative Başlangıç/Kesinleşen/Oransal/Güncel/% figures, which are
    computed exactly as before.

    Two more columns follow "Taban Tarihinden Beri Lot Değişimi (%)": "Güncel
    Fiyat (TL)"
    (from `current_prices`, see `kap_delta_engine.fetch_bist_prices`) and
    "Güncel Ağırlık (%)" -- `(Güncel Tahmini Lot * Güncel Fiyat /
    current_aum) * 100`, i.e. what fraction of the fund's TOTAL AUM
    (`current_aum`, the raw ToplamDeger, not the "Aktif Güç" subset) this
    single position currently represents. A ticker with no live price
    (missing/delisted/not yet IPO'd -- see `fetch_bist_prices`'s own
    "never fabricates" contract) or a missing/zero `current_aum` renders
    both cells as a plain "-": the weight calculation is skipped for that
    row entirely, never guessed at with a 0.

    Above the table, two Chart.js visuals are rendered from the SAME row
    math (no engine changes): a donut of Güncel Ağırlık (top 10 +
    "Diğerleri") and a bar chart of non-zero since-baseline lot-change %
    (green buys / red sells), or an explicit placeholder when every
    change is still 0%.

    Rows are sorted PRIMARILY by "Güncel Ağırlık (%)" descending (rows
    with a known weight always sort above rows without one) -- this
    table's main question shifted from "hangi hisse ne kadar değişmiş"
    to "portföyün en büyük pozisyonu ne?" once a real TL weight became
    computable. Rows without a resolvable weight (no live price, or no
    AUM at all) fall back to the ABSOLUTE size of the lot change as a
    secondary sort, so they're still roughly biggest-mover-first among
    themselves rather than in arbitrary dict-iteration order.

    Never raises: returns an explicit "veri bulunamadı" notice if there is
    nothing at all to compare (empty baseline AND no deltas of any kind).
    """
    resolved_by_ticker, proportional_by_ticker, transaction_history = _aggregate_signed_lot_deltas(
        resolved, proportional
    )
    current_prices = current_prices or {}

    all_tickers = set(baseline_data) | set(updated_data) | set(resolved_by_ticker) | set(proportional_by_ticker)
    if not all_tickers:
        return '<p class="empty-notice">Portföy evrimi için karşılaştırılabilir veri bulunamadı.</p>'

    rows = []
    for ticker in all_tickers:
        baseline_lot = baseline_data.get(ticker, 0.0)
        resolved_delta = resolved_by_ticker.get(ticker, 0.0)
        proportional_delta = proportional_by_ticker.get(ticker, 0.0)
        current_lot = baseline_lot + resolved_delta + proportional_delta

        price = current_prices.get(ticker)
        weight_pct: Optional[float] = None
        if price is not None and current_aum:
            try:
                position_size = current_lot * float(price)
                weight_pct = (position_size / current_aum) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                weight_pct = None

        rows.append((ticker, baseline_lot, resolved_delta, proportional_delta, current_lot, price, weight_pct))

    rows.sort(
        key=lambda r: (
            r[6] is not None,
            r[6] if r[6] is not None else 0.0,
            abs(r[4] - r[1]),
        ),
        reverse=True,
    )

    body_rows: List[str] = []
    total_baseline = total_resolved = total_proportional = total_current = total_position_size = 0.0
    for ticker, baseline_lot, resolved_delta, proportional_delta, current_lot, price, weight_pct in rows:
        total_baseline += baseline_lot
        total_resolved += resolved_delta
        total_proportional += proportional_delta
        total_current += current_lot

        if baseline_lot == 0:
            if current_lot == 0:
                pct_cell = '<td class="num pct-neutral">-</td>'
            else:
                pct_cell = '<td class="num pct-new">YENİ HİSSE</td>'
        else:
            pct = ((current_lot - baseline_lot) / baseline_lot) * 100.0
            pct_class = "pct-up" if pct > 0 else "pct-down" if pct < 0 else "pct-neutral"
            pct_cell = f'<td class="num {pct_class}">{html.escape(_format_pct_signed(pct))}</td>'

        history_cell = _render_transaction_history_cell(transaction_history.get(ticker) or [])

        if price is None:
            price_cell = '<td class="num pct-neutral">-</td>'
        else:
            price_cell = f'<td class="num">{html.escape(_format_turkish_number(price))}</td>'

        if weight_pct is None:
            weight_cell = '<td class="num pct-neutral">-</td>'
        else:
            total_position_size += current_lot * price
            weight_cell = f'<td class="num">{html.escape(_format_turkish_number(weight_pct))}%</td>'

        body_rows.append(
            f"""
              <tr>
                <td>{html.escape(ticker)}</td>
                <td class="num">{html.escape(_format_turkish_number(baseline_lot))}</td>
                <td class="num">{html.escape(_format_signed_number(resolved_delta))}</td>
                <td class="num">{html.escape(_format_signed_number(proportional_delta))}</td>
                <td class="history-cell">{history_cell}</td>
                <td class="num">{html.escape(_format_turkish_number(current_lot))}</td>
                {pct_cell}
                {price_cell}
                {weight_cell}
              </tr>"""
        )

    total_weight_cell = (
        f'<td class="num">{html.escape(_format_turkish_number((total_position_size / current_aum) * 100.0))}%</td>'
        if current_aum
        else '<td class="num">-</td>'
    )

    chart_html = _render_evolution_charts(_build_evolution_chart_payload(rows))

    return f"""
      {chart_html}
      <div class="table-scroll">
      <table>
            <thead>
              <tr>
                <th>Hisse Kodu</th>
                <th class="num">Başlangıç Lot</th>
                <th class="num">Kesinleşen Delta Lot</th>
                <th class="num">Oransal Tahmini Delta Lot</th>
                <th>İşlem Tarihçesi</th>
                <th class="num">Güncel Tahmini Lot</th>
                <th class="num">Taban Tarihinden Beri Lot Değişimi (%)</th>
                <th class="num">Güncel Fiyat (TL)</th>
                <th class="num">Güncel Ağırlık (%)</th>
              </tr>
            </thead>
            <tbody>{''.join(body_rows)}
            </tbody>
            <tfoot>
              <tr>
                <td>TOPLAM</td>
                <td class="num">{html.escape(_format_turkish_number(total_baseline))}</td>
                <td class="num">{html.escape(_format_signed_number(total_resolved))}</td>
                <td class="num">{html.escape(_format_signed_number(total_proportional))}</td>
                <td>-</td>
                <td class="num">{html.escape(_format_turkish_number(total_current))}</td>
                <td class="num">-</td>
                <td class="num">-</td>
                {total_weight_cell}
              </tr>
            </tfoot>
      </table>
      </div>"""


def _render_tefas_power_table(tefas_power_matrix: Dict[str, Dict[str, float]], fon_kodu: str = "") -> str:
    """Renders `kap_delta_engine.build_tefas_power_matrix`'s output
    (`{"TLY": {"2026-07-31": 191393702793.58, ...}, "DOH": {...}, ...}`) as
    a single table: one row per date (most recent first), one column per
    discovered fund code (target fund `fon_kodu` pinned first, the rest
    alphabetical). A missing (fund, date) combination -- a day that fund
    genuinely has no TEFAS Aktif Güç figure for (weekend/holiday, restricted
    fund, or simply not yet scraped) -- renders as a plain "-", never a
    fabricated 0.

    Returns an explicit "veri bulunamadı" notice (never raises, never
    renders a broken/empty table) if the matrix or its date set is empty.
    """
    if not tefas_power_matrix:
        return '<p class="empty-notice">TEFAS Aktif Güç verisi bulunamadı.</p>'

    fund_codes = sorted(tefas_power_matrix.keys())
    if fon_kodu and fon_kodu in fund_codes:
        fund_codes.remove(fon_kodu)
        fund_codes.insert(0, fon_kodu)

    all_dates = sorted({date_str for daily in tefas_power_matrix.values() for date_str in daily}, reverse=True)
    if not all_dates:
        return '<p class="empty-notice">TEFAS Aktif Güç verisi bulunamadı.</p>'

    header_cells = "".join(f'<th class="num">{html.escape(code)}</th>' for code in fund_codes)

    body_rows: List[str] = []
    for date_str in all_dates:
        data_cells = "".join(
            f"""
                <td class="num">{
                    html.escape(_format_tl_compact(tefas_power_matrix[code][date_str]))
                    if date_str in tefas_power_matrix.get(code, {}) else '-'
                }</td>"""
            for code in fund_codes
        )
        body_rows.append(
            f"""
              <tr>
                <td>{html.escape(date_str)}</td>{data_cells}
              </tr>"""
        )

    return f"""
      <div class="table-scroll">
      <table>
        <thead><tr><th>Tarih</th>{header_cells}</tr></thead>
        <tbody>{''.join(body_rows)}
        </tbody>
      </table>
      </div>"""


def _render_execution_log(execution_logs: Optional[List[Dict[str, str]]]) -> str:
    """Renders `kap_delta_engine`'s step-by-step narrative log (see that
    module's `_log_step` helper -- a list of `{"time": "HH:MM:SS",
    "message": "..."}` entries appended at every critical point of the
    pipeline: KAP baseline fetched, buy/sell disclosures scanned and
    classified, related funds discovered, TEFAS Aktif Güç calculated, the
    proportional-distribution formula applied, etc.) as a terminal-styled
    vertical timeline, meant to be placed at the very TOP of the report --
    before any data table -- so a reader can follow the full reasoning
    chain (what was fetched, what formula was applied, why) before ever
    looking at a number.

    Returns an empty string (renders nothing at all, not even an empty
    section) if there's simply no log to show -- e.g. a bare
    `export_to_html(parsed_data)` call with no `delta_report`, or a
    `delta_report` that predates this feature and has no
    `execution_logs` key. This keeps the function fully backward
    compatible with every existing call site.
    """
    if not execution_logs:
        return ""

    entries_html = "".join(
        f"""
          <li class="log-entry">
            <span class="log-time">{html.escape(str(entry.get('time', '')))}</span>
            <span class="log-message">{html.escape(str(entry.get('message', '')))}</span>
          </li>"""
        for entry in execution_logs
    )

    return f"""
  <section class="execution-trace">
    <h2>Adım Adım Hesaplama ve Çalışma Günlüğü <span class="badge">{len(execution_logs)} adım</span></h2>
    <p class="section-desc">Aşağıdaki tablolara ulaşmadan önce sistemin izlediği tam mantık zinciri: KAP'tan hangi veri çekildi, hangi fonlar keşfedildi, hangi formüller hangi sırayla uygulandı.</p>
    <ol class="log-timeline">{entries_html}
    </ol>
  </section>"""


def _render_delta_sections(delta_report: dict) -> str:
    """Renders the `kap_delta_engine.KAPDeltaEngine` pipeline's sections
    (`export_to_html`'s `delta_report` argument) as extra full-width
    `.period-card`-styled sections: Kesinlesen Deltalar, Cozulemeyen/Coklu
    Fon Bildirimleri, Oransal Olarak Dagitilan Coklu Fon Islemleri (from
    `resolve_multi_fund_deltas`, optional), Hisse Bazli Portfoy Evrimi
    (Lot Degisim Ozeti) -- baseline'dan bugune ticker basina net degisim,
    % oran, guncel BIST fiyati (`current_prices`, see
    `kap_delta_engine.fetch_bist_prices`) ve fonun toplam AUM'una
    (`current_aum`, see `kap_delta_engine.get_latest_aum_for_fund`) oranla
    guncel agirlik (%) -- bkz. `_render_portfolio_evolution_table` --, and
    Gunluk Aktif Satin Alma Gucu (TEFAS Havuzu) (from
    `build_tefas_power_matrix`, optional -- see `_render_tefas_power_table`).
    Never raises: missing/empty lists (or a missing
    `tefas_power_matrix`/`baseline_data`/`current_prices`/`current_aum`)
    are rendered as an explicit "veri bulunamadi" notice (or a plain "-"
    per-cell) rather than a broken table.
    """
    fon_kodu = delta_report.get("fon_kodu") or ""
    baseline_period = delta_report.get("baseline_period") or "?"
    # The report's OWN valuation date, measured from its contents by
    # `kap_delta_engine.date_baseline_report`. `baseline_period` is only a
    # KAP filing label and is NOT a date -- since KAP moved to weekly
    # filings it routinely says "Ağustos-2026" on September data.
    baseline_as_of = _format_baseline_as_of(delta_report.get("baseline_as_of"))
    baseline_data = delta_report.get("baseline_data") or {}
    resolved = delta_report.get("resolved") or []
    unresolved = delta_report.get("unresolved") or []
    proportional = delta_report.get("proportionally_resolved") or []
    updated_data = delta_report.get("updated_data") or {}
    tefas_power_matrix = delta_report.get("tefas_power_matrix") or {}
    current_prices = delta_report.get("current_prices") or {}
    current_aum = delta_report.get("current_aum")
    current_aum_date = delta_report.get("current_aum_date")

    if resolved:
        resolved_rows = "".join(
            f"""
              <tr>
                <td>{html.escape(str(item.get('date', '')))}</td>
                <td>{html.escape(str(item.get('ticker', '')))}</td>
                <td class="num">{html.escape(_format_turkish_number(item.get('lot', 0.0)))}</td>
                <td class="{'dir-alim' if item.get('direction') == 'ALIM' else 'dir-satim' if item.get('direction') == 'SATIM' else ''}">{html.escape(str(item.get('direction', '')))}</td>
              </tr>"""
            for item in sorted(resolved, key=lambda item: item.get("date", ""))
        )
        resolved_html = f"""
          <table>
            <thead><tr><th>Tarih</th><th>Hisse/Pay Kodu</th><th class="num">Lot Miktarı</th><th>İşlem Yönü</th></tr></thead>
            <tbody>{resolved_rows}
            </tbody>
          </table>"""
    else:
        resolved_html = '<p class="empty-notice">Bu aralıkta kesinleşmiş (tek-fonlu) bir işlem bulunamadı.</p>'

    if unresolved:
        unresolved_rows = "".join(
            f"""
              <tr>
                <td>{html.escape(str(item.get('date', '')))}</td>
                <td>{html.escape(', '.join(item.get('related_funds') or []))}</td>
                <td>{html.escape(', '.join(item.get('companies') or []) or 'BİLİNMEYEN')}</td>
                <td class="num">{html.escape(_format_turkish_number(item['net_lot'])) if item.get('net_lot') is not None else '-'}</td>
              </tr>"""
            for item in sorted(unresolved, key=lambda item: item.get("date", ""))
        )
        unresolved_html = f"""
          <table>
            <thead><tr><th>Tarih</th><th>İlgili Fonlar Listesi</th><th>Hisse/Pay Kodu</th><th class="num">Toplam Lot Miktarı (Net)</th></tr></thead>
            <tbody>{unresolved_rows}
            </tbody>
          </table>
          <p class="warn-notice">Bu {len(unresolved)} bildirim birden fazla fonu aynı anda kapsadığı için '{html.escape(fon_kodu)}'a özel KESİN bir kırılım KAP verisinde mevcut değil. Aşağıdaki "Oransal Olarak Dağıtılan" bölümünde başarıyla tahmin edilenler Güncel Portföy Son Durumu'na dahil edilmiştir; TEFAS verisi eksik olduğu için tahmin edilemeyenler ise dışarıda bırakılmıştır.</p>"""
    else:
        unresolved_html = '<p class="empty-notice">Bu aralıkta çok-fonlu/çözülemeyen bir bildirim bulunamadı.</p>'

    if proportional:
        proportional_rows = "".join(
            f"""
              <tr>
                <td>{html.escape(str(item.get('date', '')))}</td>
                <td>{html.escape(str(item.get('ticker', '')))}</td>
                <td>{html.escape(', '.join(item.get('related_funds') or []))}</td>
                <td class="num">%{item.get('weight_pct', 0.0):.2f}</td>
                <td class="num">{html.escape(_format_turkish_number(item.get('net_lot_total', 0.0) or 0.0))}</td>
                <td class="num">{html.escape(_format_turkish_number(item.get('estimated_lot', 0.0) or 0.0))}</td>
                <td class="{'dir-alim' if item.get('direction') == 'ALIM' else 'dir-satim' if item.get('direction') == 'SATIM' else ''}">{html.escape(str(item.get('direction', '')))}</td>
              </tr>"""
            for item in sorted(proportional, key=lambda item: item.get("date", ""))
        )
        proportional_html = f"""
          <table>
            <thead>
              <tr>
                <th>Tarih (İşlem Tarihi)</th><th>Hisse/Pay Kodu</th><th>İlgili Fonlar</th>
                <th class="num">Havuz Payı</th><th class="num">Toplam Net Lot (Havuz)</th>
                <th class="num">Tahmini Lot ({html.escape(fon_kodu)})</th><th>İşlem Yönü</th>
              </tr>
            </thead>
            <tbody>{proportional_rows}
            </tbody>
          </table>"""
    else:
        proportional_html = '<p class="empty-notice">Oransal olarak dağıtılmış bir işlem bulunamadı.</p>'

    evolution_html = _render_portfolio_evolution_table(
        baseline_data, resolved, proportional, updated_data, current_prices, current_aum
    )
    aum_note = (
        f'{html.escape(_format_turkish_number(current_aum))} TL toplam AUM ({html.escape(str(current_aum_date))} tarihli TEFAS verisi) baz alınmıştır.'
        if current_aum
        else "güncel Toplam AUM bulunamadığı için bu sütun \"-\" olarak bırakılmıştır."
    )
    tefas_power_html = _render_tefas_power_table(tefas_power_matrix, fon_kodu)
    tefas_power_days = len({date_str for daily in tefas_power_matrix.values() for date_str in daily})

    # Always name the KAP filing label AND, when it was established, the
    # measured valuation date -- they are routinely different periods
    # since KAP went weekly, and only the second one bounds the deltas.
    baseline_ref = (
        f"{baseline_period} dönemi (ölçülen veri tarihi {baseline_as_of})"
        if baseline_as_of
        else f"{baseline_period} dönemi"
    )
    delta_window_note = (
        f"taban raporun KENDİ verisinden ölçülen değerleme tarihinden ({baseline_as_of}) "
        "bu yana"
        if baseline_as_of
        else "taban raporun değerleme tarihinden bu yana"
    )
    if current_aum_date:
        price_cutoff = f"{current_aum_date} kapanışına"
        price_sentence = (
            f"Güncel Fiyat, {current_aum_date} tarihli (ya da o günden önceki son) BIST kapanışıdır; "
            "bugünün seansı değildir."
        )
    else:
        price_cutoff = "fiyat kesimine"
        price_sentence = "Güncel Fiyat, çağıranın verdiği BIST kapanışıdır; canlı seans değildir."

    return f"""
  <div class="delta-sections">
    <section class="period-card">
      <h2>Kesinleşen Deltalar <span class="badge ok">{len(resolved)} işlem</span></h2>
      <p class="section-desc">{html.escape(fon_kodu)} baseline'ı {html.escape(baseline_ref)} sonrası, sadece {html.escape(fon_kodu)}'yı kapsayan ve başarıyla uygulanmış işlemler. Değerleme tarihi ve öncesindeki bildirimler, taban PDF'in içinde ZATEN yer aldığı için işleme alınmaz (çift sayma koruması).</p>
      {resolved_html}
    </section>
    <section class="period-card">
      <h2>Çözülemeyen / Çoklu Fon Bildirimleri <span class="badge warn">{len(unresolved)} bildirim</span></h2>
      <p class="section-desc">KAP'tan dönen ama birden fazla fonu kapsadığı için {html.escape(fon_kodu)}'ya özel ayrıştırılamayan işlemler.</p>
      {unresolved_html}
    </section>
    <section class="period-card">
      <h2>Oransal Olarak Dağıtılan Çoklu Fon İşlemleri <span class="badge ok">{len(proportional)} kayıt</span></h2>
      <p class="section-desc">Yukarıdaki çözülemeyen bildirimler, İŞLEM TARİHİNDEKİ TEFAS "Aktif Güç" (AUM × (Hisse Senedi % + Likidite %)) değerlerine göre ilgili fonlar arasında ORANTILI olarak dağıtılmıştır. Bu bir TAHMİNDİR, KAP'ın kendi yayınladığı kesin bir rakam değildir.</p>
      {proportional_html}
    </section>
    <section class="period-card">
      <h2>Hisse Bazlı Portföy Evrimi (Lot Değişim Özeti) <span class="badge">{len(updated_data)} kod</span></h2>
      <p class="section-desc">{html.escape(baseline_ref)} Başlangıç Portföyü'nden {html.escape(price_cutoff)}, hisse başına net değişim: Başlangıç Lot + Kesinleşen Delta + Oransal Tahmini Delta = Güncel Tahmini Lot. "Taban Tarihinden Beri Lot Değişimi (%)", tek başına bir lot rakamının ("-69 milyon lot" gibi) neye göre büyük/küçük olduğunu, başlangıca oranlayarak gösterir; baseline'da hiç olmayıp yeni giren bir kod için oran hesaplanamayacağından "YENİ HİSSE" yazılır. "İşlem Tarihçesi" sütunundaki açılır listeye tıklayarak bu net toplamın hangi tarih(ler)de, kaç ayrı işlemle oluştuğunu görebilirsiniz. {price_sentence} "Güncel Ağırlık (%)" bu pozisyonun (Güncel Tahmini Lot × Güncel Fiyat) fonun toplam AUM'una oranıdır -- {aum_note} Fiyatı bulunamayan hisselerde (delist/yeni halka arz) bu iki sütun "-" gösterir ve ağırlık hesabına dahil edilmez. Tablo "portföyün en büyük pozisyonu ne?" sorusuna göre Güncel Ağırlık (%) azalan sırada listelenir; ağırlığı hesaplanamayan hisseler listenin sonunda, mutlak lot değişimine göre sıralanır.</p>
      <p class="section-alert"><strong>DİKKAT: Lot değişim oranları uzun vadeli yatırım trendini yansıtmaz. Bu oranlar sadece {html.escape(delta_window_note)} gerçekleşen işlemleri gösterir ve KAP her yeni portföy dağılım raporunu yayınladığında sıfırlanır -- bu raporlar artık HAFTALIK yayınlandığı için pencere birkaç gün kadar kısa olabilir. Raporun dönem etiketi ("Ağustos-2026" gibi) bir tarih DEĞİLDİR ve hesaplamada kullanılmaz; değerleme tarihi PDF'in kendi verisinden ölçülür.</strong></p>
      {evolution_html}
    </section>
    <section class="period-card">
      <h2>Günlük Aktif Satın Alma Gücü (TEFAS Havuzu) <span class="badge">{len(tefas_power_matrix)} fon &middot; {tefas_power_days} gün</span></h2>
      <p class="section-desc">Her fonun o günkü TEFAS Aktif Gücü (AUM × (Hisse Senedi % + Likidite %)); yukarıdaki oransal dağıtımın hangi havuz rakamlarına dayandığını doğrulamak içindir. Verisi olmayan gün/fon hücreleri "-" ile gösterilir.</p>
      {tefas_power_html}
    </section>
  </div>"""


def export_to_html(
    parsed_data: Dict[str, Dict[str, float]],
    output_filename: str = "parser_kontrol_raporu.html",
    delta_report: Optional[dict] = None,
) -> str:
    """Renders the nested dict returned by `KAPPdfParser.parse_directory()`
    (`{"2026_01": {"ALKLC": 731256.0, ...}, "2026_02": {...}}`) as a single,
    standalone HTML file for manual visual double-checking of the parsed
    figures -- one table per period, each with a "Hisse/Varlik Kodu" and a
    "Nominal Deger (Lot)" column, Turkish-formatted numbers, alternating
    row colors, and a hover highlight.

    Optional `delta_report` extends the same file with the intra-month
    "Pay Alim Satim Bildirimi" results produced by
    `kap_delta_engine.KAPDeltaEngine.apply_delta` (see that module for how
    they're computed): five extra sections (Kesinlesen Deltalar,
    Cozulemeyen/Coklu Fon Bildirimleri, Oransal Olarak Dagitilan Coklu Fon
    Islemleri, Hisse Bazli Portfoy Evrimi/Lot Degisim Ozeti, and Gunluk
    Aktif Satin Alma Gucu) appended AFTER the per-period grid above, plus
    one more -- the "Adim Adim Hesaplama ve Calisma Gunlugu" (Execution
    Trace) timeline -- placed BEFORE everything else (even before the
    per-period grid), so a reader sees the pipeline's full plain-language
    reasoning first and the raw tables second. This module has no import
    dependency on `kap_delta_engine.py` itself, so the caller passes plain
    dicts/lists, shaped as:

        {
            "fon_kodu": "TLY",
            "baseline_period": "2026_HB35",           # which parsed_data key was used as the delta baseline (a KAP filing label, NOT a date)
            "baseline_as_of": date(2026, 9, 4),         # optional -- the baseline's MEASURED valuation date
            "baseline_delta_start": date(2026, 9, 5),   # optional -- first day the deltas may cover
            "baseline_data": {"ALKLC": 731256.0, ...},  # that period's raw holdings -- the "Başlangıç Lot" column
            "resolved": [                               # single-fund, applied deltas
                {"date": "23/07/2026", "ticker": "BIGEN", "lot": 42469924.0, "direction": "ALIM"},
                ...
            ],
            "unresolved": [                             # multi-fund, KAP never breaks these down per-fund
                {"date": "02/07/2026", "related_funds": ["T3B", "TLY"], "companies": ["PEKGY"], "net_lot": 38176445.0},
                ...
            ],
            "proportionally_resolved": [                # optional -- KAPDeltaEngine.resolve_multi_fund_deltas()
                {"date": "02/07/2026", "ticker": "PEKGY", "related_funds": ["T3B", "TLY"],
                 "pool_total": 950000000.0, "target_power": 187000000.0, "weight_pct": 19.68,
                 "net_lot_total": 38176445.0, "estimated_lot": 7513744.6, "direction": "ALIM"},
                ...
            ],
            "updated_data": {"ALKLC": 731256.0, "BIGEN": 42469924.0, ...},  # baseline + resolved + proportionally_resolved
            "tefas_power_matrix": {                     # optional -- kap_delta_engine.build_tefas_power_matrix()
                "TLY": {"2026-07-31": 191393702793.58, "2026-07-30": 187837027636.22, ...},
                "DOH": {"2026-07-31": 449426747.01, ...},
                ...
            },
            "current_prices": {                          # optional -- kap_delta_engine.fetch_bist_prices()
                "ALKLC": 48.25, "BIGEN": 12.7,
                "SELEC": None,                            # delisted/no price found -> None, never 0.0
                ...
            },
            "current_aum": 191393702793.58,              # optional -- kap_delta_engine.get_latest_aum_for_fund()[1]
            "current_aum_date": "2026-07-31",             # optional -- kap_delta_engine.get_latest_aum_for_fund()[0]
            "execution_logs": [                          # optional -- see kap_delta_engine._log_step
                {"time": "12:03:41", "message": "Adım 0 (Başlangıç Portföyü): ..."},
                {"time": "12:03:58", "message": "Adım 1 tamamlandı: ..."},
                ...
            ],
        }

    Omitting `baseline_data` (older callers that predate this field) is
    handled gracefully: every ticker's "Başlangıç Lot" simply renders as 0
    and, since 0 vs. a non-zero current lot is exactly the "YENİ HİSSE"
    case (see `_render_portfolio_evolution_table`), every row would render
    as a new position -- technically correct given the missing input, but
    a strong signal to pass this field if it's available.

    Pass `delta_report=None` (the default) to render exactly the original
    per-period report with no extra sections -- fully backward compatible.
    Likewise, omitting `tefas_power_matrix` or `execution_logs` from
    `delta_report` simply renders that one extra section as an explicit
    "veri bulunamadı" notice (or, for `execution_logs`, renders nothing at
    all) rather than breaking the rest of the report. When present,
    `execution_logs` is rendered as a terminal-styled vertical timeline at
    the very TOP of the report -- before the per-period grid and every
    delta section -- narrating the full pipeline in plain language before
    any table is shown (see `_render_execution_log`).

    Writes the file to `output_filename` (relative paths are created in the
    current working directory) and also returns the generated HTML string.
    Never raises: any period (or delta list) that's empty/missing is
    rendered as an explicit "veri bulunamadi" notice rather than a broken
    table, and a bad `output_filename` results in a logged error rather
    than an unhandled crash.
    """
    generated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    periods = sorted(parsed_data.keys())

    execution_log_html = _render_execution_log(delta_report.get("execution_logs") if delta_report else None)

    sections: List[str] = []
    for period in periods:
        holdings = parsed_data.get(period) or {}

        if not holdings:
            sections.append(
                f"""
        <section class="period-card">
          <h2>Dönem: {html.escape(period)}</h2>
          <p class="empty-notice">Bu dönem için ayrıştırılmış veri bulunamadı.</p>
        </section>"""
            )
            continue

        rows_html: List[str] = []
        total_lot = 0.0
        for ticker in sorted(holdings.keys()):
            lot = holdings[ticker]
            total_lot += lot
            rows_html.append(
                f"""
              <tr>
                <td>{html.escape(str(ticker))}</td>
                <td class="num">{html.escape(_format_turkish_number(lot))}</td>
              </tr>"""
            )

        sections.append(
            f"""
        <section class="period-card">
          <h2>Dönem: {html.escape(period)} <span class="badge">{len(holdings)} kod</span></h2>
          <table>
            <thead>
              <tr>
                <th>Hisse/Varlık Kodu</th>
                <th class="num">Nominal Değer (Lot)</th>
              </tr>
            </thead>
            <tbody>{''.join(rows_html)}
            </tbody>
            <tfoot>
              <tr>
                <td>TOPLAM</td>
                <td class="num">{html.escape(_format_turkish_number(total_lot))}</td>
              </tr>
            </tfoot>
          </table>
        </section>"""
        )

    delta_sections_html = _render_delta_sections(delta_report) if delta_report else ""

    html_document = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>KAP Portföy Dağılım Raporu - Kontrol Raporu</title>
<style>
  * {{ box-sizing: border-box; }}
  /* Financial terminal dark mode
     Surface: slate-900 #0f172a / slate-800 #1e293b / slate-700 #334155
     Type: slate-200 #e2e8f0 / slate-300 #cbd5e1 / muted slate-400 #94a3b8
     Semantic: emerald-400 #34d399 · red-400 #f87171 · amber wash */
  body {{
    font-family: "Segoe UI", Arial, sans-serif;
    background-color: #0f172a;
    color: #cbd5e1;
    margin: 0;
    padding: 32px 24px 64px;
  }}
  header {{
    max-width: 1200px;
    margin: 0 auto 32px;
  }}
  header h1 {{
    margin: 0 0 6px;
    font-size: 24px;
    color: #e2e8f0;
    font-weight: 650;
    letter-spacing: -0.01em;
  }}
  header p {{
    margin: 0;
    color: #64748b;
    font-size: 13px;
  }}
  .grid {{
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    align-items: flex-start;
  }}
  .period-card {{
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 18px 20px 22px;
    box-shadow: 0 4px 16px rgba(2, 6, 23, 0.35);
    flex: 1 1 340px;
    min-width: 320px;
  }}
  .period-card h2 {{
    font-size: 16px;
    margin: 0 0 14px;
    color: #e2e8f0;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .badge {{
    font-size: 11px;
    font-weight: 600;
    color: #7dd3fc;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.28);
    padding: 2px 9px;
    border-radius: 999px;
  }}
  .empty-notice {{
    color: #64748b;
    font-style: italic;
    font-size: 13px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13.5px;
    color: #cbd5e1;
  }}
  thead th {{
    text-align: left;
    background-color: rgba(15, 23, 42, 0.65);
    color: #94a3b8;
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.02em;
    text-transform: none;
    padding: 9px 12px;
    border: 1px solid #334155;
  }}
  tbody td, tfoot td {{
    padding: 7px 12px;
    border: 1px solid rgba(51, 65, 85, 0.85);
  }}
  tbody tr:nth-child(even) {{
    background-color: rgba(15, 23, 42, 0.45);
  }}
  tbody tr:hover {{
    background-color: rgba(51, 65, 85, 0.55);
  }}
  tfoot td {{
    font-weight: 700;
    background-color: rgba(15, 23, 42, 0.65);
    border-top: 2px solid #334155;
    color: #e2e8f0;
  }}
  td.num, th.num {{
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-family: "Consolas", "Courier New", monospace;
  }}
  .delta-sections {{
    max-width: 1200px;
    margin: 36px auto 0;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }}
  .delta-sections .period-card {{
    flex: none;
    min-width: 0;
  }}
  .badge.warn {{
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.35);
  }}
  .badge.ok {{
    color: #34d399;
    background: rgba(16, 185, 129, 0.12);
    border: 1px solid rgba(16, 185, 129, 0.3);
  }}
  .section-desc {{
    margin: -6px 0 14px;
    color: #64748b;
    font-size: 12.5px;
    line-height: 1.5;
  }}
  .section-alert {{
    margin: 0 0 18px;
    padding: 10px 12px;
    background: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.32);
    border-radius: 8px;
    color: #fbbf24;
    font-size: 12.5px;
    line-height: 1.45;
  }}
  .charts-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin: 0 0 20px;
  }}
  .chart-card {{
    flex: 1 1 320px;
    min-width: 280px;
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 14px 14px 10px;
  }}
  .chart-card h3 {{
    margin: 0 0 10px;
    font-size: 13.5px;
    color: #cbd5e1;
    font-weight: 600;
  }}
  .chart-canvas-wrap {{
    position: relative;
    height: 280px;
    width: 100%;
  }}
  .chart-placeholder {{
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    margin: 0;
    padding: 16px;
    text-align: center;
    color: #64748b;
    font-size: 13px;
    font-style: italic;
    background: rgba(15, 23, 42, 0.4);
    border-radius: 8px;
    border: 1px dashed #334155;
  }}
  .warn-notice {{
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.32);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 12.5px;
    margin: 14px 0 0;
  }}
  .dir-alim {{ color: #34d399; font-weight: 600; }}
  .dir-satim {{ color: #f87171; font-weight: 600; }}
  .pct-up {{ color: #34d399; font-weight: 700; }}
  .pct-down {{ color: #f87171; font-weight: 700; }}
  .pct-neutral {{ color: #64748b; }}
  .pct-new {{
    color: #7dd3fc;
    font-weight: 700;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 6px;
    font-size: 11.5px;
    letter-spacing: 0.02em;
    padding: 1px 6px;
  }}
  td.history-cell {{
    white-space: nowrap;
  }}
  .no-history {{
    color: #64748b;
    font-style: italic;
    font-size: 12px;
  }}
  .history-details {{
    display: inline-block;
  }}
  .history-details summary {{
    cursor: pointer;
    color: #7dd3fc;
    font-size: 12px;
    font-weight: 600;
    list-style: none;
    padding: 3px 9px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.25);
    white-space: nowrap;
  }}
  .history-details summary::-webkit-details-marker {{
    display: none;
  }}
  .history-details summary:hover {{
    background: rgba(56, 189, 248, 0.16);
  }}
  .history-details[open] summary {{
    background: rgba(56, 189, 248, 0.16);
    border-radius: 8px 8px 0 0;
  }}
  .history-list {{
    list-style: none;
    margin: 0;
    padding: 8px 10px;
    background: rgba(15, 23, 42, 0.75);
    border: 1px solid #334155;
    border-top: none;
    border-radius: 0 0 8px 8px;
    min-width: 220px;
    max-width: 320px;
    white-space: normal;
  }}
  .history-list li {{
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    color: #cbd5e1;
    padding: 3px 0;
    border-bottom: 1px dashed rgba(51, 65, 85, 0.9);
  }}
  .history-list li:last-child {{
    border-bottom: none;
  }}
  .table-scroll {{
    overflow-x: auto;
  }}
  .table-scroll table {{
    min-width: 640px;
  }}
  .execution-trace {{
    max-width: 1200px;
    margin: 0 auto 28px;
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 20px 24px 24px;
    box-shadow: 0 4px 16px rgba(2, 6, 23, 0.35);
  }}
  .execution-trace h2 {{
    margin: 0 0 6px;
    font-size: 16px;
    color: #e2e8f0;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .execution-trace .section-desc {{
    color: #64748b;
    margin: 0 0 18px;
  }}
  .log-timeline {{
    list-style: none;
    margin: 0;
    padding: 2px 0 2px 20px;
    border-left: 2px solid #334155;
  }}
  .log-entry {{
    position: relative;
    padding: 0 0 16px 18px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12.5px;
    line-height: 1.6;
  }}
  .log-entry:last-child {{
    padding-bottom: 0;
  }}
  .log-entry::before {{
    content: "";
    position: absolute;
    left: -25px;
    top: 4px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #38bdf8;
    opacity: 0.85;
    box-shadow: 0 0 0 3px #1e293b, 0 0 0 4px #334155;
  }}
  .log-time {{
    display: inline-block;
    color: #7dd3fc;
    font-weight: 700;
    margin-right: 10px;
  }}
  .log-message {{
    color: #cbd5e1;
  }}
</style>
</head>
<body>
  <header>
    <h1>KAP Portföy Dağılım Raporu &mdash; Kontrol Raporu</h1>
    <p>Oluşturulma zamanı: {html.escape(generated_at)} &middot; {len(periods)} dönem &middot; kap_pdf_parser.py tarafından otomatik üretildi</p>
  </header>
  {execution_log_html}
  <div class="grid">{''.join(sections)}
  </div>
  {delta_sections_html}
</body>
</html>
"""

    try:
        with open(output_filename, "w", encoding="utf-8") as file:
            file.write(html_document)
        print(f"[BAŞARILI] Kontrol raporu oluşturuldu: {output_filename}")
    except OSError as exc:
        print(f"[HATA] HTML raporu yazılamadı ('{output_filename}'): {exc}")

    return html_document


if __name__ == "__main__":
    parser = KAPPdfParser()
    history = parser.parse_directory("tly_pdfs")

    print("\n" + "=" * 60)
    print("KAP PORTFÖY DAĞILIM RAPORU - HİSSE SENEDİ AYRIŞTIRMA SONUCU")
    print("=" * 60)

    for period in sorted(history):
        holdings = history[period]
        print(f"\n[{period}] {len(holdings)} hisse kodu:")
        if not holdings:
            print("  (veri bulunamadı)")
            continue
        for ticker in sorted(holdings):
            lot = holdings[ticker]
            print(f"  {ticker:<8} {lot:>18,.2f} lot")

    print("\n" + "=" * 60)
    export_to_html(history, "parser_kontrol_raporu.html")
