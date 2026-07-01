"""
回测 — SP500 20日新高 + 每周轮动策略
每周五：筛选过去20个交易日内创20日新高的股票（新高日落在本周），按20日涨幅排名选Top N
等权持有至下周五，轮动
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

_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_FONT_PATH):
    fm.fontManager.addfont(_FONT_PATH)
    plt.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scanner import fetch_sp500_constituents

_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "backtest")


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _load_data(symbols: list[str], years: int = 3) -> pd.DataFrame:
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

    print(f"  Daily range: {close.index[0].date()} → {close.index[-1].date()}")
    print(f"  Trading days: {len(close)}")
    return close


def _qualify_stocks(close: pd.DataFrame, symbols: list[str], friday_idx: int) -> list[tuple[str, float]]:
    """
    For a given Friday index in the close DataFrame:
    - Look back 20 trading days window: [friday_idx - 19, friday_idx]
    - A stock qualifies if its max close in this window falls in the last 5 trading days
    - For qualifiers, compute 20-day return: close[friday] / close[friday - 20] - 1
    Returns list of (symbol, 20d_return) sorted descending.
    """
    if friday_idx < 20:
        return []

    window = close.iloc[friday_idx - 19 : friday_idx + 1]
    cutoff = window.index[-5]  # last 5 trading days start
    past = close.iloc[friday_idx - 20]  # 20 trading days ago

    results = []
    for sym in symbols:
        if sym not in close.columns:
            continue
        series = window[sym].dropna()
        if len(series) < 20:
            continue

        max_val = series.max()
        max_date = series.idxmax()

        if max_date >= cutoff:
            cur = close.loc[window.index[-1], sym]
            if pd.notna(cur) and pd.notna(past.get(sym, None)) and past[sym] > 0:
                ret_20d = cur / past[sym] - 1
                results.append((sym, ret_20d))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def run_backtest(top_n: int = 20, years: int = 3) -> dict:
    os.makedirs(_OUTPUT_DIR, exist_ok=True)

    print(f"{'=' * 60}")
    print(f"  📊 SP500 20日新高轮动回测")
    print(f"{'=' * 60}")
    print(f"  条件: 过去20日中最高价落在近5日（本周）")
    print(f"  排名: 按20日涨幅选 Top {top_n}, 等权持有 1 周")
    print(f"  区间: 最近 {years} 年")
    print()

    constituents = fetch_sp500_constituents()
    symbols = constituents["Symbol"].tolist()
    name_map = dict(zip(constituents["Symbol"], constituents["Security"]))

    close = _load_data(symbols, years)
    avail = [s for s in symbols if s in close.columns]
    spy = close["SPY"] if "SPY" in close.columns else None

    # Get all Friday dates in the data
    all_fridays = pd.Series(close.resample("W-FRI").last().index)
    all_fridays = all_fridays[all_fridays.isin(close.index)].tolist()

    # Build index map
    date_to_idx = {d: i for i, d in enumerate(close.index)}
    friday_to_next = {all_fridays[i]: all_fridays[i + 1] for i in range(len(all_fridays) - 1)}

    # Run weekly simulation
    records = []
    prev_selected: list[str] = []
    trades = []
    name_map_sym = dict(zip(constituents["Symbol"], constituents["Security"]))

    for friday in all_fridays:
        if friday not in friday_to_next:
            break
        next_friday = friday_to_next[friday]
        idx = date_to_idx[friday]
        qualified = _qualify_stocks(close, avail, idx)

        if not qualified:
            continue

        selected = [s for s, _ in qualified[:top_n]]
        rets_20d = dict(qualified)
        next_idx = date_to_idx[next_friday]

        fwd_rets = []
        for sym in selected:
            p0 = close.iloc[idx][sym]
            p1 = close.iloc[next_idx][sym]
            if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                fwd_rets.append(p1 / p0 - 1)
        port_ret = np.mean(fwd_rets) if fwd_rets else 0

        record = {
            "date": friday,
            "next_date": next_friday,
            "n_qualifiers": len(qualified),
            "return_20d_avg": np.mean([rets_20d[s] for s in selected]),
            "return": port_ret,
            "selected": selected,
        }
        records.append(record)

        # Track turnover
        cur_set = set(selected)
        prev_set = set(prev_selected)
        if prev_selected:
            trades.append({
                "date": friday,
                "n_qualifiers": len(qualified),
                "new_buys": [s for s in selected if s not in prev_set],
                "sells": [s for s in prev_selected if s not in cur_set],
                "hold": [s for s in selected if s in prev_set],
            })
        prev_selected = selected

    # Build performance DataFrame
    pf = pd.DataFrame(records).set_index("date")
    pf["cum_return"] = (1 + pf["return"]).cumprod()
    pf["peak"] = pf["cum_return"].cummax()
    pf["drawdown"] = pf["cum_return"] / pf["peak"] - 1

    # SPY benchmark: buy-and-hold over same period
    if spy is not None and not pf.empty:
        spy_entry = spy.loc[pf.index].values
        spy_exit = spy.loc[pf["next_date"]].values
        spy_ret = pd.Series(spy_exit / spy_entry - 1, index=pf.index)
        pf["spy_return"] = spy_ret
        pf["spy_cum"] = (1 + pf["spy_return"]).cumprod()

    return {"pf": pf, "trades": trades, "close": close, "name_map": name_map_sym}


def compute_performance(pf: pd.DataFrame, rf: float = 0.05) -> dict:
    total_ret = pf["cum_return"].iloc[-1] - 1
    years = (pf.index[-1] - pf.index[0]).days / 365.25
    cagr = (1 + total_ret) ** (1 / years) - 1

    dd = pf["drawdown"]
    max_dd = dd.min()

    weekly_r = pf["return"]
    excess_pa = weekly_r.mean() * 52 - rf
    std_pa = weekly_r.std() * np.sqrt(52)
    sharpe = excess_pa / std_pa if std_pa > 0 else 0

    win_rate = (weekly_r > 0).mean()
    avg_win = weekly_r[weekly_r > 0].mean() if (weekly_r > 0).any() else 0
    avg_loss = weekly_r[weekly_r < 0].mean() if (weekly_r < 0).any() else 0
    profit_factor = (
        weekly_r[weekly_r > 0].sum() / abs(weekly_r[weekly_r < 0].sum())
        if (weekly_r < 0).any()
        else float("inf")
    )

    result = {
        "总收益率": _fmt_pct(total_ret),
        "年化收益率": _fmt_pct(cagr),
        "最大回撤": _fmt_pct(max_dd),
        "夏普比": f"{sharpe:.2f}",
        "胜率": _fmt_pct(win_rate),
        "平均周盈利": _fmt_pct(avg_win),
        "平均周亏损": _fmt_pct(avg_loss),
        "盈亏比": f"{avg_win / abs(avg_loss) if avg_loss != 0 else float('inf'):.2f}",
        "获利因子": f"{profit_factor:.2f}",
        "交易周数": f"{len(weekly_r)}",
    }

    if "spy_cum" in pf.columns:
        spy_total = pf["spy_cum"].iloc[-1] - 1
        spy_cagr = (1 + spy_total) ** (1 / years) - 1
        result["SPY 总收益率"] = _fmt_pct(spy_total)
        result["SPY 年化"] = _fmt_pct(spy_cagr)

    return result


def plot_equity_curve(pf: pd.DataFrame, output_path: str):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(pf.index, pf["cum_return"], label="Top 20 轮动", linewidth=2, color="#2196F3")
    if "spy_cum" in pf.columns:
        ax1.plot(pf.index, pf["spy_cum"], label="SPY 买入持有", linewidth=2, color="#FF5722", alpha=0.7)
    ax1.axhline(1.0, color="gray", linestyle="--", linewidth=0.5)
    ax1.set_ylabel("累计收益")
    ax1.set_title("SP500 20日新高轮动 — 回测权益曲线")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    ax2.fill_between(pf.index, 0, pf["drawdown"] * 100, color="#f44336", alpha=0.3)
    ax2.plot(pf.index, pf["drawdown"] * 100, color="#f44336", linewidth=1)
    ax2.set_ylabel("回撤 (%)")
    ax2.set_xlabel("日期")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    if output_path:
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ 权益曲线 -> {output_path}")


def main(top_n: int = 20, years: int = 3):
    result = run_backtest(top_n=top_n, years=years)
    pf = result["pf"]

    perf = compute_performance(pf)

    print(f"\n{'=' * 60}")
    print(f"  回测结果")
    print(f"{'=' * 60}")
    for k, v in perf.items():
        print(f"  {k:<14} {v}")

    trades = result["trades"]
    if trades:
        avg_churn = np.mean([len(t["new_buys"]) + len(t["sells"]) for t in trades])
        print(f"  平均换手    {avg_churn:.0f} 只/周 ({(avg_churn / (top_n * 2)) * 100:.0f}%)")
    print(f"  数据跨度    {len(result['close'])} 个交易日")

    chart_path = os.path.join(_OUTPUT_DIR, "equity_curve.png")
    plot_equity_curve(pf, chart_path)

    print(f"{'=' * 60}\n")

    return result


if __name__ == "__main__":
    main()
