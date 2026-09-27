"""Figures and numbers for the September 2026 fund-run case study.

Reads the TEFAS daily records the terminal collected (`fund_database.json`),
applies a small set of pre-declared liquidity/redemption signals to every
tracked fund, and renders the charts used by `CASE_STUDY_2026_09.md`.

Optional inputs, used only when present:

- `private/data/TLY_*.pdf` -- TLY's KAP portfolio reports, for the
  single-stock concentration chart and the 16.09 NAV estimate.
- `private/trades.json` -- personal orders; produces the private
  trade-annotated chart and exit scenarios under `private/`.

Row-date convention: a TEFAS record dated T is published on T and is
valued at the previous business day's close (measured against KAP PDFs,
see `kap_pdf_downloader/report_dating.py`). Every date below is the day a
number became VISIBLE, which is the only fair date for "could this have
been acted on".
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TERMINAL_DIR = os.path.dirname(HERE)
DATABASE_PATH = os.path.join(TERMINAL_DIR, "fund_database.json")
KAP_DIR = os.path.join(TERMINAL_DIR, "kap_pdf_downloader")
PUBLIC_IMG_DIR = os.path.join(HERE, "img")
PRIVATE_DIR = os.path.join(HERE, "private")
PRIVATE_IMG_DIR = os.path.join(PRIVATE_DIR, "img")
PRIVATE_PDF_DIR = os.path.join(PRIVATE_DIR, "data")
TRADES_PATH = os.path.join(PRIVATE_DIR, "trades.json")

sys.path.insert(0, KAP_DIR)
from kap_delta_engine import _liquidity_ratio_pct  # noqa: E402  same definition the pipeline uses

CHART_START = date(2026, 7, 15)
INDEX_BASE_DATE = date(2026, 7, 16)

# Signals. Thresholds are round numbers declared before looking at how
# early each one fires; they were not tuned to maximise lead time.
WHALE_SHARE_DROP_PCT = -2.0       # one-day share-count change at or below this...
WHALE_INVESTOR_FLOOR_PCT = -0.5   # ...while the investor count is roughly flat or rising
BORROW_REPO_PCT = -5.0            # repo borrowing at or beyond 5% of NAV
DRAIN_WINDOW_DAYS = 20            # business days
DRAIN_SHARE_DROP_PCT = -15.0      # shares down 15%+ over the window...
DRAIN_PRICE_FLOOR_PCT = -3.0      # ...while price is down less than 3%
COLLAPSE_DAILY_DROP_PCT = -5.0

SIGNALS = {
    "whale": "Whale day (shares -2%+, investors flat/up)",
    "borrow": "Repo borrowing <= -5% of NAV",
    "drain": "Silent drain (shares -15%/20d, price > -3%)",
    "net_liquidity": "Net liquidity < 0",
}
SIGNAL_COLORS = {"whale": "#8e44ad", "borrow": "#d35400", "drain": "#2c7fb8", "net_liquidity": "#c0392b"}

FEATURED_FUNDS = ["PHE", "PBR", "TLY", "DFI"]


def parse_tr_date(raw):
    return datetime.strptime(raw, "%d.%m.%Y").date()


def load_series(database, fund):
    """Daily rows up to (not including) the first zero-price row, plus the
    date trading stopped (None if it never did)."""
    rows, suspended_on = [], None
    for record in database[fund]["records"]:
        day = parse_tr_date(record["Tarih"])
        price = record.get("Fiyat") or 0.0
        if price <= 0:
            suspended_on = day
            break
        varliklar = record.get("Varliklar") or {}
        rows.append({
            "date": day,
            "price": price,
            "shares": record.get("Pay") or 0.0,
            "investors": record.get("Yatirimci"),
            "aum": record.get("ToplamDeger") or 0.0,
            "has_allocation": bool(varliklar),
            "equity": varliklar.get("Hisse Senedi", 0.0) if varliklar else None,
            "repo": varliklar.get("Repo", 0.0) if varliklar else None,
            "net_liquidity": _liquidity_ratio_pct(varliklar) if varliklar else None,
        })
    return rows, suspended_on


def pct(new, old):
    return (new / old - 1.0) * 100.0 if old else 0.0


def annotate_signals(rows):
    for i, row in enumerate(rows):
        prev = rows[i - 1] if i else None
        row["share_chg"] = pct(row["shares"], prev["shares"]) if prev else 0.0
        row["price_chg"] = pct(row["price"], prev["price"]) if prev else 0.0
        if prev and prev["investors"] and row["investors"] is not None:
            row["investor_chg"] = pct(row["investors"], prev["investors"])
        else:
            row["investor_chg"] = 0.0

        window = rows[i - DRAIN_WINDOW_DAYS] if i >= DRAIN_WINDOW_DAYS else None
        row["signals"] = {
            "whale": bool(prev) and row["share_chg"] <= WHALE_SHARE_DROP_PCT
            and row["investor_chg"] >= WHALE_INVESTOR_FLOOR_PCT,
            "borrow": row["repo"] is not None and row["repo"] <= BORROW_REPO_PCT,
            "drain": window is not None
            and pct(row["shares"], window["shares"]) <= DRAIN_SHARE_DROP_PCT
            and pct(row["price"], window["price"]) > DRAIN_PRICE_FLOOR_PCT,
            "net_liquidity": row["net_liquidity"] is not None and row["net_liquidity"] < 0,
        }
    return rows


def collapse_date(rows, suspended_on):
    for row in rows:
        if row["price_chg"] <= COLLAPSE_DAILY_DROP_PCT:
            return row["date"]
    return suspended_on


def business_days_between(start, end):
    days, cursor = 0, start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def summarize(rows, suspended_on):
    end = collapse_date(rows, suspended_on)
    summary = {"collapse": end, "suspended": suspended_on, "signals": {}}
    for key in SIGNALS:
        hits = [r["date"] for r in rows if r["signals"][key] and (end is None or r["date"] < end)]
        summary["signals"][key] = {
            "first": hits[0] if hits else None,
            "days": len(hits),
            "lead_bd": business_days_between(hits[0], end) if hits and end else None,
        }
    return summary


def index_series(rows, field):
    base = next((r[field] for r in rows if r["date"] >= INDEX_BASE_DATE), None)
    return [r[field] / base * 100.0 if base else None for r in rows]


def style_axis(ax):
    ax.grid(True, color="#e5e5e5", linewidth=0.7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))


def fund_panel(fund, rows, summary, output_path, trades=None):
    rows = [r for r in rows if r["date"] >= CHART_START]
    dates = [r["date"] for r in rows]
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True,
                             gridspec_kw={"height_ratios": [1.3, 1, 1]})

    ax = axes[0]
    ax.plot(dates, index_series(rows, "price"), color="#222222", linewidth=2, label="Unit price")
    ax.plot(dates, index_series(rows, "shares"), color="#2c7fb8", linewidth=2, label="Shares outstanding")
    ax.set_ylabel(f"Index ({INDEX_BASE_DATE:%d.%m} = 100)")
    ax.set_title(f"{fund}: price vs. shares outstanding, balance-sheet liquidity, daily flows", loc="left")

    if trades:
        price_index = dict(zip(dates, index_series(rows, "price")))
        for trade in trades:
            day = date.fromisoformat(trade["date"])
            level = price_index.get(day) or price_index.get(max((d for d in price_index if d <= day), default=None))
            if level is None:
                continue
            marker, color = ("^", "#1a9850") if trade["side"] == "buy" else ("v", "#d73027")
            ax.scatter([day], [level], marker=marker, s=120, color=color, zorder=5, edgecolor="black")
            ax.annotate(f"{'BUY' if trade['side'] == 'buy' else 'SELL'} {day:%d.%m}", (day, level),
                        textcoords="offset points", xytext=(0, 12 if trade["side"] == "buy" else -18),
                        ha="center", fontsize=8)

    ax = axes[1]
    # Once NAV nears zero, allocation percentages explode (PHE showed +-500%);
    # those rows carry no information and are drawn as gaps.
    def sane(value):
        return value if value is not None and abs(value) <= 100 else float("nan")

    liquidity = [sane(r["net_liquidity"]) for r in rows]
    repo = [sane(r["repo"]) for r in rows]
    ax.plot(dates, liquidity, color="#1a9850", linewidth=2, label="Net liquidity (% NAV)")
    ax.plot(dates, repo, color="#d35400", linewidth=1.5, linestyle="--", label="Repo borrowing (% NAV)")
    ax.axhline(0, color="#555555", linewidth=0.8)
    ax.set_ylim(-60, 35)
    ax.set_ylabel("% of NAV (clipped)")

    ax = axes[2]
    width = 0.4
    ax.bar([d - timedelta(hours=5) for d in dates], [max(r["share_chg"], -20) for r in rows], width=width,
           color="#2c7fb8", label="Shares, daily %")
    ax.bar([d + timedelta(hours=5) for d in dates], [max(r["investor_chg"], -20) for r in rows], width=width,
           color="#bbbbbb", label="Investors, daily %")
    ax.axhline(0, color="#555555", linewidth=0.8)
    ax.set_ylim(-20, 8)
    ax.set_ylabel("Daily change % (clipped)")

    for ax in axes:
        style_axis(ax)
        for key, info in summary["signals"].items():
            if info["first"] and info["first"] >= CHART_START:
                ax.axvline(info["first"], color=SIGNAL_COLORS[key], linewidth=1.2, linestyle=":")
        if summary["collapse"]:
            ax.axvline(summary["collapse"], color="black", linewidth=1.8)

    handles, labels = [], []
    for ax in axes:
        h, lbl = ax.get_legend_handles_labels()
        handles += h
        labels += lbl
    for key, info in summary["signals"].items():
        if info["first"] and info["first"] >= CHART_START:
            handles.append(plt.Line2D([], [], color=SIGNAL_COLORS[key], linestyle=":", linewidth=1.5))
            labels.append(f"First: {SIGNALS[key]} ({info['first']:%d.%m})")
    if summary["collapse"]:
        handles.append(plt.Line2D([], [], color="black", linewidth=1.8))
        labels.append(f"Collapse / suspension ({summary['collapse']:%d.%m})")
    fig.legend(handles, labels, loc="lower center", ncol=2, fontsize=8, frameon=False)
    fig.tight_layout(rect=(0, 0.12, 1, 1))
    fig.savefig(output_path, dpi=130)
    plt.close(fig)


def tly_concentration(pdf_dir):
    """Top holdings by NAV weight for each TLY report on disk, using the
    report's own per-stock valuation prices; plus a 15.09 estimate from
    the 04.09 lots at 15.09 BIST closes when yfinance is available."""
    from kap_pdf_parser import KAPPdfParser

    parser = KAPPdfParser(verbose=False)
    snapshots = {}
    for slug, label in (("2026_AB07", "31.07 (monthly)"), ("2026_HB34", "31.08 (weekly)"),
                        ("2026_HB35", "04.09 (weekly)")):
        path = os.path.join(pdf_dir, f"TLY_{slug}.pdf")
        if not os.path.exists(path):
            continue
        lots = parser.parse_file(path)
        fingerprint = parser.extract_fingerprint(path)
        weights = {t: lots[t] * p / fingerprint.total_value * 100
                   for t, p in fingerprint.valuation_prices.items() if t in lots}
        snapshots[label] = {"weights": weights, "lots": lots}

    estimate = None
    latest = snapshots.get("04.09 (weekly)")
    if latest:
        estimate = estimate_tly_0916(latest["lots"])
        if estimate:
            snapshots["15.09 (04.09 lots, est.)"] = {"weights": estimate["weights_15"], "lots": latest["lots"]}
    return snapshots, estimate


def estimate_tly_0916(lots):
    try:
        import yfinance as yf
    except ImportError:
        return None
    tickers = sorted(t for t, lot in lots.items() if lot > 0)
    data = yf.download([f"{t}.IS" for t in tickers], start="2026-09-14", end="2026-09-18",
                       group_by="ticker", auto_adjust=False, progress=False)
    nav_row_1609 = 243_624_000_000.0  # TEFAS row 16.09 = NAV valued at the 15.09 close
    weights_15, moves, pnl = {}, {}, 0.0
    for ticker in tickers:
        try:
            closes = data[f"{ticker}.IS"]["Close"].dropna()
            c15, c16 = float(closes.loc["2026-09-15"]), float(closes.loc["2026-09-16"])
        except Exception:
            continue
        weights_15[ticker] = lots[ticker] * c15 / nav_row_1609 * 100
        moves[ticker] = pct(c16, c15)
        pnl += lots[ticker] * (c16 - c15)
    covered = sum(weights_15.values())
    return {
        "weights_15": weights_15,
        "moves_16": moves,
        "equity_covered_pct": covered,
        "nav_change_pct_upper": pnl / nav_row_1609 * 100,
    }


def concentration_chart(snapshots, output_path):
    labels = list(snapshots)
    order = sorted(snapshots[labels[-1]]["weights"], key=lambda t: -snapshots[labels[-1]]["weights"][t])[:6]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    width = 0.8 / len(labels)
    palette = ["#c6dbef", "#6baed6", "#2171b5", "#08306b"]
    for i, label in enumerate(labels):
        values = [snapshots[label]["weights"].get(t, 0.0) for t in order]
        positions = [x + i * width for x in range(len(order))]
        ax.bar(positions, values, width=width, label=label, color=palette[i % len(palette)])
    ax.set_xticks([x + width * (len(labels) - 1) / 2 for x in range(len(order))])
    ax.set_xticklabels(order)
    ax.set_ylabel("% of fund NAV")
    ax.set_title("TLY: largest single-stock positions (KAP portfolio reports)", loc="left")
    style = ax.spines
    style["top"].set_visible(False)
    style["right"].set_visible(False)
    ax.grid(True, axis="y", color="#e5e5e5")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=130)
    plt.close(fig)


def top_n_share(weights, n):
    return sum(sorted(weights.values(), reverse=True)[:n])


def price_on_or_after(rows_by_date, day):
    for d in sorted(rows_by_date):
        if d >= day:
            return d, rows_by_date[d]
    return None, None


def next_business_day(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def exit_scenarios(database, holdings, order_dates, tly_estimate_price):
    """Value of the final holdings had the same exit order been placed
    (before the 13:30 cut-off) on each date. Fill = the next business
    day's TEFAS row, the rule every broker fill in the statements obeys."""
    table = []
    for order_day in order_dates:
        fill_day = next_business_day(order_day)
        total, parts = 0.0, {}
        for fund, units in holdings.items():
            prices = {parse_tr_date(r["Tarih"]): r["Fiyat"] for r in database[fund]["records"]}
            price = prices.get(fill_day)
            if not price and fund == "TLY" and tly_estimate_price:
                price = tly_estimate_price
            if not price:
                continue
            parts[fund] = units * price
            total += units * price
        table.append({"order": order_day, "fill_row": fill_day, "total": total, "parts": parts})
    return table


