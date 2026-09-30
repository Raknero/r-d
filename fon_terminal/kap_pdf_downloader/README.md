# KAP PDF Downloader & Parser (Sandbox)

TLY had years of positive returns by concentrating in a few names and
absorbing the swings from cash; newer funds in the same style were
already running the same structure. This sandbox opens that portfolio: **what the fund holds, what it buys and sells, and how
concentrated those holdings are** — the structure while it still works,
and the break when the liquid/illiquid mix goes. The
[terminal](../README.md) is the daily series (whale radar, cash buffer).
What those reports showed before September 2026 is in the
[case study](../case_study/CASE_STUDY_2026_09.md).

![Execution trace](img/report_overview.png)

*Frozen product shot — last operating session is 16.09.2026. After that
TEFAS prints 0 and BIST names are halted or limit-down; do not refresh
these "current weight" columns from live Yahoo/TEFAS. The pipeline now
caps BIST closes and TEFAS AUM at that date. The screenshots themselves
are from an earlier HTML run (AUM header 26.08); they show the report
layout, not a live post-liquidation book.*

The HTML report opens with an execution trace: which KAP endpoints were
called, which filings were kept or dropped, and how multi-fund trades
were classified. Holdings parsed from a PDF:

![Holdings table](img/report_holdings.png)

And the estimated holdings since that baseline — weights, lot changes, BIST
prices as of 16.09.2026:

![Portfolio evolution](img/report_evolution.png)

Standalone, isolated module for downloading a Turkish investment fund's
"Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF
attachments from KAP (Kamuyu Aydinlatma Platformu / Public Disclosure
Platform). `TLY` (Tera Portfoy Birinci Serbest Fon) is the fund this
project was built around and remains explicitly pinned, but as of
2026-07-30 **any fund code KAP tracks works out of the box** via a
dynamic fund directory -- see "Fund resolution" below.

This directory lives inside `fon_terminal/` but is intentionally
decoupled from the rest of the application -- it has its own
`requirements.txt` and does not import anything from `fon_terminal/`'s
own modules (`data_scraper.py`, `main.py`, etc.), with exactly two
documented exceptions (`build_tefas_power_matrix`, see Step 3 below, and
`report_dating.load_tefas_records`, which reuses the same bridge). The
four modules here (`kap_downloader.py`, `kap_pdf_parser.py`,
`report_dating.py`, `kap_delta_engine.py`) only depend on each other and
third-party packages, so the whole folder can still be lifted into
another project as a self-contained unit.

Beyond the base download/parse pair, this sandbox has grown into a full
**"Shadow Portfolio" pipeline**: `kap_delta_engine.py` bridges the gap
between KAP's PDF reports by layering the buy/sell
disclosures filed since each report's measured valuation date on top of
it (Step 0-1), discovers every other fund sharing
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
   days as JSON, including `disclosureIndex`, `year`, `donem`, `period`,
   and `attachmentCount`. `donem` is a month for monthly filings and a
   week number for weekly ones -- it is never treated as a date, see
   "Weekly reports" below.
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

### Weekly reports: the period label is not a date (2026-09-09)

**This is the most important behavioral rule in the whole sandbox, so it
is stated first: a report's period label is metadata, not a date. The
report's own contents are the only source for when its holdings were
valued.** Everything below explains why that rule had to be learned the
hard way.

Turkish funds became required to publish their "Portfoy Dagilim Raporu"
**weekly** instead of monthly. KAP's schema was not extended for this --
the same two fields are simply reused with different meanings:

| Field | Monthly filing | Weekly filing |
|---|---|---|
| `period` | `"AB"` | `"HB"` |
| `donem` | month (1..12) | KAP's week ordinal as filed (e.g. `34`, `35`) — not an ISO week, and not a date |

