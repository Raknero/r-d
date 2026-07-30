# KAP PDF Downloader & Parser (Sandbox)

Standalone, isolated module for downloading a Turkish investment fund's
monthly "Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF
attachments from KAP (Kamuyu Aydinlatma Platformu / Public Disclosure
Platform). `TLY` (Tera Portfoy Birinci Serbest Fon) is the fund this
project was built around and remains explicitly pinned, but as of
2026-07-30 **any fund code KAP tracks works out of the box** via a
dynamic fund directory -- see "Fund resolution" below.

This directory lives inside `fon_terminal/` but is intentionally
decoupled from the rest of the application -- it has its own
`requirements.txt` and does not import anything from `fon_terminal/`'s
own modules (`data_scraper.py`, `main.py`, etc.). The three modules here
(`kap_downloader.py`, `kap_pdf_parser.py`, `kap_delta_engine.py`) only
depend on each other and third-party packages, so the whole folder can
still be lifted into another project as a self-contained unit.

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

## Fund resolution: static override + dynamic KAP directory (2026-07-30)

`KAPPdfDownloader` used to support **only** funds manually registered in
`KNOWN_FUNDS` (originally just `TLY`), because its filter endpoint scopes
a fund via two opaque GUIDs (`company_oid`, `member_oid`) and there was no
known public lookup that resolves a bare fund code to them. This was the
blocker that made the Shadow Portfolio engine's co-filing funds (`DOH`,
`T3B`, `THF`, `TMV`, `FSU`, `TGI` -- see `discover_related_funds` below)
fail to download at all.

**Resolved.** Two things, both verified live rather than assumed:

1. **A public source for `company_oid` does exist**: KAP's own
   `https://kap.org.tr/tr/YatirimFonlari/ALL` page (a Next.js app) embeds
   every tracked fund's `fundCode` + `fundOid` as backslash-escaped JSON
   inside an inline `<script>` tag (`fundPermaLinks`, 4483 entries
   confirmed present on 2026-07-30) -- there's no separate REST/JSON
   endpoint for it, this page IS the source. `build_dynamic_fund_directory()`
   fetches it once per process (BeautifulSoup pulls the `<script>` text,
   a regex parses the embedded records), caches the result at the class
   level, and never raises -- a network/parsing failure just logs
   `[HATA]`/`[UYARI]` and returns an empty dict.
