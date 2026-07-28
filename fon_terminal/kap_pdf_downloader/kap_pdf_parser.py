"""
kap_pdf_parser.py

Standalone, self-contained module for parsing a fund's monthly
"Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF -- as downloaded
by `kap_downloader.py` -- into a clean {hisse_kodu: lot_miktari} dictionary
for the "HISSE SENETLERI" (equities) section only.

This module lives in the same isolated sandbox as `kap_downloader.py` and
has no dependency on any other part of the host project; it only needs the
third-party `pdfplumber` library.

Usage:
    from kap_pdf_parser import KAPPdfParser

    parser = KAPPdfParser()
    holdings = parser.parse_file("tly_pdfs/TLY_2026_03.pdf")
    # {"ALKLC": 731256.0, "CWENE": 3000000.0, ...}

    history = parser.parse_directory("tly_pdfs")
    # {"2026_01": {...}, "2026_02": {...}, "2026_03": {...}}
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

import pdfplumber


class KAPPdfParser:
    """Extracts the "HISSE SENETLERI" (equities) holdings table out of a
    KAP monthly Portfoy Dagilim Raporu PDF.

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

    # Matches "TLY_2026_03.pdf" (or any "..._{YYYY}_{MM}..." filename) so
    # `parse_directory` can key its results by period without depending on
    # any particular fund code prefix.
    PERIOD_FILENAME_PATTERN = re.compile(r"(\d{4})_(\d{2})")

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
        dictionary keyed by the "{YIL}_{AY}" period parsed out of each
        filename, e.g.:

            {"2026_01": {"ALKLC": 731256.0, ...}, "2026_02": {...}}

        Files whose name doesn't contain a recognizable "YYYY_MM" period
        (as produced by `KAPPdfDownloader`, e.g. "TLY_2026_01.pdf") are
        skipped with a console warning rather than raising.
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
                self._log(f"[UYARI] '{filename}' dosya adından tarih (YYYY_AA) çıkarılamadı, atlanıyor.")
                continue

            filepath = os.path.join(dirpath, filename)
            results[period_key] = self.parse_file(filepath)

        return results

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
        """Pulls a "{YYYY}_{MM}" period key out of a filename like
        "TLY_2026_03.pdf" -> "2026_03". Returns None if no such pattern is
        present.
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


def _holdings_table_html(holdings: Dict[str, float], code_header: str = "Hisse/Varlık Kodu") -> str:
    """Shared renderer for a simple {kod: lot} table with a TOPLAM footer
    row, used both by the per-period cards above and by the "Güncel
    Portföy Son Durumu" delta section below."""
    if not holdings:
        return '<p class="empty-notice">Veri bulunamadı.</p>'
    rows = "".join(
        f"""
              <tr>
                <td>{html.escape(str(ticker))}</td>
                <td class="num">{html.escape(_format_turkish_number(holdings[ticker]))}</td>
              </tr>"""
        for ticker in sorted(holdings)
    )
    total = sum(holdings.values())
    return f"""
          <table>
            <thead><tr><th>{html.escape(code_header)}</th><th class="num">Nominal Değer (Lot)</th></tr></thead>
            <tbody>{rows}
            </tbody>
            <tfoot><tr><td>TOPLAM</td><td class="num">{html.escape(_format_turkish_number(total))}</td></tr></tfoot>
          </table>"""


