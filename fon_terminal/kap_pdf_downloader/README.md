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
own modules (`data_scraper.py`, `main.py`, etc.), with exactly one
documented exception (`build_tefas_power_matrix`, see Step 3 below). The
three modules here (`kap_downloader.py`, `kap_pdf_parser.py`,
`kap_delta_engine.py`) only depend on each other and third-party
packages, so the whole folder can still be lifted into another project as
a self-contained unit.

Beyond the base download/parse pair, this sandbox has grown into a full
**"Shadow Portfolio" pipeline**: `kap_delta_engine.py` bridges the gap
between KAP's monthly PDF reports by layering intra-month buy/sell
disclosures on top of them (Step 0-1), discovers every other fund sharing
the target fund's portfolio manager (Step 2), collects each of their own
baselines (Step 2), pulls their daily TEFAS purchasing power (Step 3),
and proportionally resolves the multi-fund transactions KAP never breaks
down per fund (Step 4) -- with every step narrated in plain language via
an "Execution Trace" log rendered at the top of the final HTML report (see
below).

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

**Optional delta sections (2026-07-28, extended through 2026-08-01):**
`export_to_html` also accepts an optional `delta_report` dict (produced by
`kap_delta_engine.py` -- see below), shaped as `{"fon_kodu",
"baseline_period", "resolved", "unresolved", "proportionally_resolved",
"updated_data", "tefas_power_matrix", "execution_logs"}`. When provided,
the SAME `parser_kontrol_raporu.html` file gets extended with, in order:

1. **"Adım Adım Hesaplama ve Çalışma Günlüğü" (Execution Trace)** -- a
   terminal-styled vertical timeline placed at the very TOP of the report,
   before every table, narrating the whole pipeline in plain language
   (see "Execution Trace" section below).
2. **"Kesinleşen Deltalar"** -- single-fund, KAP-confirmed transactions
   applied to the baseline.
3. **"Çözülemeyen / Çoklu Fon Bildirimleri"** -- multi-fund disclosures
   KAP never breaks down per fund.
4. **"Oransal Olarak Dağıtılan Çoklu Fon İşlemleri"** -- the subset of (3)
   that `resolve_multi_fund_deltas` was able to estimate proportionally
   (see Step 4 below).
5. **"Güncel Portföy Son Durumu"** -- baseline + (2) + (4).
6. **"Günlük Aktif Satın Alma Gücü (TEFAS Havuzu)"** -- the raw
   `tefas_power_matrix` values that (4)'s weighting was based on, one row
   per date (most recent first), one column per fund.

`delta_report=None` (the default) renders exactly the original per-period
report with no extra sections; any individual key missing from
`delta_report` (e.g. an older caller that predates `tefas_power_matrix` or
`execution_logs`) just renders that one section as an explicit "veri
bulunamadı" notice (or, for `execution_logs`, renders nothing at all)
rather than breaking the rest of the report. This module still has zero
import dependency on `kap_delta_engine.py` -- the caller passes plain
dicts/lists, never the other module's dataclasses.

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
    updated, resolved, unresolved = engine.apply_delta(baseline, start_date="2026-06-01", end_date="2026-07-28")