Neither the attachment filename nor the PDF's own header was updated by
the filers. TLY's report published **09.09.2026** arrives as
`donem=35, period="HB"`, is attached as `TLY_2026.08.pdf`, and page 1
still reads **"Ağustos-2026"** -- while its holdings are valued
**04.09.2026**. ISO week 35 of 2026 ended 30.08; TLY's `HB35` is later
than that, so the ordinal is KAP's filing number, not a calendar week.
The figures inside are correct; only every label around them is wrong.

#### Two separate bugs this caused

**1. Crash.** A raw `34` flowed into `date(year, donem, 1)` in
`find_latest_report` -> `ValueError: month must be in 1..12` (and
`calendar.monthrange` in `kap_delta_engine` next).

**2. Silent double-counting -- much worse than the crash.** The first fix
attempt (`normalize_report_period`, now removed) forced `donem` back into
a month by falling back to `publishDate` minus one month. That stopped the
crash and introduced two invisible failures:

- **Reports overwrote each other.** `2026_HB34` and `2026_HB35` are
  genuinely different weekly reports, but both normalized to
  `August 2026` and both were named `TLY_2026_08.pdf`, so deduplication
  dropped one and the file system clobbered the other.
- **The baseline date was up to a week too early.** "August 2026" implied
  a 31.08.2026 month end, so the delta window opened 01.09.2026 -- but
  the PDF's holdings were already valued 04.09.2026. Every disclosure
  filed 01.09-04.09 was therefore applied **on top of a baseline that
  already contained it**. Measured on TLY: **8 multi-fund transactions
  double-counted**, with no error, no warning, and plausible-looking
  output.

#### The fix: measure the date, never derive it

Period metadata is now preserved exactly as filed and never converted to
a month. `ReportPeriod` (`kap_downloader.py`) holds `year`, `ordinal`
(raw `donem`), `code` (raw `period`) and a derived `cadence`, exposing:

- `slug` -> `2026_HB35`, used for the local filename
  (`TLY_2026_HB35.pdf`), for deduplication, and for old-file cleanup, so
  two weekly reports can no longer collide.
- `label` -> `"2026 / 35. hafta (HB)"`, for display only.

"Latest" is decided by **publication instant** (`DisclosureRecord.
sort_key`, from `publishDate`) rather than by reconstructing a date out
of the period, so it works identically for weekly, monthly, and any
future cadence KAP invents.