def main():
    with open(DATABASE_PATH, encoding="utf-8") as handle:
        database = json.load(handle)

    os.makedirs(PUBLIC_IMG_DIR, exist_ok=True)
    report = {"funds": {}}

    series = {}
    for fund in database:
        rows, suspended_on = load_series(database, fund)
        rows = annotate_signals(rows)
        series[fund] = (rows, suspended_on)
        report["funds"][fund] = summarize(rows, suspended_on)

    for fund in FEATURED_FUNDS:
        rows, _ = series[fund]
        fund_panel(fund, rows, report["funds"][fund], os.path.join(PUBLIC_IMG_DIR, f"{fund.lower()}_panel.png"))

    print("=== Signal first-trigger dates (row date = day visible) and business-day lead ===")
    for fund, summary in report["funds"].items():
        parts = []
        for key, info in summary["signals"].items():
            if info["first"]:
                parts.append(f"{key}={info['first']:%d.%m} ({info['days']}d, lead {info['lead_bd']})")
            else:
                parts.append(f"{key}=-")
        collapse = summary["collapse"].strftime("%d.%m") if summary["collapse"] else "none"
        suspended = summary["suspended"].strftime("%d.%m") if summary["suspended"] else "-"
        first_row = database[fund]["records"][0]["Tarih"]
        print(f"{fund:4} from {first_row} collapse={collapse} suspended={suspended} | " + "; ".join(parts))

    print("\n=== Fund stats, 16.07 (or first row) to the last row before collapse ===")
    for fund, (rows, _) in series.items():
        collapse = report["funds"][fund]["collapse"]
        calm = [r for r in rows if collapse is None or r["date"] < collapse]
        start = next((r for r in calm if r["date"] >= date(2026, 7, 16)), calm[0])
        end = calm[-1]
        print(f"{fund:4} {start['date']:%d.%m}->{end['date']:%d.%m} price {pct(end['price'], start['price']):+.1f}% "
              f"shares {pct(end['shares'], start['shares']):+.1f}% "
              f"investors {pct(end['investors'], start['investors']) if start['investors'] else 0:+.1f}% "
              f"net_liq {start['net_liquidity']}->{end['net_liquidity']}")

    tly_estimate_price = None
    if os.path.isdir(PRIVATE_PDF_DIR):
        snapshots, estimate = tly_concentration(PRIVATE_PDF_DIR)
        if snapshots:
            concentration_chart(snapshots, os.path.join(PUBLIC_IMG_DIR, "tly_concentration.png"))
            print("\n=== TLY concentration ===")
            for label, snap in snapshots.items():
                w = snap["weights"]
                top = sorted(w.items(), key=lambda kv: -kv[1])[:5]
                print(f"{label:26} top1 {top_n_share(w, 1):.1f}% top3 {top_n_share(w, 3):.1f}% "
                      f"top5 {top_n_share(w, 5):.1f}% | " + ", ".join(f"{t} {v:.1f}" for t, v in top))
        if estimate:
            tly_row_1609 = 10308.9671
            upper = estimate["nav_change_pct_upper"]
            tly_equity_1609 = 88.39
            scaled = upper * tly_equity_1609 / estimate["equity_covered_pct"]
            tly_estimate_price = tly_row_1609 * (1 + (upper + scaled) / 200)
            print(f"\n=== TLY 16.09 estimate ===\nequities priced: {estimate['equity_covered_pct']:.1f}% of NAV; "
                  f"moves: {', '.join(f'{t} {m:+.1f}' for t, m in estimate['moves_16'].items())}")
            print(f"NAV change: {scaled:.2f}% (scaled to TEFAS equity {tly_equity_1609}%) .. {upper:.2f}% (04.09 lots) "
                  f"-> price {tly_row_1609 * (1 + scaled / 100):.2f} .. {tly_row_1609 * (1 + upper / 100):.2f}")

    if os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, encoding="utf-8") as handle:
            personal = json.load(handle)
        os.makedirs(PRIVATE_IMG_DIR, exist_ok=True)
        for fund in ("PHE", "PBR", "TLY", "DFI", "THF", "DOH"):
            rows, _ = series[fund]
            trades = [t for t in personal["trades"] if t["fund"] == fund]
            fund_panel(fund, rows, report["funds"][fund],
                       os.path.join(PRIVATE_IMG_DIR, f"{fund.lower()}_trades.png"), trades=trades)

        order_dates = [date(2026, 9, 2), date(2026, 9, 8), date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 16)]
        print("\n=== Exit scenarios for final holdings (TLY on 16.09 uses the estimate midpoint) ===")
        for row in exit_scenarios(database, personal["holdings_at_exit"], order_dates, tly_estimate_price):
            parts = ", ".join(f"{f} {v:,.0f}" for f, v in row["parts"].items())
            print(f"order {row['order']:%d.%m} -> row {row['fill_row']:%d.%m}: total {row['total']:,.0f} TL | {parts}")


if __name__ == "__main__":
    main()
