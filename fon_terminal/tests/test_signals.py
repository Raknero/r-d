"""Case-study signal rules against compact TEFAS windows."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "kap_pdf_downloader"))

from signals import annotate_records, liquidity_ratio_pct  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "signal_windows.json"


def load_windows():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


class LiquidityMatchesKap(unittest.TestCase):
    def test_same_as_kap_delta_engine(self):
        from kap_delta_engine import _liquidity_ratio_pct

        sample = {
            "Repo": -7.61,
            "Ters-Repo": 1.27,
            "Mevduat (TL)": 0.55,
            "Hisse Senedi": 87.43,
        }
        self.assertAlmostEqual(liquidity_ratio_pct(sample), _liquidity_ratio_pct(sample))


class TlyNetLiquidity(unittest.TestCase):
    def test_negative_from_8_september(self):
        rows = annotate_records(load_windows()["TLY"])
        by_date = {row["Tarih"]: row for row in rows}
        self.assertFalse(by_date["07.09.2026"]["signals"]["net_liquidity"])
        self.assertTrue(by_date["08.09.2026"]["signals"]["net_liquidity"])


class PheSilentDrain(unittest.TestCase):
    def test_fires_3_august(self):
        rows = annotate_records(load_windows()["PHE"])
        by_date = {row["Tarih"]: row for row in rows}
        self.assertTrue(by_date["03.08.2026"]["signals"]["drain"])
        earlier = [row for row in rows if row["Tarih"] != "03.08.2026"]
        self.assertTrue(any(not row["signals"]["drain"] for row in earlier))


class PryNoLiquidityAlarm(unittest.TestCase):
    def test_drain_without_buffer_break(self):
        rows = annotate_records(load_windows()["PRY"])
        last = rows[-1]
        self.assertEqual(last["Tarih"], "14.09.2026")
        self.assertTrue(last["signals"]["drain"])
        self.assertFalse(last["signals"]["net_liquidity"])
        self.assertFalse(last["signals"]["borrow"])
        self.assertFalse(last["signals"]["high_severity"])


if __name__ == "__main__":
    unittest.main()
