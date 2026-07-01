"""
回测 — 20日新高 + 严进宽出轮动策略
池子20只：20日新高筛选前20名。买top10，持mid10，跌出池子就卖。
"""

import os
import sys
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scanner import fetch_sp500_constituents
from src.scanner import qualify_20day_highs

_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_FONT_PATH):
    fm.fontManager.addfont(_FONT_PATH)
    plt.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "backtest")


def _fmt_pct(v):
    return f"{v * 100:.2f}%"


def _fmt_usd(v):
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def _load_data(symbols, years=5):
    end = datetime.today()
    start = end - timedelta(days=years * 370)
    print(f"  Downloading {len(symbols)} tickers ({years} years)...")
    data = yf.download(
        list(set(symbols + ["SPY"])),
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    close = data["Close"].dropna(axis=1, how="all")
    if isinstance(close.columns, pd.MultiIndex):
        close = close.droplevel(0, axis=1)
    print(f"  Range: {close.index[0].date()} → {close.index[-1].date()}  ({len(close)} days)")
    return close


def detect_bull_bear_windows(close, window=120):
    spy = close["SPY"] if "SPY" in close.columns else None
    if spy is None:
        return None, None

    ret = spy.pct_change(window).dropna()
    if ret.empty:
        return None, None

    best_date = ret.idxmax()
    worst_date = ret.idxmin()
    idx_map = {d: i for i, d in enumerate(close.index)}

    def get_start(end_date):
        pos = idx_map.get(end_date)
        if pos is None or pos < window:
            return None
        return close.index[pos - window]

    bull = None
    bear = None
    bs = get_start(best_date)
    if bs is not None:
        bull = (bs, best_date)
    ws = get_start(worst_date)
    if ws is not None:
        bear = (ws, worst_date)
    return bull, bear


def qualify_at_date(close, symbols, idx):
    if idx < 20:
        return []
    window = close.iloc[idx - 19: idx + 1]
    cutoff = window.index[-5]
    past = close.iloc[idx - 20]
    results = []
    for sym in symbols:
        if sym not in close.columns:
            continue
        series = window[sym].dropna()
        if len(series) < 20:
            continue
        max_date = series.idxmax()
        if max_date >= cutoff:
            cur = close.iloc[idx][sym]
            p = past[sym]
            if pd.notna(cur) and pd.notna(p) and p > 0:
                results.append((sym, cur / p - 1))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def run_backtest(close, symbols, name_map, start_date=None, end_date=None,
                 top_n=20, buy_top=10, initial_cash_per_stock=2000):
    if start_date is None:
        start_date = close.index[0]
    if end_date is None:
        end_date = close.index[-1]

    dates = close.resample("W-FRI").last().index
    dates = [d for d in dates if start_date <= d <= end_date and d in close.index]
    if len(dates) < 2:
        return None

    date_to_idx = {d: i for i, d in enumerate(close.index)}
    friday_to_next = {dates[i]: dates[i + 1] for i in range(len(dates) - 1)}

    initial_capital = buy_top * initial_cash_per_stock
    cash = float(initial_capital)
    positions = {}
    records = []
    trade_log = []
    buy_dates: dict[str, datetime] = {}
    hold_periods: list[int] = []
    prev_friday = None
    prev_port_value = None

    for friday in dates:
        if friday not in friday_to_next:
            break
        idx = date_to_idx[friday]

        top20 = qualify_at_date(close, symbols, idx)
        if len(top20) < buy_top:
            continue
        top20_set = {s for s, _ in top20}
        top10 = [s for s, _ in top20[:buy_top]]

        # --- sell: positions not in top20 pool ---
        for sym in list(positions.keys()):
            if sym not in top20_set:
                price = close.iloc[idx][sym]
                if pd.notna(price) and price > 0:
                    proceeds = positions[sym] * price
                    cash += proceeds
                    trade_log.append({"date": friday, "action": "SELL", "symbol": sym,
                                      "price": price, "qty": positions[sym],
                                      "value": proceeds})
                    if sym in buy_dates:
                        weeks = int((friday - buy_dates[sym]).days / 7)
                        hold_periods.append(weeks)
                        del buy_dates[sym]
                    del positions[sym]

        # --- buy: top10 not yet held, if cash allows ---
        for sym in top10:
            if sym not in positions and cash >= initial_cash_per_stock:
                price = close.iloc[idx][sym]
                if pd.notna(price) and price > 0:
                    shares = initial_cash_per_stock / price
                    positions[sym] = shares
                    buy_dates[sym] = friday
                    cash -= initial_cash_per_stock
                    trade_log.append({"date": friday, "action": "BUY", "symbol": sym,
                                      "price": price, "qty": shares,
                                      "value": initial_cash_per_stock})

        # --- record (skip first week — no forward return yet) ---
        if prev_friday is None:
            prev_friday = friday
            continue

        h_val = sum(positions[s] * close.iloc[idx][s]
                    for s in positions if pd.notna(close.iloc[idx][s]))
        port_value = cash + h_val

        if prev_port_value is not None:
            weekly_r = port_value / prev_port_value - 1
        else:
            weekly_r = 0.0
        prev_port_value = port_value

        records.append({
            "date": friday,
            "prev_date": prev_friday,
            "holdings_value": h_val,
            "cash": cash,
            "port_value": port_value,
            "return": weekly_r,
            "n_qualifiers": len(top20),
            "n_positions": len(positions),
            "n_top10_held": sum(1 for s in top10 if s in positions),
        })
        prev_friday = friday

    if not records:
        return None

    pf = pd.DataFrame(records).set_index("date")
    pf["cum_return"] = (1 + pf["return"]).cumprod()
    pf["peak"] = pf["cum_return"].cummax()
    pf["drawdown"] = pf["cum_return"] / pf["peak"] - 1
    pf["capital_util"] = pf["holdings_value"] / (pf["holdings_value"] + pf["cash"])

    # SPY benchmark
    spy_prices = close["SPY"]
    spy_vals = pd.DataFrame({
        "entry": spy_prices.loc[pf["prev_date"]].values,
        "exit": spy_prices.loc[pf.index].values,
    }, index=pf.index)
    pf["spy_return"] = spy_vals["exit"] / spy_vals["entry"] - 1
    pf["spy_cum"] = (1 + pf["spy_return"]).cumprod()

    # Include currently held stocks in hold period stats
    for sym, buy_date in buy_dates.items():
        weeks = int((records[-1]["date"] - buy_date).days / 7) if records else 0
        hold_periods.append(weeks)

    avg_hold = np.mean(hold_periods) if hold_periods else 0
    med_hold = float(np.median(hold_periods)) if hold_periods else 0

    return {
        "pf": pf,
        "trade_log": trade_log,
        "initial_capital": initial_capital,
        "total_buys": sum(1 for t in trade_log if t["action"] == "BUY"),
        "total_sells": sum(1 for t in trade_log if t["action"] == "SELL"),
        "avg_hold_weeks": round(avg_hold, 1),
        "med_hold_weeks": round(med_hold, 1),
    }


def compute_metrics(result):
    pf = result["pf"]
    total_ret = pf["cum_return"].iloc[-1] - 1
    years = (pf.index[-1] - pf.index[0]).days / 365.25
    cagr = (1 + total_ret) ** (1 / years) - 1
    dd = pf["drawdown"]
    max_dd = dd.min()
    weekly_r = pf["return"]
    win_rate = (weekly_r > 0).mean()

    if "spy_cum" in pf.columns:
        spy_total = pf["spy_cum"].iloc[-1] - 1
        spy_cagr = (1 + spy_total) ** (1 / years) - 1
    else:
        spy_total = spy_cagr = 0

    return {
        "period": f"{pf.index[0].date()} → {pf.index[-1].date()}",
        "weeks": len(pf),
        "initial_capital": result["initial_capital"],
        "port_value": pf["port_value"].iloc[-1],
        "total_ret": total_ret,
        "cagr": cagr,
        "max_dd": max_dd,
        "win_rate": win_rate,
        "spy_total": spy_total,
        "spy_cagr": spy_cagr,
        "total_buys": result["total_buys"],
        "total_sells": result["total_sells"],
        "avg_hold_weeks": result["avg_hold_weeks"],
        "med_hold_weeks": result["med_hold_weeks"],
    }


def print_results(perf, title="回测结果"):
    print(f"\n  {title}")
    print(f"  {'=' * 50}")
    print(f"  运行区间    {perf['period']} ({perf['weeks']} 周)")
    print(f"  初始资金    {_fmt_usd(perf['initial_capital'])}")
    print(f"  当前价值    {_fmt_usd(perf['port_value'])}")
    print(f"  收益率      {_fmt_pct(perf['total_ret'])}")
    print(f"  年化        {_fmt_pct(perf['cagr'])}")
    print(f"  最大回撤    {_fmt_pct(perf['max_dd'])}")
    print(f"  胜率        {_fmt_pct(perf['win_rate'])}")
    print(f"  SPY 收益    {_fmt_pct(perf['spy_total'])} ({_fmt_pct(perf['spy_cagr'])} 年化)")
    print(f"  交易次数    {perf['total_buys']} 买 / {perf['total_sells']} 卖")
    print(f"  平均持股    {perf['avg_hold_weeks']} 周 (中位数 {perf['med_hold_weeks']} 周)")
    print(f"  {'=' * 50}")


def plot_equity_curve(pf, label, output_path):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(pf.index, pf["cum_return"], label=f"策略 ({label})", linewidth=2, color="#2196F3")
    if "spy_cum" in pf.columns:
        ax1.plot(pf.index, pf["spy_cum"], label="SPY 买入持有", linewidth=2, color="#FF5722", alpha=0.7)
    ax1.axhline(1.0, color="gray", linestyle="--", linewidth=0.5)
    ax1.set_ylabel("累计收益")
    ax1.set_title(f"20日新高轮动回测 — {label}")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    ax2.fill_between(pf.index, 0, pf["drawdown"] * 100, color="#f44336", alpha=0.3)
    ax2.plot(pf.index, pf["drawdown"] * 100, color="#f44336", linewidth=1)
    ax2.set_ylabel("回撤 (%)")
    ax2.set_xlabel("日期")
    ax2.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ 权益曲线 -> {output_path}")


def run_all_periods(top_n=20, buy_top=10, years=5, initial_cash_per_stock=2000):
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    print("=" * 60)
    print("  📊 20日新高轮动回测 — 严进宽出")
    print("  " + "=" * 60)
    print(f"  池子 {top_n} 只, 买 top {buy_top}")
    print(f"  初始 ${buy_top * initial_cash_per_stock:,}, 每只 ${initial_cash_per_stock:,}")
    print()

    constituents = fetch_sp500_constituents()
    symbols = constituents["Symbol"].tolist()
    name_map = dict(zip(constituents["Symbol"], constituents["Security"]))
    close = _load_data(symbols, years)
    avail = [s for s in symbols if s in close.columns]

    bull_window, bear_window = detect_bull_bear_windows(close)

    periods = [
        ("full", "全周期", None, None),
    ]
    if bull_window:
        periods.append(("bull", f"牛市 {bull_window[0].date()}→{bull_window[1].date()}", bull_window[0], bull_window[1]))
    if bear_window:
        periods.append(("bear", f"熊市 {bear_window[0].date()}→{bear_window[1].date()}", bear_window[0], bear_window[1]))
    periods.append(("sideways", "猴市 2025-10-01→2026-04-29",
                    datetime(2025, 10, 1), datetime(2026, 4, 30)))

    results = []
    multi_pf = {}

    for key, label, sd, ed in periods:
        print(f"\n  ▶ {label}")
        result = run_backtest(close, avail, name_map, start_date=sd, end_date=ed,
                              top_n=top_n, buy_top=buy_top,
                              initial_cash_per_stock=initial_cash_per_stock)
        if result is None:
            print("  ⚠️ 数据不足")
            continue
        perf = compute_metrics(result)
        print_results(perf, label)
        results.append((key, label, perf, result))
        multi_pf[key] = result["pf"]

    # Combined equity curve
    if multi_pf:
        fig, ax = plt.subplots(figsize=(14, 6))
        colors = {"full": "#2196F3", "bull": "#4CAF50", "bear": "#f44336", "sideways": "#FF9800"}
        labels_dict = {k: lbl for k, lbl, _, _ in periods}
        for key, pf in multi_pf.items():
            label = labels_dict.get(key, key)
            ax.plot(pf.index, pf["cum_return"], label=label, linewidth=2, color=colors.get(key, "#999"))
        ax.axhline(1.0, color="gray", linestyle="--", linewidth=0.5)
        ax.set_ylabel("累计收益")
        ax.set_title("20日新高轮动 — 多区间对比")
        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)
        chart_path = os.path.join(_OUTPUT_DIR, "equity_curve.png")
        fig.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"\n  ✓ 合并权益曲线 -> {chart_path}")

        # Send to Telegram
        try:
            from src.notifier import send_photo
            summary_lines = []
            for key, label, perf, _ in results:
                summary_lines.append(
                    f"{'🟢' if perf['total_ret'] > 0 else '🔴'} <b>{label}</b>: "
                    f"{_fmt_pct(perf['total_ret'])}  |  SPY {_fmt_pct(perf['spy_total'])}"
                )
            caption = "📊 <b>20日新高轮动回测</b>\n" + "\n".join(summary_lines)
            send_photo(chart_path, caption=caption)
        except Exception as e:
            print(f"  ⚠️ TG send failed: {e}")

    print(f"\n{'=' * 60}\n")
    return results


if __name__ == "__main__":
    run_all_periods()
