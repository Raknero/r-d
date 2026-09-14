"""
report_dating.py

Establishes the TRUE as-of date of a KAP "Portfoy Dagilim Raporu" from the
report's own DATA, because as of 2026-09 none of its LABELS can be trusted.

Why this module exists
----------------------
Funds moved from monthly to weekly portfolio disclosure, but the documents
and their metadata kept the monthly-era labelling. For TLY's report
published 09.09.2026, every label says August and every piece of data says
September:

    KAP `donem` / `period`      35 / "HB"   (a WEEK number, not a month)
    KAP attachment filename     TLY_2026.08.pdf
    PDF page-1 heading          "Ağustos-2026"
    -------------------------------------------------------------------
    Header figures match        TEFAS 07.09.2026 (exactly, 3/3 fields)
    Holdings valued at          04.09.2026 BIST closes (exactly, 8/8)
    Holdings include buys on    01, 02, 03, 04 September

The old pipeline coerced those labels into a month and took that month's
last day (31.08.2026) as the baseline's validity date. That is 4 calendar
days EARLIER than the report's real snapshot, so every KAP transaction
notification from 01-04 September was applied on top of a baseline that
already contained it -- silent double counting, in the exact direction
that overstates the fund's activity.

How dating works instead
------------------------
Two independent, purely data-derived signals, neither of which involves a
label:

1. PRIMARY -- TEFAS header-figure match. The report's page-1 figures
   (`Toplam Değer/Net Varlık Değeri`, `Katılma Payı Sayısı`, and the unit
   price) appear verbatim in TEFAS's own daily series for the same fund.
   Matching all three pins the report to ONE TEFAS day, `tefas_date`.
   Verified unique and exact on all 6 reports on hand (2026-09-14).

   The snapshot itself is the TEFAS day BEFORE that one. TEFAS publishes a
   fund's price for day D from the previous business day's closes (the
   same T+1 convention `kap_delta_engine.resolve_multi_fund_deltas`
   already relies on), so the holdings were valued at the close of the
   preceding business day. Because TEFAS only lists business days, "the
   preceding business day" is simply the previous date present in the
   fund's own series -- no market-holiday calendar needed.

2. CROSS-CHECK -- newest `Satın Alış Tarihi` in the holdings table. A
   position bought on date D proves the snapshot is from D or later, so
   this is a hard LOWER bound that must not exceed the resolved date.
   Empirically it lands exactly ON it for every equity-holding report
   (weekly: 04.09.2026, monthly: 31.08.2026 / 31.07.2026).

Both cadences resolve correctly through the same rule, which is the point:
a monthly report resolves to its month end (31.08.2026 -- and the monthly
PDF even labels its own price "Ay Sonu Pay Fiyatı", month-END), while a
weekly one resolves to its week end. Nothing special-cases the cadence.

When it cannot be established, this module RAISES rather than guessing
(`ReportDatingError`). Falling back to a month end is precisely the
behaviour that produced the double-counting bug: it is always plausible,
silently wrong, and indistinguishable in the logs from a correct answer.
Callers skip the fund with a loud warning instead.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

from kap_pdf_parser import ReportFingerprint

# The sandbox-local TEFAS cache (see `load_tefas_records`) -- deliberately
# never the live app's fund_database.json.
TEFAS_CACHE_FILENAME = "tefas_cache.json"

# A report must match at least this many of the three header figures before
# a TEFAS day is accepted as its match. Two independent figures agreeing to
# full precision on the same day is already a very strong signal; one alone
# is not (a price can repeat across days).
MIN_MATCHING_FIGURES = 2

# Relative tolerance for comparing a PDF figure with a TEFAS figure, plus a
# per-field absolute floor for values near zero. Observed differences are
# exactly 0.0, so this only absorbs float round-tripping.
FIGURE_RELATIVE_TOLERANCE = 1e-7
FIGURE_ABSOLUTE_FLOOR = {"ToplamDeger": 0.01, "Pay": 0.5, "Fiyat": 1e-6}

# Largest acceptable gap between the matched TEFAS day and the resolved
# snapshot day. Consecutive business days are 1-3 days apart (Friday ->
# Monday, or across a public holiday). A larger gap means the fund's TEFAS
# series has a HOLE, so "the previous date present" is not actually the
# previous business day and the resolved date would be too early -- which
# would double-count. Refuse instead.
MAX_BUSINESS_DAY_GAP_DAYS = 5

# How recent the cached TEFAS series must be before it is reused as-is.
# Dating needs the matched day AND the day before it, so a cache that
# predates the report is useless.
CACHE_FRESHNESS_DAYS = 4


class ReportDatingError(RuntimeError):
    """Raised when a report's as-of date cannot be established from its
    data. Carries a Turkish, self-explanatory reason naming which signal
    failed, so the caller can log it verbatim."""


@dataclass
class AsOfResolution:
    """A successfully dated report.

    `as_of` is the load-bearing value: the date the holdings were valued,
    and therefore the last date whose transactions are ALREADY inside the
    PDF. The delta window must open on the day after it.
    """

    as_of: date
    tefas_date: date
    matched_figures: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    @property
    def delta_start(self) -> date:
        """First date whose KAP buy/sell notifications may be layered on
        top of this baseline without double-counting."""
        return self.as_of + timedelta(days=1)


def _parse_tefas_date(raw: object) -> Optional[date]:
    """Parses TEFAS's own "DD.MM.YYYY" `Tarih` field. Returns None instead
    of raising for anything unrecognized, so one malformed record can
    never abort the dating of a whole report."""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _figures_match(pdf_value: float, tefas_value: object, field_name: str) -> bool:
    """True when a PDF header figure and a TEFAS field agree to within
    `FIGURE_RELATIVE_TOLERANCE` (see that constant)."""
    try:
        actual = float(tefas_value)
    except (TypeError, ValueError):
        return False
    tolerance = max(
        abs(pdf_value) * FIGURE_RELATIVE_TOLERANCE,
        FIGURE_ABSOLUTE_FLOOR.get(field_name, 0.0),
    )
    return abs(actual - pdf_value) <= tolerance


def resolve_as_of_date(
    fon_kodu: str,
    fingerprint: ReportFingerprint,
    tefas_records: Sequence[dict],
) -> AsOfResolution:
    """Resolves the report's true as-of date (see the module docstring for
    the two signals and why labels are not among them).

    Raises `ReportDatingError` -- never returns an approximation -- when
    the figures match no TEFAS day, match more than one, or contradict the
    holdings' own purchase dates.
    """
    figures = fingerprint.header_figures
    if len(figures) < MIN_MATCHING_FIGURES:
        raise ReportDatingError(
            f"[{fon_kodu}] PDF'in başlık rakamları okunamadı "
            f"(bulunan alanlar: {sorted(figures) or 'yok'}); en az {MIN_MATCHING_FIGURES} alan gerekli. "
            f"Rapor tarihi veriden doğrulanamaz."
        )

    by_date: Dict[date, dict] = {}
    for record in tefas_records or []:
        parsed = _parse_tefas_date(record.get("Tarih"))
        if parsed is not None:
            by_date[parsed] = record

    if not by_date:
        raise ReportDatingError(
            f"[{fon_kodu}] TEFAS günlük serisi boş/okunamadı; rapor tarihi doğrulanamaz."
        )

    ordered_dates = sorted(by_date)
    matches = [
        day
        for day in ordered_dates
        if all(
            _figures_match(value, by_date[day].get(field_name), field_name)
            for field_name, value in figures.items()
        )
    ]

    if not matches:
        newest = ordered_dates[-1]
        raise ReportDatingError(
            f"[{fon_kodu}] PDF'in başlık rakamları ({', '.join(sorted(figures))}) "
            f"TEFAS serisinin hiçbir gününde bulunamadı "
            f"(seri: {ordered_dates[0]} – {newest}, {len(ordered_dates)} gün). "
            f"TEFAS verisi bu raporu kapsamıyor olabilir."
        )
    if len(matches) > 1:
        raise ReportDatingError(
            f"[{fon_kodu}] PDF'in başlık rakamları birden fazla TEFAS gününe uyuyor "
            f"({[d.isoformat() for d in matches]}); rapor tarihi tek bir güne indirgenemedi."
        )

    tefas_date = matches[0]
    earlier = [day for day in ordered_dates if day < tefas_date]
    if not earlier:
        raise ReportDatingError(
            f"[{fon_kodu}] Rapor TEFAS'ta {tefas_date} gününe eşleşti, ancak seride bu günden "
            f"ÖNCEKİ hiçbir iş günü yok; değerleme günü belirlenemedi (TEFAS geçmişi yetersiz)."
        )

    as_of = earlier[-1]
    gap = (tefas_date - as_of).days
    if gap > MAX_BUSINESS_DAY_GAP_DAYS:
        raise ReportDatingError(
            f"[{fon_kodu}] TEFAS serisinde boşluk var: eşleşen gün {tefas_date}, ondan önceki "
            f"kayıtlı gün {as_of} ({gap} gün fark, izin verilen en fazla "
            f"{MAX_BUSINESS_DAY_GAP_DAYS}). Bir önceki İŞ GÜNÜ güvenilir şekilde "
            f"belirlenemediği için tarih reddedildi (erken bir tarih çift saymaya yol açar)."
        )

    evidence = [
        f"TEFAS eşleşmesi: {', '.join(f'{k}={figures[k]!r}' for k in sorted(figures))} "
        f"-> TEFAS Tarih={tefas_date} (tek eşleşme, {len(ordered_dates)} gün tarandı)",
        f"Değerleme günü = eşleşen günden önceki kayıtlı TEFAS iş günü = {as_of} "
        f"({gap} gün önce; TEFAS T+1 yayınladığı için fiyat bir önceki kapanışı yansıtır)",
    ]

    purchase = fingerprint.newest_purchase_date
    if purchase is None:
        evidence.append(
            "Çapraz kontrol yok: portföyde hisse satırı (Satın Alış Tarihi) bulunmadı "
            "-- para piyasası/borçlanma ağırlıklı fon olabilir."
        )
    elif purchase > as_of:
        raise ReportDatingError(
            f"[{fon_kodu}] İki bağımsız sinyal çelişiyor: TEFAS eşleşmesi değerleme gününü "
            f"{as_of} veriyor, ancak portföyde {purchase} tarihli (daha YENİ) bir alım var. "
            f"Bir pozisyon, alındığı tarihten önceki bir fotoğrafta yer alamaz; tarih "
            f"belirsiz olduğu için reddedildi."
        )
    elif purchase == as_of:
        evidence.append(
            f"Çapraz kontrol GEÇTİ: portföydeki en yeni Satın Alış Tarihi ({purchase}) "
            f"değerleme günüyle birebir aynı."
        )
    else:
        evidence.append(
            f"Çapraz kontrol GEÇTİ: portföydeki en yeni Satın Alış Tarihi ({purchase}) "
            f"değerleme gününden ({as_of}) önce -- fon o gün işlem yapmamış, çelişki yok."
        )

    if fingerprint.declared_period_label:
        evidence.append(
            f"PDF'in kendi dönem etiketi ({fingerprint.declared_period_label!r}) "
            f"BİLEREK yok sayıldı; ritim={fingerprint.cadence or 'bilinmiyor'}."
        )

    return AsOfResolution(
        as_of=as_of,
        tefas_date=tefas_date,
        matched_figures=sorted(figures),
        evidence=evidence,
    )


def load_tefas_records(
    fund_codes: Sequence[str],
    days_back: int = 45,
    scrape_if_stale: bool = True,
) -> Dict[str, List[dict]]:
    """Returns `{FON_KODU: [TEFAS gunluk kaydi, ...]}` for dating, reading
    this sandbox's own `tefas_cache.json` and topping it up from TEFAS when
    it is missing a fund or has gone stale (see `CACHE_FRESHNESS_DAYS`).

    Uses the same deliberate, documented `sys.path` bridge to
    `fon_terminal/data_scraper.py` as `kap_delta_engine.
    build_tefas_power_matrix`, and redirects `data_scraper.DATABASE_FILE`
    to the sandbox cache for the duration of the call so this can never
    write into the live dashboard's `fund_database.json`.

    Topping up is cheap on purpose: since the v5.3 bulk rewrite one
    all-funds request serves every code at once, so fetching what dating
    needs costs ~2 requests regardless of how many funds are asked for.

    Never raises: a failed import, a failed handshake or a failed scrape
    leaves whatever was already cached (possibly nothing) and is reported
    as a "[UYARI]" -- the caller then gets a `ReportDatingError` from
    `resolve_as_of_date`, which is the honest outcome rather than a
    fabricated date.
    """
    wanted = [code.strip().upper() for code in fund_codes if code and code.strip()]
    if not wanted:
        return {}

    sandbox_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(sandbox_dir)  # fon_terminal/
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        import data_scraper
    except ImportError as exc:
        print(f"[UYARI] TEFAS veri modulu import edilemedi, rapor tarihleme icin veri yok: {exc}")
        return {}

    data_scraper.DATABASE_FILE = os.path.join(sandbox_dir, TEFAS_CACHE_FILENAME)

    def read_cache() -> Dict[str, List[dict]]:
        try:
            database = data_scraper.load_database()
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not crash dating
            print(f"[UYARI] TEFAS cache okunamadi ({data_scraper.DATABASE_FILE}): {exc}")
            return {}
        out: Dict[str, List[dict]] = {}
        for code in wanted:
            entry = database.get(code) or {}
            records = entry.get("records") if isinstance(entry, dict) else entry
            out[code] = list(records or [])
        return out

    cached = read_cache()

    if scrape_if_stale:
        threshold = date.today() - timedelta(days=CACHE_FRESHNESS_DAYS)
        stale = []
        for code in wanted:
            dates = [d for d in (_parse_tefas_date(r.get("Tarih")) for r in cached.get(code, [])) if d]
            if not dates or max(dates) < threshold:
                stale.append(code)

        if stale:
            print(
                f"[BILGI] Rapor tarihleme icin TEFAS verisi tazeleniyor "
                f"({len(stale)} fon: {stale}, son {days_back} gun)."
            )
            try:
                data_scraper.scrape_and_update(stale, days_back=days_back)
            except Exception as exc:  # noqa: BLE001 - see docstring
                print(f"[UYARI] TEFAS verisi tazelenemedi, elde olan cache ile devam ediliyor: {exc}")
            else:
                cached = read_cache()

    return cached
