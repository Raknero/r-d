# KAP PDF Downloader & Parser (Sandbox)

Standalone, isolated module for downloading a Turkish investment fund's
monthly "Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF
attachments from KAP (Kamuyu Aydinlatma Platformu / Public Disclosure
Platform). Currently ships with the `TLY` (Tera Portfoy Birinci Serbest
Fon) fund pre-registered.

This directory is intentionally decoupled from the rest of the repository
-- it has its own `requirements.txt` and does not import anything from
`fon_terminal/`. It is meant to be imported into another project later as
a single drop-in module (`kap_downloader.py`).

## How it works

KAP exposes an internal (undocumented) 2-stage backend API:

1. **Disclosure list** -- `GET /tr/api/disclosure/filter/FILTERYFBF/{company_oid}/{member_oid}/{days_back}`
   returns every disclosure for the fund published in the last `days_back`
   days as JSON, including `disclosureIndex`, `year`, `donem` (month), and
   `attachmentCount`.
2. **Attachment resolution + download** -- the disclosure's own ID is
   *not* the same as its PDF attachment's file ID (they diverge in the
   trailing hex characters), and there is no separate JSON endpoint that
   exposes the mapping. The real attachment ID is only ever rendered
   directly into the disclosure's public detail page HTML
   (`/tr/Bildirim/{disclosureIndex}`), next to the attachment's display
   filename. A plain `requests.get` is enough to read it -- the page is
   fully server-rendered, no browser/JS execution required. The PDF
   itself is then fetched from `GET /tr/api/file/download/{attachment_id}`.

**Quirk handled automatically:** that download endpoint claims
`Content-Type: application/pdf` but actually returns the file wrapped in
a Java-serialized `byte[]` (a legacy backend artifact). The module strips
this envelope by locating the `%PDF` magic marker in the response and
keeping everything from there onward, which recovers the original file
byte-for-byte.

## Usage

```python
from kap_downloader import KAPPdfDownloader

with KAPPdfDownloader(fon_kodu="TLY") as downloader:
    results = downloader.download_reports(days_back=365)

# Or narrow down to a specific date range without changing how far back
# KAP itself is queried:
with KAPPdfDownloader(fon_kodu="TLY") as downloader:
    downloader.download_reports(days_back=730, start_period=(2025, 1), end_period=(2025, 12))
```

Or run it directly:

```bash
pip install -r requirements.txt
python kap_downloader.py
```

PDFs are saved into a `tly_pdfs/` folder (created automatically) as
`TLY_{YEAR}_{MONTH:02d}.pdf`, e.g. `TLY_2026_06.pdf`.

## Adding a new fund

`KAPPdfDownloader.KNOWN_FUNDS` maps a fund code to the two opaque GUIDs
(`company_oid`, `member_oid`) KAP's filter endpoint requires. There is no
public lookup that resolves a bare fund code to these IDs, so add new
funds manually by capturing them from the fund's disclosure list page's
network requests (the values that appear in the `FILTERYFBF/.../...`
filter URL).

---

## `kap_pdf_parser.py` -- extracting equity holdings from the PDFs

`KAPPdfParser` reads the PDFs `KAPPdfDownloader` produces and extracts the
"HISSE SENETLERI" (equities) holdings table into a clean
`{hisse_kodu: toplam_lot}` dictionary, using `pdfplumber`.

### Why this is harder than it sounds

This report has **no visible grid lines**, so `page.extract_tables()`
with pdfplumber's default (line-based) settings finds **zero** tables on
the pages that actually hold the data. The parser instead:

1. Crops each page down to just the portfolio table (anchored on the
   repeating `"VADEYE"` header word), then calls `extract_tables()` with
   a text-position-based strategy (`vertical_strategy="text",
   horizontal_strategy="text"`) that works from word alignment instead of
   ruling lines.
2. Never trusts a fixed column index for the "Nominal Deger" (lot count)
   field -- pdfplumber's inferred column boundaries shift from page to
   page (verified: the same field lands at index 7 on one page and index
   8-9 on the next). Instead, each row is scanned left-to-right for the
   first cell matching a strict Turkish thousands-grouped number pattern
   (`-?\d{1,3}(\.\d{3})*(,\d+)?`), which is reliably the lot count since
   ISIN codes/issuer names contain letters and borsa kodu integers (e.g.
   `"80100511"`) have no thousands separators at all.
3. Detects section boundaries (`"HISSE SENETLERI"`, `"BORCLANMA"`, `"GRUP
   TOPLAMI"`) by joining every cell in a row together first, since column
   guessing frequently splits these headers mid-word (e.g. `"HISSE
   SENETL"` + `"ERI"` as two separate cells).
4. Sums a ticker's lot amount across every row it appears in (a stock
   bought at different prices on different days shows up as multiple
   rows for the same code).

This was verified against real downloaded reports: every aggregated total
was manually cross-checked against the raw extracted PDF text and matched
exactly (e.g. `ALKLC` appearing on two rows, `1.255.508,00 + (-524.252,00)
= 731.256,00`, matched the parser's output to the cent).

### Usage

```python
from kap_pdf_parser import KAPPdfParser

parser = KAPPdfParser()

# Single file
holdings = parser.parse_file("tly_pdfs/TLY_2026_03.pdf")
# {"ALKLC": 731256.0, "CWENE": 3000000.0, ...}

# Whole directory, keyed by period parsed from each filename
history = parser.parse_directory("tly_pdfs")
# {"2026_01": {...}, "2026_02": {...}, "2026_03": {...}}
```

Or run it directly (parses everything in `tly_pdfs/` and pretty-prints the
result):

```bash
python kap_pdf_parser.py
```

Malformed rows, blank spacer rows, sub-headings like `"Hisse Turk"`, and
files with no recognizable date in their name are all safely skipped via
`try/except` and logged as warnings rather than raising.

### Manual double-check: `export_to_html`

Terminal output alone shouldn't be trusted for financial figures.
`export_to_html(parsed_data, output_filename="parser_kontrol_raporu.html")`
is a pure, additive export step -- it doesn't touch any parsing logic --
that renders the nested dict from `parse_directory()` as a single,
standalone HTML file (inline CSS, no external assets) with one table per
period: bordered, zebra-striped, hover-highlighted rows, right-aligned
Turkish-formatted numbers (`731256.0` -> `731.256,00`), and a `TOPLAM`
footer row per table so the sum can be eyeballed against the PDF's own
"GRUP TOPLAMI" line.

Running `python kap_pdf_parser.py` now does both steps: prints the parsed
holdings to the console, then writes `parser_kontrol_raporu.html` into the
current directory for visual verification.