2. **The endpoint's `member_oid` segment turned out not to be
   fund-specific at all.** It was originally assumed unique per
   fund/manager (captured from TLY's own traffic). Live probing proved
   that wrong: reusing TLY's exact `member_oid` value against a
   completely unrelated fund/manager (Is Portfoy's `IHK`) and a random
   sample of 12 other fund codes across several other companies still
   returned each fund's own correct "Portfoy Dagilim Raporu" list in
   every case where that fund actually publishes one. So this single
   constant (`GENERIC_MEMBER_OID`) is reused for every dynamically
   resolved fund -- only `company_oid` actually needs to vary.

`KAPPdfDownloader.__init__` now resolves a fund code in two steps, in
order: (1) the static `KNOWN_FUNDS` override (zero network cost -- kept
for `TLY` and any fund worth pinning/documenting manually), then (2) the
dynamic directory. If neither has the code, it raises a clear
`ValueError` rather than guessing an ID. `output_dir` also now defaults
to `{fon_kodu_lower}_pdfs/` instead of being hardcoded to `tly_pdfs/`, so
each fund gets its own folder automatically (all `*_pdfs/` folders are
gitignored).

Net effect, verified end-to-end: of the Shadow Portfolio engine's 7
discovered funds, 5 now resolve and download successfully (`TLY`, `DOH`,
`THF`, `TMV`, `FSU` all have March 2026 reports; `T3B` and `TGI` are
correctly skipped with `[UYARI]` because KAP genuinely has zero
"Portfoy Dagilim Raporu" disclosures for them in the probed window --
not a resolution failure). Before this fix it was 1 of 7 (`TLY` only).

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

**Optional delta sections (2026-07-28):** `export_to_html` also accepts an
optional `delta_report` dict (`{"fon_kodu", "baseline_period", "resolved",
"unresolved", "updated_data"}`, produced by `kap_delta_engine.py` -- see
below). When provided, the SAME `parser_kontrol_raporu.html` file gets
three extra sections appended after the per-period grid -- "Kesinleşen
Deltalar", "Çözülemeyen / Çoklu Fon Bildirimleri", "Güncel Portföy Son
Durumu" -- instead of a second, separate HTML file. `delta_report=None`
(the default) renders exactly the original per-period report with no
extra sections, so this module still has zero import dependency on
`kap_delta_engine.py` -- the caller passes plain dicts/lists, never the
other module's dataclasses.

---

## `kap_delta_engine.py` -- bridging the gap between monthly reports

`KAPPdfParser` gives an exact holdings snapshot, but only once a month
(whenever KAP publishes the next "Portfoy Dagilim Raporu"). `KAPDeltaEngine`
keeps that snapshot current in between reports by layering KAP's
intra-month buy/sell disclosures on top of it.

### Architecture

`KAPDeltaEngine` reuses `KAPPdfDownloader`'s detail-page endpoint
(`/tr/Bildirim/{disclosureIndex}`) and its Turkish-number-parsing
convention, but its disclosure *list* stage queries a different KAP
endpoint entirely -- see "Endpoint correction" below for why.

1. **Disclosure list** -- `POST /tr/api/disclosure/members/byCriteria`,
   scoped to the fund's portfolio management company via
   `MANAGER_MKK_MEMBER_OID`, filtered client-side to `subject == "Pay
   Alim Satim Bildirimi"` entries whose `relatedStocks` field mentions
   the target fund code, published inside the requested date window.
2. **Detail page parsing** -- fetches the same `/tr/Bildirim/{disclosureIndex}`
   page `KAPPdfDownloader` reads for the monthly report's PDF link, but
   parses its inline `tbl_oda-10400_Shares-Transaction-Notification`
   table with BeautifulSoup instead: a GWT-rendered taxonomy table where
   every column is duplicated Turkish-then-English with no separator
   between the halves (the split point is located by finding the first
   `"Transaction Date"` cell), and rows are matched by their Turkish
   label text (`"İlgili Şirketler"`, `"İlgili Fonlar"`, `"İşlem
   Tarihi"`) rather than a fixed row index, since optional flag rows
   shift the layout between disclosures.

### Usage

```python
from kap_pdf_parser import KAPPdfParser
from kap_delta_engine import KAPDeltaEngine

baseline = KAPPdfParser().parse_file("tly_pdfs/TLY_2026_06.pdf")

with KAPDeltaEngine(fon_kodu="TLY") as engine:
    updated, unresolved = engine.apply_delta(baseline, start_date="2026-06-01", end_date="2026-07-28")

# `updated` only reflects disclosures that named TLY exclusively.
# `unresolved` is every multi-fund disclosure, fully parsed but not
# merged -- see "Data limitation" below before assuming `updated` is complete.
```

Or run it directly (uses a hardcoded `{"SVGYO": 10000.0}` baseline and
prints an "Onceki -> Delta -> Sonraki" comparison plus the full list of
unresolved multi-fund disclosures for the last 30 days):

```bash
python kap_delta_engine.py
```

### Endpoint correction (2026-07-28)

The first version of `_fetch_delta_disclosures` queried the same
`FILTERYFBF` endpoint `KAPPdfDownloader` uses for the monthly report,
which only ever returns `"Portfoy Dagilim Raporu"` entries for `TLY` --
it returned zero buy/sell notices. **That was a wrong endpoint choice,
not evidence that the fund doesn't publish them.** `FILTERYFBF` is
scoped to the "Yatirim Fonu Bildirimleri" (fund report) category only.

`"Pay Alim Satim Bildirimi"` disclosures are filed under a different KAP
category (`disclosureClass: "ODA"`) by the fund's *portfolio management
company* ("Tera Portfoy Yonetimi A.S." for TLY), via KAP's general
member-disclosure query endpoint:
`POST /tr/api/disclosure/members/byCriteria` with
`mkkMemberOidList: [manager_mkk_member_oid]` and an explicit
`fromDate`/`toDate` range. Verified live: **113 such disclosures exist
for TLY's manager over the last 12 months.** The manager's `mkkMemberOid`
is not the same OID pair `KAPPdfDownloader` uses (`company_oid`/
`member_oid`) -- it's a separate identifier, registered per-fund in
`KAPDeltaEngine.MANAGER_MKK_MEMBER_OID`.

Each disclosure's detail page (`/tr/Bildirim/{disclosureIndex}`) carries
its data inline as a `tbl_oda-10400_Shares-Transaction-Notification`
taxonomy table (GWT-rendered, every field duplicated Turkish-then-English
with no separator, and un-nested field labels rather than a simple flat
grid) -- see `_parse_html_table` for how it's located and parsed.

### Data limitation (verified, not assumed): no per-fund breakdown

Most disclosures fetched for TLY name **multiple funds at once** in
their "İlgili Fonlar" (Related Funds) field -- e.g. one notice's related
funds are `[T3B, TLY, TMV, TGI]` -- because the disclosure is filed by
the *management company* for its combined position across every fund it
manages that holds the traded security. **For those, KAP's data contains
a single aggregate buy/sell/net nominal TL figure for the combined
position; it does not break the trade down per individual fund
anywhere.** Verified live (2026-07-28) on a 30-day/24-disclosure sample
for TLY: 23 of 24 named multiple funds; only 1 (disclosureIndex
`1636905`, a "BIGEN, TLY"-only notice) named TLY exclusively. A full
12-month re-check is still pending -- KAP's WAF rate-limited this
project's IP (`429 Request Limit Exceeded`) partway through a broader
verification pass, so that count is not yet confirmed and is not claimed
here.

`apply_delta()` handles this honestly rather than guessing: it only
auto-merges a disclosure into the baseline when it names the target fund
*exclusively* (an unambiguous case). Every multi-fund disclosure is still
fully parsed -- transaction date, traded company/companies, the complete
related-funds list, and all five nominal TL figures -- and returned as-is
via the `unresolved` list in `apply_delta`'s return value, but is
deliberately kept out of the merged baseline. Attributing a manager-level
aggregate to one fund would require an allocation rule KAP's data doesn't
provide (e.g. an AUM-proportional split), and fabricating one would
silently corrupt the holdings numbers -- so this is left as an explicit,
visible gap for a human to resolve rather than a silent guess.

---

## Shadow Portfolio engine, step 1: `discover_related_funds` (2026-07-30)

The multi-fund `unresolved` disclosures above are also the answer to a
different question: *which other funds share TLY's portfolio manager and
therefore need their own baseline tracked?* `KAPDeltaEngine.
discover_related_funds(unresolved)` scans every unresolved disclosure's
"İlgili Fonlar" list and collects every unique fund code into a single,
deduplicated array -- the target fund is always kept first (as a
reference point), the rest in first-seen order:

```python
with KAPDeltaEngine(fon_kodu="TLY") as engine:
    updated, resolved, unresolved = engine.apply_delta(baseline, start_date=..., end_date=...)
    related_funds = engine.discover_related_funds(unresolved)
    # ['TLY', 'DOH', 'T3B', 'THF', 'TMV', 'FSU', 'TGI']
```

Parsing is defensive by design (`_clean_fund_codes`): it accepts either
an already-parsed `List[str]` (what `ParsedTransaction.related_funds`
actually is) or raw delimited text (`"DOH, T3B, TLY"` / `"[DOH, T3B,
TLY]"`), strips brackets/quotes/whitespace from every token, and drops
anything that doesn't look like a real fund code -- so the result never
carries a trailing space or stray character regardless of the input
shape.

## Shadow Portfolio engine, step 2: `collect_global_baseline` (2026-07-30)

Once the related funds are known, `collect_global_baseline(related_funds,
baseline_period)` (a module-level function, not tied to one fund's
`KAPDeltaEngine` instance) builds a single same-period baseline snapshot
across all of them by orchestrating the other two modules per fund:
`KAPPdfDownloader` downloads that fund's PDF for the given `(year, donem)`
period into its own `{fon_kodu_lower}_pdfs/` folder, then `KAPPdfParser`
parses it, and the result is merged into one dict:

```python
global_baseline = collect_global_baseline(
    related_funds,
    baseline_period=(2026, 3),  # align every fund to the SAME period
)
# {"TLY": {"ALKLC": 731256.0, ...}, "DOH": {...}, "THF": {...}, ...}
```

**Never crashes on a bad fund**, by design: a fund with no registered/
resolvable KAP identity, a failed/empty download for that period, or an
empty parse result are all caught individually, logged as `[UYARI]`, and
simply omitted from the result -- one bad fund never aborts the loop.
Logs a final summary (`X/Y fon basariyla toplandi, Z benzersiz hisse kodu
bulundu`).

**`days_back` hard ceiling, discovered while testing this (2026-07-30):**
KAP's `FILTERYFBF` endpoint silently returns an empty list (`HTTP 200`,
`[]`, no error) for any `days_back` value of 366 or higher -- verified by
probing 30/90/180/365/366/400, where 365 returned real disclosures and
366+ returned zero every time. `collect_global_baseline` therefore
defaults to `days_back=365` and should not be raised past that; an older
`baseline_period` needs multiple windowed calls instead.
