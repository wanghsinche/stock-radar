"""
回测 — 20日新高 + 120日动量排序 + 严进宽出轮动策略
池子20只：20日新高过滤后按120日涨幅筛选前20名。买top10，持mid10，跌出池子就卖。
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
from src.trading import (
    calc_buy_count,
    calc_sell_list,
    is_spy_entry_trigger,
    is_spy_exit_trigger,
    increment_cooldown,
    calc_drawdown,
    calc_portfolio_value,
    calc_shares_to_buy,
)

_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_FONT_PATH):
    fm.fontManager.addfont(_FONT_PATH)
    plt.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "backtest")
_RANK_WINDOW = 120
_WARMUP_DAYS = 260


def _fmt_pct(v):
    return f"{v * 100:.2f}%"


def _fmt_usd(v):
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def _load_data(symbols, years=5, warmup_days=_WARMUP_DAYS):
    end = datetime.today()
    start = end - timedelta(days=years * 370 + warmup_days)
    print(f"  Downloading {len(symbols)} tickers ({years} years)...")
    data = yf.download(
        list(set(symbols + ["SPY"])),
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    close = data["Close"].dropna(axis=1, how="all")
    open_prices = data["Open"].dropna(axis=1, how="all")
    if isinstance(close.columns, pd.MultiIndex):
        close = close.droplevel(0, axis=1)
        open_prices = open_prices.droplevel(0, axis=1)
    common = close.columns.intersection(open_prices.columns)
    close = close[common]
    open_prices = open_prices[common]
    print(f"  Range: {close.index[0].date()} → {close.index[-1].date()}  ({len(close)} days)")
    return close, open_prices


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


def qualify_at_date(close, symbols, idx, high_window=20, rank_window=120, recent_days=5):
    if idx < max(high_window, rank_window):
        return []
    window = close.iloc[idx - high_window + 1: idx + 1]
    cutoff = window.index[-recent_days]
    past = close.iloc[idx - rank_window]
    results = []
    for sym in symbols:
        if sym not in close.columns:
            continue
        series = window[sym].dropna()
        if len(series) < high_window:
            continue
        max_date = series.idxmax()
        if max_date >= cutoff:
            cur = close.iloc[idx][sym]
            p = past[sym]
            if pd.notna(cur) and pd.notna(p) and p > 0:
                results.append((sym, cur / p - 1))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def run_backtest(close, open_prices, symbols, name_map, start_date=None, end_date=None,
                 top_n=20, buy_top=10, initial_cash_per_stock=2000,
                 dd_switch_to_spy=0.15, reentry_min_qual=30, reentry_spy_ma=50,
                 spy_cooldown_weeks=4, slippage=0.001,
                 high_window=20, rank_window=_RANK_WINDOW, recent_days=5):
    if start_date is None:
        start_date = close.index[min(rank_window, len(close.index) - 1)]
    if end_date is None:
        end_date = close.index[-1]

    dates = close.resample("W-FRI").last().index
    dates = [d for d in dates if start_date <= d <= end_date and d in close.index]
    if len(dates) < 2:
        return None

    date_to_idx = {d: i for i, d in enumerate(close.index)}
    friday_to_next = {dates[i]: dates[i + 1] for i in range(len(dates) - 1)}

    spy_sma = close["SPY"].rolling(reentry_spy_ma).mean()

    initial_capital = buy_top * initial_cash_per_stock
    cash = float(initial_capital)
    positions: dict[str, float] = {}
    spy_shares = 0.0
    spy_mode = False
    weeks_since_stock_entry = 999
    peak_port_value = float(initial_capital)
    records = []
    trade_log = []
    buy_dates: dict[str, datetime] = {}
    hold_periods: list[int] = []
    prev_friday = None
    prev_port_value = None
    position_budget = initial_cash_per_stock
    budget_year = None

    for friday in dates:
        if friday not in friday_to_next:
            break
        idx = date_to_idx[friday]
        spy_price = close.iloc[idx]["SPY"]

        top20 = qualify_at_date(close, symbols, idx, high_window, rank_window, recent_days)
        top20_set = {s for s, _ in top20}
        n_qual = len(top20)
        n_buy = calc_buy_count(n_qual, buy_top)
        buy_list = [s for s, _ in top20[:n_buy]] if n_buy > 0 else []

        # Mark the portfolio to market at this Friday close before executing
        # trades scheduled for the next trading day's open.
        prices = {s: close.iloc[idx][s] for s in positions if pd.notna(close.iloc[idx][s])}
        h_val = calc_portfolio_value(0, positions, prices, spy_mode, spy_shares, spy_price)
        port_value = cash + h_val
        cash_at_close = cash
        n_positions_at_close = len(positions)
        spy_mode_at_close = spy_mode

        peak_port_value, dd_from_peak = calc_drawdown(port_value, peak_port_value)
        if budget_year != friday.year:
            position_budget = max(1, int(port_value / buy_top))
            budget_year = friday.year

        trading_mode = "spy" if spy_mode else "stocks"

        # --- execution price: next trading day open + slippage ---
        exec_idx = min(idx + 1, len(close) - 1)
        exec_date = close.index[exec_idx]

        def _exec_price(sym, is_buy):
            p = open_prices.iloc[exec_idx][sym]
            if pd.isna(p) or p <= 0:
                p = close.iloc[idx][sym]
            if pd.isna(p) or p <= 0:
                return None
            multi = 1 + slippage if is_buy else 1 - slippage
            return p * multi

        spy_ma = spy_sma.loc[friday] if friday in spy_sma.index else None
        spy_entry = is_spy_entry_trigger(spy_mode, dd_from_peak, weeks_since_stock_entry,
                                         dd_switch_to_spy, spy_cooldown_weeks)
        spy_exit = is_spy_exit_trigger(spy_mode, n_qual, spy_price, spy_ma, reentry_min_qual)

        # --- SPY mode entry (drawdown protection, with cooldown) ---
        if spy_entry:
            for sym in list(positions.keys()):
                p = _exec_price(sym, is_buy=False)
                if p is not None and p > 0:
                    proceeds = positions[sym] * p
                    cash += proceeds
                    trade_log.append({"date": exec_date, "action": "SELL", "symbol": sym,
                                      "price": p, "qty": positions[sym], "value": proceeds})
                    if sym in buy_dates:
                        hold_periods.append(int((exec_date - buy_dates[sym]).days / 7))
                        del buy_dates[sym]
                    del positions[sym]
            if is_spy_exit_trigger(True, n_qual, spy_price, spy_ma, reentry_min_qual):
                spy_mode = False
                weeks_since_stock_entry = 0
                trading_mode = "rebalance"
            else:
                spy_p = _exec_price("SPY", is_buy=True)
                if spy_p is not None and spy_p > 0:
                    spy_shares = calc_shares_to_buy(spy_p, cash)
                    cash -= spy_shares * spy_p
                    trade_log.append({"date": exec_date, "action": "BUY_SPY", "symbol": "SPY",
                                      "price": spy_p, "qty": spy_shares, "value": spy_shares * spy_p})
                spy_mode = True
                weeks_since_stock_entry = 999
                trading_mode = "spy_in"

        # --- SPY mode exit (re-entry to stocks) ---
        if not spy_entry and spy_exit:
            spy_p = _exec_price("SPY", is_buy=False)
            if spy_p is not None and spy_p > 0:
                proceeds = spy_shares * spy_p
                cash += proceeds
                trade_log.append({"date": exec_date, "action": "SELL_SPY", "symbol": "SPY",
                                  "price": spy_p, "qty": spy_shares, "value": proceeds})
            spy_shares = 0.0
            spy_mode = False
            weeks_since_stock_entry = 0
            trading_mode = "stocks_in"

        # --- normal stock trading (when NOT in SPY mode) ---
        if not spy_mode:
            sell_syms = calc_sell_list(set(positions.keys()), top20_set)
            for sym in sell_syms:
                p = _exec_price(sym, is_buy=False)
                if p is not None and p > 0:
                    proceeds = positions[sym] * p
                    cash += proceeds
                    trade_log.append({"date": exec_date, "action": "SELL", "symbol": sym,
                                      "price": p, "qty": positions[sym], "value": proceeds})
                    if sym in buy_dates:
                        hold_periods.append(int((exec_date - buy_dates[sym]).days / 7))
                        del buy_dates[sym]
                    del positions[sym]

            for sym in buy_list:
                if sym not in positions and cash >= position_budget:
                    p = _exec_price(sym, is_buy=True)
                    if p is not None and p > 0:
                        shares = calc_shares_to_buy(p, position_budget)
                        if shares == 0:
                            continue
                        cost = shares * p
                        if cash < cost:
                            continue
                        positions[sym] = shares
                        buy_dates[sym] = exec_date
                        cash -= cost
                        trade_log.append({"date": exec_date, "action": "BUY", "symbol": sym,
                                          "price": p, "qty": shares, "value": cost})

        weeks_since_stock_entry = increment_cooldown(spy_mode, weeks_since_stock_entry)

        # --- record Friday close performance before next-Monday trades take effect ---
        if prev_friday is not None and prev_port_value is not None:
            weekly_r = port_value / prev_port_value - 1
            records.append({
                "date": friday,
                "prev_date": prev_friday,
                "holdings_value": h_val,
                "cash": cash_at_close,
                "port_value": port_value,
                "return": weekly_r,
                "n_qualifiers": n_qual,
                "n_positions": n_positions_at_close,
                "n_buy": n_buy,
                "position_budget": position_budget,
                "spy_mode": spy_mode_at_close,
                "trading_mode": trading_mode,
            })
        prev_friday = friday
        prev_port_value = port_value

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
    close, open_prices = _load_data(symbols, years)
    avail = [s for s in symbols if s in close.columns]

    full_start = close.index[-1] - timedelta(days=years * 365)
    bull_window, bear_window = detect_bull_bear_windows(close)

    periods = [
        ("full", "全周期", full_start, None),
    ]
    if bull_window:
        periods.append(("bull", f"牛市 {bull_window[0].date()}→{bull_window[1].date()}", bull_window[0], bull_window[1]))
    if bear_window:
        periods.append(("bear", f"熊市 {bear_window[0].date()}→{bear_window[1].date()}", bear_window[0], bear_window[1]))
    periods.append(("sideways", "猴市 2025-10-01→2026-04-29",
                    datetime(2025, 10, 1), datetime(2026, 4, 30)))
    periods.append(("severe_bear", "最大回撤期 2021-11-05→2023-03-10",
                    datetime(2021, 11, 5), datetime(2023, 3, 10)))

    results = []
    multi_pf = {}

    for key, label, sd, ed in periods:
        print(f"\n  ▶ {label}")
        result = run_backtest(close, open_prices, avail, name_map, start_date=sd, end_date=ed,
                              top_n=top_n, buy_top=buy_top,
                              initial_cash_per_stock=initial_cash_per_stock,
                              dd_switch_to_spy=0.15, reentry_min_qual=30, reentry_spy_ma=50)
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
        colors = {"full": "#2196F3", "bull": "#4CAF50", "bear": "#f44336", "sideways": "#FF9800", "severe_bear": "#9C27B0"}
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
