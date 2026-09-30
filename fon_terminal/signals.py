"""September 2026 case-study signals, applied to TEFAS daily rows.

Thresholds are the round numbers in CASE_STUDY_2026_09.md. They were written
after the event; this module exists so the dashboard uses the same rules
the write-up used, not a retuned set.

Net liquidity matches `kap_delta_engine._liquidity_ratio_pct` (repo,
reverse repo, money-market, deposits). Keep the keyword list in sync.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

WHALE_SHARE_DROP_PCT = -2.0
WHALE_INVESTOR_FLOOR_PCT = -0.5
BORROW_REPO_PCT = -5.0
DRAIN_WINDOW_DAYS = 20
DRAIN_SHARE_DROP_PCT = -15.0
DRAIN_PRICE_FLOOR_PCT = -3.0

# Same strings as kap_delta_engine.LIQUIDITY_ASSET_KEYWORDS.
LIQUIDITY_ASSET_KEYWORDS = ("repo", "para piyasası", "mevduat")

SIGNAL_KEYS = ("whale", "drain", "borrow", "net_liquidity")


def _tr_lower(text) -> str:
    return str(text).replace("İ", "i").replace("I", "i").lower()


def liquidity_ratio_pct(varliklar: Dict[str, Any]) -> float:
    total = 0.0
    for asset_name, pct_value in varliklar.items():
        if not any(keyword in _tr_lower(asset_name) for keyword in LIQUIDITY_ASSET_KEYWORDS):
            continue
        try:
            total += float(pct_value)
        except (TypeError, ValueError):
            continue
    return total


def pct(new, old) -> float:
    if not old:
        return 0.0
    return (new / old - 1.0) * 100.0


def _repo_pct(varliklar: Optional[Dict[str, Any]]) -> Optional[float]:
    if not varliklar:
        return None
    if "Repo" not in varliklar:
        return None
    try:
        return float(varliklar["Repo"])
    except (TypeError, ValueError):
        return None


def row_signals(row: Dict[str, Any], prev: Optional[Dict[str, Any]], window: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    share_chg = pct(row.get("Pay") or 0.0, (prev or {}).get("Pay") or 0.0) if prev else 0.0
    price_chg = pct(row.get("Fiyat") or 0.0, (prev or {}).get("Fiyat") or 0.0) if prev else 0.0
    if prev and prev.get("Yatirimci") and row.get("Yatirimci") is not None:
        investor_chg = pct(row["Yatirimci"], prev["Yatirimci"])
    else:
        investor_chg = 0.0

    varliklar = row.get("Varliklar") or {}
    has_allocation = bool(varliklar)
    repo = _repo_pct(varliklar)
    net_liq = liquidity_ratio_pct(varliklar) if has_allocation else None

    drain_share = pct(row.get("Pay") or 0.0, window.get("Pay") or 0.0) if window else None
    drain_price = pct(row.get("Fiyat") or 0.0, window.get("Fiyat") or 0.0) if window else None
    whale = bool(prev) and share_chg <= WHALE_SHARE_DROP_PCT and investor_chg >= WHALE_INVESTOR_FLOOR_PCT
    drain = (
        window is not None
        and drain_share <= DRAIN_SHARE_DROP_PCT
        and drain_price > DRAIN_PRICE_FLOOR_PCT
    )
    borrow = repo is not None and repo <= BORROW_REPO_PCT
    net_liquidity = net_liq is not None and net_liq < 0

    outflow = whale or drain
    buffer = borrow or net_liquidity
    return {
        "whale": whale,
        "drain": drain,
        "borrow": borrow,
        "net_liquidity": net_liquidity,
        "high_severity": outflow and buffer,
        "share_chg": share_chg,
        "investor_chg": investor_chg,
        "drain_share_chg": drain_share,
        "drain_price_chg": drain_price,
        "repo": repo,
        "net_liquidity_pct": net_liq,
    }


def annotate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Adds a `signals` dict on each TEFAS row. Does not write to disk."""
    for index, row in enumerate(records):
        prev = records[index - 1] if index else None
        window = records[index - DRAIN_WINDOW_DAYS] if index >= DRAIN_WINDOW_DAYS else None
        row["signals"] = row_signals(row, prev, window)
    return records


def is_suspended(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    try:
        return float(row.get("Fiyat") or 0.0) <= 0.0
    except (TypeError, ValueError):
        return False