The attachment filename is no longer treated as authoritative either.
`_confirm_period_from_attachment` (which used to let the filename
override KAP's metadata) is gone -- it was itself a source of collisions,
since two different weekly reports share the filename `TLY_2026.08.pdf`.
`_note_attachment_period` replaces it and only logs the discrepancy for
the audit trail.

The actual valuation date comes from `report_dating.py` (see its own
section below), which reads it out of the PDF's contents. Nothing in the
pipeline infers a date from a label anymore.

---

## `report_dating.py` -- establishing a report's real valuation date

Once labels are known to be unreliable, the pipeline still needs one hard
number: **as of which day are these holdings true?** The delta window
opens the day after it, so being a few days early silently double-counts
and a few days late silently drops trades.

`resolve_as_of_date(fon_kodu, fingerprint, tefas_records)` derives it from
evidence only:

1. **Primary signal -- match the PDF against TEFAS history.**
   `KAPPdfParser.extract_fingerprint` pulls the report's own header
   figures (Toplam Değer / Net Varlık Değeri, Katılma Payı Sayısı, and
   the Haftalık/Ay Sonu Pay Fiyatı) and these are matched against the
   fund's daily TEFAS records. Those three numbers together identify a
   single trading day essentially uniquely -- a fund's unit price and
   share count never coincidentally repeat.
2. **T+1 correction.** TEFAS publishes a day's price on the following
   day, so the matched TEFAS row is shifted back to the previous recorded
   business day to get the actual valuation date.
3. **Cross-check -- the holdings must not contain the future.** The
   newest "SATIN ALIŞ TARİHİ" in the equities table has to be consistent
   with the resolved date. This is what exposed the original problem: an
   "Ağustos-2026" report contained purchases dated after 31.08.
4. **Refuse rather than guess.** If the figures match no day, match
   several ambiguously, or the cross-check fails, it raises
   `ReportDatingError`. The caller skips that fund loudly. Quietly
   falling back to a month end is precisely the behavior that caused the
   double-counting, so it is not offered as a fallback.

Verified on consecutive weekly filings, which is what makes the rule
trustworthy rather than a one-off patch:

| Report | Label says | Measured valuation date |
|---|---|---|
| `TLY_2026_HB34` | Ağustos-2026 | 31.08.2026 |
| `TLY_2026_HB35` | Ağustos-2026 | 04.09.2026 |

Both are labeled the same month; the two dates are 4 days apart, and only
the measured pair produces a correct, non-overlapping delta window.

TEFAS records are loaded via `load_tefas_records`, which reads the
sandbox-local `tefas_cache.json` and only re-scrapes when it is stale --
reusing `data_scraper` through the same `sys.path` bridge documented in
Step 3 below, so no TEFAS logic is duplicated here and the live app's
`fund_database.json` is never touched.

## Usage

```python
from kap_downloader import KAPPdfDownloader

with KAPPdfDownloader(fon_kodu="TLY") as downloader:
    results = downloader.download_reports(days_back=365)

# Or narrow down to a publication-date range without changing how far
# back KAP itself is queried. This filters on when KAP PUBLISHED the
# report, which is the only date its metadata actually carries -- a
# report's holdings-valuation date is a separate thing entirely, read
# from the PDF itself (see `report_dating.py`).
with KAPPdfDownloader(fon_kodu="TLY") as downloader:
    downloader.download_reports(
        days_back=365,
        published_since=date(2026, 7, 1),
        published_until=date(2026, 9, 30),
    )
```

Or run it directly:

```bash
pip install -r requirements.txt
python kap_downloader.py
```

PDFs are saved into a `tly_pdfs/` folder (created automatically) as
`TLY_{ReportPeriod.slug}.pdf` -- i.e. `TLY_2026_AB06.pdf` for the 6th
month's monthly filing and `TLY_2026_HB35.pdf` for the 35th week's weekly
one. The cadence code is part of the name on purpose: without it, a
weekly report would overwrite the monthly one covering the same month.

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
holdings = parser.parse_file("tly_pdfs/TLY_2026_HB35.pdf")
# {"ALKLC": 731256.0, "CWENE": 3000000.0, ...}

# Whole directory, keyed by the period slug parsed from each filename
history = parser.parse_directory("tly_pdfs")
# {"2026_AB06": {...}, "2026_AB07": {...}, "2026_HB35": {...}}

# When the holdings' actual valuation date matters (it always does for
# delta math), read it from the PDF rather than from the key above:
fingerprint = parser.extract_fingerprint("tly_pdfs/TLY_2026_HB35.pdf")
# fingerprint.declared_period_label == "Ağustos-2026"  <- do NOT trust
# fingerprint.unit_price / .share_count / .total_value <- match vs TEFAS
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
standalone HTML file (inline CSS; Chart.js loaded from jsDelivr CDN when
delta charts are present) with one table per period: bordered,
zebra-striped, hover-highlighted rows, right-aligned Turkish-formatted
numbers (`731256.0` -> `731.256,00`), and a `TOPLAM` footer row per table
so the sum can be eyeballed against the PDF's own "GRUP TOPLAMI" line.
The whole report uses a financial-terminal dark theme (slate surfaces,
soft typography, matte emerald/brick semantic colors -- no neon reds or
bright beige warning boxes).

Running `python kap_pdf_parser.py` now does both steps: prints the parsed
holdings to the console, then writes `parser_kontrol_raporu.html` into the
current directory for visual verification.

**Optional delta sections (2026-07-28, extended through 2026-08-03):**
`export_to_html` also accepts an optional `delta_report` dict (produced by
`kap_delta_engine.py` -- see below), shaped as `{"fon_kodu",
"baseline_period", "baseline_as_of", "baseline_delta_start",
"baseline_data", "resolved", "unresolved",
"proportionally_resolved", "updated_data", "tefas_power_matrix",
"current_prices", "current_aum", "current_aum_date",
"execution_logs"}`. `baseline_period` is the KAP filing label (not a
date); `baseline_as_of` / `baseline_delta_start` are the measured
valuation window from `report_dating.py`. When provided, the SAME `parser_kontrol_raporu.html`
file gets extended with, in order:

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
5. **"Hisse Bazlı Portföy Evrimi (Lot Değişim Özeti)"** -- baseline + (2)
   + (4), reshaped ticker-by-ticker with since-baseline % change, BIST
   price/weight columns when available, Chart.js visuals above the table,
   and a click-to-expand day-by-day transaction history (see its own
   section below).
6. **"Günlük Aktif Satın Alma Gücü (TEFAS Havuzu)"** -- the raw
   `tefas_power_matrix` values that (4)'s weighting was based on, one row
   per date (most recent first), one column per fund.

`delta_report=None` (the default) renders exactly the original per-period
report with no extra sections; any individual key missing from
`delta_report` (e.g. an older caller that predates `baseline_data`,
`tefas_power_matrix`, or `execution_logs`) just renders that one section
as an explicit "veri bulunamadı" notice (or, for `execution_logs`, renders
nothing at all) rather than breaking the rest of the report. This module
still has zero import dependency on `kap_delta_engine.py` -- the caller
passes plain dicts/lists, never the other module's dataclasses.

### "Hisse Bazlı Portföy Evrimi (Lot Değişim Özeti)": context over raw deltas (2026-08-03)

A raw cumulative number like "PEKGY: -33.528.112,76 lot" (or the earlier
"Güncel Portföy Son Durumu" section this replaces, which only showed the
FINAL lot count) answers "what" but not "how much does that actually
matter" or "did that happen in one shot or gradually" -- exactly the
feedback that motivated this section. Each row now shows the FULL journey
per ticker:

| Sütun | Anlamı |
|---|---|
| Başlangıç Lot | `baseline_data[ticker]` -- KAP PDF baseline, 0 if the ticker didn't exist yet. |
| Kesinleşen Delta Lot | Net (signed) sum of every single-fund `resolved` entry for this ticker. |
| Oransal Tahmini Delta Lot | Net (signed) sum of every `proportionally_resolved` entry for this ticker. |
| İşlem Tarihçesi | Click-to-expand `<details>` list of every INDIVIDUAL dated entry behind the two delta columns above (see below). |
| Güncel Tahmini Lot | Başlangıç + Kesinleşen + Oransal. |
| Taban Tarihinden Beri Lot Değişimi (%) | `(Güncel - Başlangıç) / Başlangıç * 100`, matte emerald if positive, matte brick-red if negative. A ticker with Başlangıç Lot == 0 (division by zero is meaningless, not just an edge case) renders `"YENİ HİSSE"` here instead, unless it also nets out to exactly 0 (rendered as a plain `-`). **This covers only the span since the baseline PDF's measured valuation date** -- it resets when the next KAP PDF becomes the new baseline; it is not a long-term trend. |
| Güncel Fiyat | BIST close from yfinance (`TICKER.IS`) on the last operating session (16.09.2026), or `-` if unavailable. Not today's print. |
| Güncel Ağırlık (%) | `(Güncel Tahmini Lot × Güncel Fiyat) / fon AUM * 100` when both price and AUM exist; otherwise `-`. AUM is the last TEFAS row on or before that same date with a positive unit price. |

When AUM/prices are available, rows are sorted by **Güncel Ağırlık (%)**
descending (largest portfolio weight first); tickers without a computable
weight fall to the end, ordered by absolute lot change. Without prices,
sorting falls back to absolute lot-change magnitude.

**Short-window caveat (UI, 2026-08-06; re-dated 2026-09-09):** the HTML
section renders a bold warning above the table stating that lot-change
percentages do **not** reflect long-term investment trend -- only activity
since the baseline PDF's valuation date -- and reset when that PDF rolls
forward.

The original wording said "ay başından beri" / "ay sonu" and the column
was named "Ay Başından Beri Lot Değişimi (%)", which stopped being true
the moment KAP went weekly: the window is now often 4-7 days, not a month.
Both were renamed to **"Taban Tarihinden Beri Lot Değişimi (%)"**, and the
warning now prints the MEASURED valuation date it was computed from
(`delta_report["baseline_as_of"]`, e.g. "04.09.2026") plus an explicit
note that the period label is not a date. When `baseline_as_of` is absent
(an older caller), the copy falls back to naming the baseline generically
rather than printing a date the pipeline never established. The
calculation engine is unchanged -- this is wording and provenance only.

**Chart.js visuals (UI, 2026-08-06):** above the evolution table,
`_render_evolution_charts` injects a `const chartData = ...` payload
(`json.dumps` from the same row tuples the table already computed -- no
engine recalculation) and two canvases via Chart.js CDN:

1. **Güncel Ağırlık Dağılımı** (doughnut) -- top 10 tickers by weight;
   remainder collapsed into a single "Diğerleri" slice.
2. **Taban Tarihinden Beri Lot Değişimi (%)** (bar) -- only tickers with a
   non-zero since-baseline %; buys emerald, sells brick-red. If every
   change is zero, an HTML placeholder replaces the bar canvas:
   "Taban raporunun veri tarihinden beri yeni işlem (delta)
   bulunmamaktadır."

Chart chrome (legend/ticks/grid) follows the same terminal dark palette as
the rest of the report (soft slate text, low-opacity grid lines).

**"İşlem Tarihçesi" column (2026-08-03):** the cumulative delta columns
answer "how much changed", but a user correctly pointed out they don't
answer "did that happen all at once, or gradually, and on which dates?" --
a "-69 milyon lot" total could be one disclosure or ten. `kap_pdf_parser.
_aggregate_signed_lot_deltas` now ALSO builds a per-ticker,
chronologically-sorted (oldest first) list of every individual entry that
fed into the cumulative total -- `{"date": "23/07/2026", "lot":
42469924.0, "type": "Kesinleşen"}` -- and `_render_transaction_history_cell`
renders it as a collapsed `<details>`/`<summary>` disclosure ("3 İşlem
Göster") so the table stays scannable by default while the full
day-by-day story (`[Tarih]: [İşaretli Lot] ([Tip])`, one `<li>` per entry)
is one click away. A ticker with zero entries (e.g. one whose Kesinleşen
AND Oransal deltas both happened to net to zero, or one that simply never
appeared in any disclosure) renders a plain "İşlem Yok" instead of an
empty, misleadingly-clickable disclosure. This is purely additive --
expanding it never changes the cumulative Başlangıç/Kesinleşen/Oransal/
Güncel/% figures next to it, which are computed exactly as before.

---

## `kap_delta_engine.py` -- bridging the gap between reports

`KAPPdfParser` gives an exact holdings snapshot, but only as often as KAP
publishes the next "Portfoy Dagilim Raporu" (monthly historically, weekly
since 2026-09). `KAPDeltaEngine` keeps that snapshot current in between
reports by layering KAP's buy/sell disclosures on top of it.

### Where the delta window starts: `date_baseline_report`

The single most failure-prone number in this module is the delta window's
first day, because both directions of error are silent: too early
re-applies trades the PDF already contains, too late drops trades
entirely.

`date_baseline_report(fon_kodu, pdf_path, tefas_records)` produces it by
delegating to `report_dating.resolve_as_of_date` (see its section above)
and returning an `AsOfResolution` whose `delta_start` is simply
`as_of + 1 day` -- the PDF is complete through its valuation date, so the
deltas start the day after.

This replaced `baseline_period_end_date()` /
`baseline_period_to_delta_start()`, which computed the same thing from the
period label via `calendar.monthrange`. That was correct only while every
report was monthly; for a weekly report labeled with a month it produced a
start date up to a week too early, and double-counted every disclosure in
between (measured: 8 transactions on TLY).

If the date cannot be established, `ReportDatingError` propagates: a
single fund is skipped with a `[UYARI]` inside `collect_global_baseline`,
and `__main__` exits with an explanation instead of continuing on a
guessed date. Deliberately chosen over a month-end fallback, since that
fallback is exactly what caused the double-counting.

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
   page `KAPPdfDownloader` reads for the allocation report's PDF link, but
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
from datetime import date
from kap_pdf_parser import KAPPdfParser
from kap_delta_engine import KAPDeltaEngine, date_baseline_report
from report_dating import load_tefas_records

pdf_path = "tly_pdfs/TLY_2026_HB35.pdf"
baseline = KAPPdfParser().parse_file(pdf_path)

# Never hand-pick `start_date` off the period label -- measure it.
dating = date_baseline_report("TLY", pdf_path, load_tefas_records(["TLY"])["TLY"])

with KAPDeltaEngine(fon_kodu="TLY") as engine:
    updated, resolved, unresolved = engine.apply_delta(
        baseline,
        start_date=dating.delta_start.isoformat(),   # 2026-09-05, not 2026-09-01
        end_date=date.today().isoformat(),
    )

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
`FILTERYFBF` endpoint `KAPPdfDownloader` uses for the allocation report,
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
global_baseline, baseline_datings = collect_global_baseline(related_funds)
# global_baseline:  {"TLY": {"ALKLC": 731256.0, ...}, "DOH": {...}, ...}
# baseline_datings: {"TLY": AsOfResolution(as_of=date(2026, 9, 4), ...), ...}
```

The second return value used to be `baseline_periods`, a
`{fon: (year, donem)}` map of KAP period labels. It is now
`{fon: AsOfResolution}` -- each fund's MEASURED valuation date plus the
evidence behind it (see `report_dating.py`), because the label alone
cannot bound a delta window. Funds legitimately differ here in both
report period AND valuation date: in a verified run TLY/DOH/TMV resolved
to 04.09.2026 from weekly filings while THF resolved to 31.08.2026 from a
monthly one, all in the same pass.

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
queries KAP directly and takes the report with the newest PUBLICATION
instant -- then deletes any other PDF already sitting in that fund's
folder so a stale file can never be parsed alongside the fresh one.

That "newest publication instant" ordering is itself part of the
2026-09-09 weekly-report fix: it previously sorted by a `date()` rebuilt
from the normalized `(year, donem)`, which cannot order two reports whose
labels collapse to the same month (`HB34` and `HB35` both said August
2026). `publishDate` is unambiguous for every cadence.

**Never crashes on a bad fund**, by design: a fund with no registered/
resolvable KAP identity, a failed/empty download, a valuation date that
cannot be established from the PDF (`ReportDatingError` -- see
`report_dating.py` above), or an empty parse result are all
caught individually, logged as `[UYARI]`, and simply omitted from the
result -- one bad fund never aborts the loop. Logs a final summary
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
allocation report at all) pulls the last `days_back` days of TEFAS AUM
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
data only exists there, and reimplementing its TEFAS session handshake
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
terminal-styled (dark slate card, monospaced, vertical timeline line)
block at the very TOP of `parser_kontrol_raporu.html` -- before the
per-period grid and every delta section -- via `delta_report[
"execution_logs"]`. The same financial-terminal dark palette used for the
rest of the report (cards, tables, Chart.js) is derived from this log
chrome so the page reads as one surface rather than a light report with a
dark log bolted on.
