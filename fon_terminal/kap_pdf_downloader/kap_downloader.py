"""
kap_downloader.py

Standalone, self-contained module for downloading a Turkish investment
fund's monthly "Portfoy Dagilim Raporu" (Portfolio Allocation Report) PDF
attachments from KAP (Kamuyu Aydinlatma Platformu / Public Disclosure
Platform).

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
from typing import Dict, List, Optional, Tuple

import requests


@dataclass
class DisclosureRecord:
    """One "Portfoy Dagilim Raporu" entry from KAP's disclosure filter
    API, trimmed down to only the fields this module needs."""

    disclosure_id: str
    disclosure_index: int
    year: int
    donem: int
    attachment_count: int
    publish_date: str


class KAPPdfDownloader:
    """Downloads monthly "Portfoy Dagilim Raporu" (Portfolio Allocation
    Report) PDF attachments for a tracked KAP-listed fund.

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
    human-readable fund code, so each supported fund is registered in
    `KNOWN_FUNDS` below; add an entry there to support a new fund code.
    """

    BASE_URL = "https://kap.org.tr"
    FILTER_ENDPOINT = "/tr/api/disclosure/filter/FILTERYFBF/{company_oid}/{member_oid}/{days_back}"
    DETAIL_PAGE = "/tr/Bildirim/{disclosure_index}"
    DOWNLOAD_ENDPOINT = "/tr/api/file/download/{attachment_id}"

    REPORT_TITLE = "Portfoy Dagilim Raporu"

    # GUIDs captured by reverse-engineering KAP's frontend network calls
    # for each fund's disclosure page. There is no public lookup endpoint
    # that resolves a bare fund code (e.g. "TLY") to these IDs, so new
    # funds must be registered here manually.
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
        output_dir: str = "tly_pdfs",
        request_delay: float = 0.5,
        timeout: int = 30,
    ):
        fon_kodu = fon_kodu.strip().upper()
        if fon_kodu not in self.KNOWN_FUNDS:
            supported = ", ".join(self.KNOWN_FUNDS)
            raise ValueError(
                f"'{fon_kodu}' fonu icin KAP company/member OID bilgisi tanimli degil. "
                f"Desteklenen fonlar: {supported}. Yeni bir fon eklemek icin "
                "KNOWN_FUNDS sozlugune kayit ekleyin."
            )

        self.fon_kodu = fon_kodu
        self.fund_config = self.KNOWN_FUNDS[fon_kodu]
        self.output_dir = output_dir
        self.request_delay = request_delay
        self.timeout = timeout
        self.session = self._build_session()

        os.makedirs(self.output_dir, exist_ok=True)

    # --- Context manager support --------------------------------------------

    def __enter__(self) -> "KAPPdfDownloader":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Closes the underlying HTTP session/connection pool."""
        self.session.close()

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
        deduplicated so only the most-recently-published report per
        (year, donem) is kept (KAP occasionally republishes a corrected
        report for a period that was already reported).
        """
        url = self.BASE_URL + self.FILTER_ENDPOINT.format(
            company_oid=self.fund_config["company_oid"],
            member_oid=self.fund_config["member_oid"],
            days_back=days_back,
        )

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
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

        best_by_period: Dict[Tuple[int, int], DisclosureRecord] = {}
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
            if year is None or donem is None or disclosure_index is None:
                continue

            record = DisclosureRecord(
                disclosure_id=basic.get("disclosureId"),
                disclosure_index=disclosure_index,
                year=year,
                donem=donem,
                attachment_count=attachment_count,
                publish_date=basic.get("publishDate") or "",
            )

            period_key = (record.year, record.donem)
            existing = best_by_period.get(period_key)
            if existing is None or record.publish_date > existing.publish_date:
                best_by_period[period_key] = record

        records = sorted(best_by_period.values(), key=lambda r: (r.year, r.donem))
        print(
            f"[BILGI] [{self.fon_kodu}] {len(records)} adet '{self.REPORT_TITLE}' bulundu "
            f"(son {days_back} gun icinde)."
        )
        return records

    # --- Stage 2: resolve the real attachment ID, then download --------------

    def _resolve_attachment(self, disclosure_index: int) -> Optional[Tuple[str, str]]:
        """Fetches the disclosure's public detail page and extracts the
        real attachment file ID + display filename for its PDF.

        Returns (attachment_id, filename), or None if no PDF attachment
        could be found on the page.
        """
        url = self.BASE_URL + self.DETAIL_PAGE.format(disclosure_index=disclosure_index)
        try:
            response = self.session.get(
                url, timeout=self.timeout, headers={"Accept": "text/html,application/xhtml+xml"}
            )
            response.raise_for_status()
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
            response = self.session.get(url, timeout=self.timeout, headers={"Accept": "application/pdf,*/*"})
            response.raise_for_status()
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
        start_period: Optional[Tuple[int, int]] = None,
        end_period: Optional[Tuple[int, int]] = None,
    ) -> List[dict]:
        """Downloads every available monthly "Portfoy Dagilim Raporu" PDF
        for this fund published in the last `days_back` days, saving each
        as "{FON_KODU}_{YIL}_{AY:02d}.pdf" inside `self.output_dir`.

        `start_period` / `end_period` are optional inclusive `(year,
        donem)` bounds -- e.g. `start_period=(2025, 1), end_period=(2025,
        12)` -- for narrowing the result down to a specific date range
        without changing how far back KAP itself is queried. Note that
        `days_back` must still be large enough to cover the requested
        range, since it controls what KAP's API returns in the first
        place.

        Returns a list of per-report result dicts, e.g.:
            [{"year": 2026, "donem": 6, "status": "success", "file": "TLY_2026_06.pdf"}, ...]

        Never raises: every per-report failure is caught, logged to the
        console, and recorded in the returned results so one bad report
        doesn't abort the whole run.
        """
        print(f"[SISTEM] [{self.fon_kodu}] KAP {self.REPORT_TITLE} indirme islemi basliyor...")

        records = self._fetch_disclosure_list(days_back)
        if start_period is not None:
            records = [r for r in records if (r.year, r.donem) >= start_period]
        if end_period is not None:
            records = [r for r in records if (r.year, r.donem) <= end_period]

        if not records:
            print(f"[SISTEM] [{self.fon_kodu}] Indirilecek rapor bulunamadi.")
            return []

        results: List[dict] = []
        for index, record in enumerate(records):
            local_filename = f"{self.fon_kodu}_{record.year}_{record.donem:02d}.pdf"
            print(
                f"\n[{self.fon_kodu}] {record.year}/{record.donem:02d} donemi isleniyor "
                f"(disclosureIndex={record.disclosure_index})..."
            )

            try:
                resolved = self._resolve_attachment(record.disclosure_index)
                if not resolved:
                    results.append(
                        {"year": record.year, "donem": record.donem, "status": "error", "message": "PDF eki bulunamadi."}
                    )
                    continue

                attachment_id, _remote_filename = resolved
                success = self._download_pdf(attachment_id, local_filename)
                results.append(
                    {
                        "year": record.year,
                        "donem": record.donem,
                        "status": "success" if success else "error",
                        "file": local_filename if success else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - a single bad report must never abort the run
                print(
                    f"[KRITIK HATA] [{self.fon_kodu}] {record.year}/{record.donem:02d} islenirken "
                    f"beklenmeyen hata: {exc}"
                )
                results.append({"year": record.year, "donem": record.donem, "status": "error", "message": str(exc)})

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