def _render_delta_sections(delta_report: dict) -> str:
    """Renders the three `kap_delta_engine.KAPDeltaEngine.apply_delta`
    sections (`export_to_html`'s `delta_report` argument) as extra
    full-width `.period-card`-styled sections: Kesinlesen Deltalar,
    Cozulemeyen/Coklu Fon Bildirimleri, and Guncel Portfoy Son Durumu.
    Never raises: missing/empty lists are rendered as an explicit
    "veri bulunamadi" notice rather than a broken table.
    """
    fon_kodu = delta_report.get("fon_kodu") or ""
    baseline_period = delta_report.get("baseline_period") or "?"
    resolved = delta_report.get("resolved") or []
    unresolved = delta_report.get("unresolved") or []
    updated_data = delta_report.get("updated_data") or {}

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
          <p class="warn-notice">Bu {len(unresolved)} bildirim birden fazla fonu aynı anda kapsadığı için '{html.escape(fon_kodu)}'a özel kırılım KAP verisinde mevcut değil; bu yüzden Güncel Portföy Son Durumu'na dahil edilmemiştir.</p>"""
    else:
        unresolved_html = '<p class="empty-notice">Bu aralıkta çok-fonlu/çözülemeyen bir bildirim bulunamadı.</p>'

    return f"""
  <div class="delta-sections">
    <section class="period-card">
      <h2>Kesinleşen Deltalar <span class="badge ok">{len(resolved)} işlem</span></h2>
      <p class="section-desc">{html.escape(fon_kodu)} baseline dönemi {html.escape(baseline_period)} sonrası, sadece {html.escape(fon_kodu)}'yı kapsayan ve başarıyla uygulanmış işlemler.</p>
      {resolved_html}
    </section>
    <section class="period-card">
      <h2>Çözülemeyen / Çoklu Fon Bildirimleri <span class="badge warn">{len(unresolved)} bildirim</span></h2>
      <p class="section-desc">KAP'tan dönen ama birden fazla fonu kapsadığı için {html.escape(fon_kodu)}'ya özel ayrıştırılamayan işlemler.</p>
      {unresolved_html}
    </section>
    <section class="period-card">
      <h2>Güncel Portföy Son Durumu <span class="badge">{len(updated_data)} kod</span></h2>
      <p class="section-desc">{html.escape(baseline_period)} Başlangıç Portföyü + Kesinleşen Deltalar (Çözülemeyen bildirimler hariç).</p>
      {_holdings_table_html(updated_data, code_header="Hisse/Pay Kodu")}
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
    they're computed), as three extra sections appended after the
    per-period grid above -- this module has no import dependency on
    `kap_delta_engine.py` itself, so the caller passes plain dicts/lists,
    shaped as:

        {
            "fon_kodu": "TLY",
            "baseline_period": "2026_03",             # which parsed_data key was used as the delta baseline
            "resolved": [                               # single-fund, applied deltas
                {"date": "23/07/2026", "ticker": "BIGEN", "lot": 42469924.0, "direction": "ALIM"},
                ...
            ],
            "unresolved": [                             # multi-fund, NOT applied -- see kap_delta_engine's docstring
                {"date": "02/07/2026", "related_funds": ["T3B", "TLY"], "companies": ["PEKGY"], "net_lot": 38176445.0},
                ...
            ],
            "updated_data": {"ALKLC": 731256.0, "BIGEN": 42469924.0, ...},  # baseline + resolved
        }

    Pass `delta_report=None` (the default) to render exactly the original
    per-period report with no extra sections -- fully backward compatible.

    Writes the file to `output_filename` (relative paths are created in the
    current working directory) and also returns the generated HTML string.
    Never raises: any period (or delta list) that's empty/missing is
    rendered as an explicit "veri bulunamadi" notice rather than a broken
    table, and a bad `output_filename` results in a logged error rather
    than an unhandled crash.
    """
    generated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    periods = sorted(parsed_data.keys())

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
  body {{
    font-family: "Segoe UI", Arial, sans-serif;
    background-color: #f4f6f8;
    color: #1f2937;
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
    color: #111827;
  }}
  header p {{
    margin: 0;
    color: #6b7280;
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
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 18px 20px 22px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
    flex: 1 1 340px;
    min-width: 320px;
  }}
  .period-card h2 {{
    font-size: 16px;
    margin: 0 0 14px;
    color: #111827;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .badge {{
    font-size: 11px;
    font-weight: 600;
    color: #1d4ed8;
    background: #dbeafe;
    padding: 2px 9px;
    border-radius: 999px;
  }}
  .empty-notice {{
    color: #9ca3af;
    font-style: italic;
    font-size: 13px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13.5px;
  }}
  thead th {{
    text-align: left;
    background-color: #111827;
    color: #f9fafb;
    padding: 9px 12px;
    border: 1px solid #111827;
  }}
  tbody td, tfoot td {{
    padding: 7px 12px;
    border: 1px solid #e5e7eb;
  }}
  tbody tr:nth-child(even) {{
    background-color: #f3f4f6;
  }}
  tbody tr:hover {{
    background-color: #fef3c7;
  }}
  tfoot td {{
    font-weight: 700;
    background-color: #eef2ff;
    border-top: 2px solid #c7d2fe;
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
  .badge.warn {{ color: #92400e; background: #fef3c7; }}
  .badge.ok {{ color: #065f46; background: #d1fae5; }}
  .section-desc {{
    margin: -6px 0 14px;
    color: #6b7280;
    font-size: 12.5px;
  }}
  .warn-notice {{
    color: #92400e;
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 12.5px;
    margin: 14px 0 0;
  }}
  .dir-alim {{ color: #065f46; font-weight: 600; }}
  .dir-satim {{ color: #991b1b; font-weight: 600; }}
</style>
</head>
<body>
  <header>
    <h1>KAP Portföy Dağılım Raporu &mdash; Kontrol Raporu</h1>
    <p>Oluşturulma zamanı: {html.escape(generated_at)} &middot; {len(periods)} dönem &middot; kap_pdf_parser.py tarafından otomatik üretildi</p>
  </header>
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