# `updated` only reflects disclosures that named TLY exclusively.
# `resolved` is one ResolvedDelta per ticker actually merged into `updated`.
# `unresolved` is every multi-fund disclosure, fully parsed but not
# merged -- see "Data limitation" below (and Step 4 above, which CAN
# resolve some of these proportionally) before assuming `updated` is complete.
```

Or run it directly (downloads TLY's own latest KAP baseline, runs the full
Steps 0-4 pipeline against the last 30 days, and writes
`parser_kontrol_raporu.html`):

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

## Shadow Portfolio engine, step 2: `collect_global_baseline` (2026-07-30, revised 2026-07-31)

Once the related funds are known, `collect_global_baseline(related_funds,
days_back=365)` (a module-level function, not tied to one fund's
`KAPDeltaEngine` instance) builds a "Global Baseline" snapshot across all
of them by orchestrating the other two modules per fund: `KAPPdfDownloader`
downloads that fund's OWN most recently published PDF into its own
`{fon_kodu_lower}_pdfs/` folder, then `KAPPdfParser` parses it, and the
result is merged into one dict:

```python
global_baseline, baseline_periods = collect_global_baseline(related_funds)
# global_baseline:  {"TLY": {"ALKLC": 731256.0, ...}, "DOH": {...}, ...}
# baseline_periods: {"TLY": (2026, 6), "DOH": (2026, 5), ...}  # funds can legitimately differ here
```

**"Date Lag" fix (2026-07-31):** the first version of this function took a
single, externally-supplied `baseline_period` and forced every fund onto
that SAME period. This broke the moment that period was derived from
whatever happened to already be sitting in a local `tly_pdfs/` folder
(e.g. a stale March report left over from an earlier dev session) instead
of asking KAP what its actual latest publication was -- funds legitimately
publish on different schedules, so forcing a shared period meant any fund
whose true latest report was newer than that period silently lost every
month in between. The fix: each fund now calls `KAPPdfDownloader.
download_latest_report()` (see that module's own section above), which
queries KAP directly, converts every candidate disclosure's `(year,
donem)` into a real `date()` object, and takes the genuine `max()` --
then deletes any other PDF already sitting in that fund's folder so a
stale file can never be parsed alongside the fresh one. The second return
value, `baseline_periods`, records exactly which `(year, donem)` ended up
being used per fund, since they are no longer forced to match.

**Never crashes on a bad fund**, by design: a fund with no registered/
resolvable KAP identity, a failed/empty download, or an empty parse result
are all caught individually, logged as `[UYARI]`, and simply omitted from
the result -- one bad fund never aborts the loop. Logs a final summary
(`X/Y fon basariyla toplandi, Z benzersiz hisse kodu bulundu`).

**`days_back` hard ceiling, discovered while testing this (2026-07-30):**
KAP's `FILTERYFBF` endpoint silently returns an empty list (`HTTP 200`,
`[]`, no error) for any `days_back` value of 366 or higher -- verified by
probing 30/90/180/365/366/400, where 365 returned real disclosures and
366+ returned zero every time. `collect_global_baseline` therefore
defaults to `days_back=365` and should not be raised past that.

## Shadow Portfolio engine, step 3: `build_tefas_power_matrix` (2026-07-30)

Knowing WHICH stocks a fund holds isn't enough to weigh a multi-fund
transaction -- that also needs to know HOW MUCH capital each co-filing
fund actually has to deploy, day by day. `build_tefas_power_matrix
(fund_codes, days_back=30)` (module-level, called with the FULL discovered
fund list from step 1 -- not just the subset that also had a KAP PDF
baseline, since a fund like `T3B`/`TGI` can have daily TEFAS data with no
monthly report at all) pulls the last `days_back` days of TEFAS AUM
("Toplam Deger") and portfolio distribution for every fund via this
project's existing TEFAS scraper, then computes a daily "Aktif Güç"
(active purchasing power) figure per fund:

```python
Aktif_Guc_TL = Toplam_AUM * (Hisse_Senedi_Orani + Likidite_Orani) / 100
```

`Likidite_Orani` sums every liquidity-category percentage in that day's
`Varliklar` (Repo/Ters-Repo, any "... Para Piyasası" money-market line,
and Mevduat) -- not just the equity percentage -- because a fund's real
ability to participate in a joint buy/sell isn't just its existing stock
position, it's stock PLUS readily deployable cash (see `INTERNAL_
ARCHITECTURE.md`, item 13, for the full reasoning behind including
liquidity here).

```python
tefas_power_matrix = build_tefas_power_matrix(related_funds_target_array, days_back=30)
# {"TLY": {"2026-07-31": 191393702793.58, ...}, "DOH": {...}, ...}
```

**Bridging out of the sandbox, on purpose:** this is the one place in
`kap_pdf_downloader/` that intentionally imports `fon_terminal/
data_scraper.py` (via a lazy `sys.path` bridge) -- TEFAS AUM/distribution
data only exists there, and reimplementing its Playwright WAF-bypass logic
here would be a maintenance hazard. `data_scraper.DATABASE_FILE` is
redirected to a sandbox-local `tefas_cache.json` (gitignored) for the
duration of the call, so this exploratory pipeline can never write an
unrequested fund into the live app's `fund_database.json` or add a
surprise tab to the real dashboard.

**Never crashes:** a total TEFAS handshake failure, a specific fund's
scrape failing, a fund with no stored records, a malformed/missing date,
a missing AUM figure, or an unparseable percentage are all caught
individually, logged as `[UYARI]`, and result in that fund/day being
skipped. A `Varliklar` dict that's present but missing the "Hisse Senedi"
key specifically is treated as a real 0% (the fund sold off its equities
that day); a wholly missing/empty `Varliklar` dict is treated as genuinely
unknown and skips that day entirely -- same distinction documented in
`fon_terminal/README.md`.

## Shadow Portfolio engine, step 4: `resolve_multi_fund_deltas` (2026-07-30)

Step 1 (`apply_delta`) leaves every multi-fund "Pay Alım Satım Bildirimi"
disclosure `unresolved` -- KAP never breaks its aggregate TL figure down
per fund.
`KAPDeltaEngine.resolve_multi_fund_deltas(unresolved, tefas_power_matrix,
updated_data)` estimates each fund's share of that aggregate using step
3's Aktif Güç values, rather than leaving the data gap unfilled:

1. **Timing:** uses the disclosure's own "İşlem Tarihi" (transaction date),
   not its "Bildirim Tarihi" (publish date, which can trail the real trade
   by days) -- see `INTERNAL_ARCHITECTURE.md`, item 12, for why this
   distinction matters.
2. **Pool:** sums every related fund's Aktif Güç on that transaction date
   into a "Toplam Güç Havuzu".
3. **Weight & estimate:** the target fund's weight is
   `hedef_fonun_gücü / toplam_havuz`; the disclosure's aggregate
   `net_nominal_tl` is multiplied by that weight to get an estimated lot
   amount for the target fund alone.
4. **Recording:** the estimate is added on top of `updated_data` (from
   `apply_delta`'s single-fund `resolved` merge) and returned separately
   as a `ProportionalResolution` list, so reporting code can clearly label
   it as an ESTIMATE, never a KAP-confirmed figure.

```python
updated_data, proportionally_resolved = engine.resolve_multi_fund_deltas(
    unresolved, tefas_power_matrix, updated_data
)
```

**Never crashes, never guesses past a missing input:** if ANY related
fund is missing TEFAS Aktif Güç data for that specific date, or the pool
sums to zero/negative, that ONE transaction is skipped with a `[UYARI]`
and left out of `updated_data` entirely -- silently assuming 0 for a
missing fund would artificially inflate every other fund's share, so a
transaction is either resolved with real data for every participant or
not resolved at all.

## Execution Trace: turning the pipeline into a readable story (2026-08-01)

Every step above already prints extensively to the console, but that
console log is a *technical* trace (one line per disclosure, per fund, per
day) -- not something a non-technical reader opening
`parser_kontrol_raporu.html` could follow. `kap_delta_engine._log_step`
adds a second, parallel narrative log for exactly that audience: a short
list of `{"time": "HH:MM:SS", "message": "..."}` entries appended at each
critical milestone (KAP baseline fetched, disclosures scanned and
classified, related funds discovered, TEFAS Aktif Güç calculated, the
proportional-distribution formula applied, etc.), phrased as a step-by-step
story rather than a raw dump.

`KAPDeltaEngine.execution_logs` (an instance attribute, always a list --
optionally seeded via the constructor's `execution_logs=` parameter so it
can be a single object shared across the whole `__main__` run) is where
its own methods (`apply_delta`, `discover_related_funds`,
`resolve_multi_fund_deltas`) append to; `collect_global_baseline` and
`build_tefas_power_matrix` accept the same shared list as an optional
parameter for the same purpose. Passing `execution_logs=None` anywhere
(the default) is a pure no-op -- every existing call site that doesn't
care about this feature behaves exactly as it did before.

`kap_pdf_parser._render_execution_log` renders the accumulated list as a
terminal-styled (dark background, monospaced, vertical timeline line)
block at the very TOP of `parser_kontrol_raporu.html` -- before the
per-period grid and every delta section -- via `delta_report[
"execution_logs"]`.
