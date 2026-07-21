"""
SP500 20日新高雷达 — 筛选过去20日创20日新高的股票（新高日落在最近5日），按120日涨幅排名
"""

from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def fetch_sp500_constituents() -> pd.DataFrame:
    resp = requests.get(_SP500_URL, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = tables[0][["Symbol", "Security"]].copy()
    df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)
    print(f"  ✓ Fetched {len(df)} SP500 constituents")
    return df


def qualify_20day_highs(
    close: pd.DataFrame,
    symbols: list[str],
    rank_window: int = 120,
    high_window: int = 20,
    recent_days: int = 5,
) -> list[dict]:
    """
    For each stock: look at last high_window trading days.
    If the max close in that window falls in the most recent recent_days → qualifies.
    Returns list sorted by rank_window return descending.
    """
    if len(close) <= max(rank_window, high_window):
        return []

    window = close.tail(high_window)
    cutoff = window.index[-recent_days]
    past = close.iloc[-rank_window - 1]

    results = []
    for sym in symbols:
        if sym not in close.columns:
            continue
        series = window[sym].dropna()
        if len(series) < high_window:
            continue

        max_val = series.max()
        max_date = series.idxmax()

        if max_date >= cutoff:
            cur_price = close[sym].iloc[-1]
            past_price = past[sym]
            if pd.notna(cur_price) and pd.notna(past_price) and past_price > 0:
                rank_ret = cur_price / past_price - 1
                results.append({
                    "symbol": sym,
                    "rank_ret": rank_ret,
                    "rank_window": rank_window,
                    "close": cur_price,
                    "high_date": max_date,
                })

    results.sort(key=lambda x: x["rank_ret"], reverse=True)
    return results


def scan_top_strong(top_n: int = 20) -> pd.DataFrame:
    constituents = fetch_sp500_constituents()
    symbols = constituents["Symbol"].tolist()
    name_map = dict(zip(constituents["Symbol"], constituents["Security"]))

    end = datetime.today()
    start = end - timedelta(days=260)

    print(f"  Downloading {len(symbols)} tickers (last 260 days)...")
    data = yf.download(
        symbols,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    close = data["Close"].dropna(axis=1, how="all")
    if isinstance(close.columns, pd.MultiIndex):
        close = close.droplevel(0, axis=1)

    qualifiers = qualify_20day_highs(close, symbols)
    print(f"  ✓ {len(qualifiers)} stocks qualified (new 20-day high this week)")

    top = qualifiers[:top_n]
    rows = []
    for i, q in enumerate(top, 1):
        rows.append({
            "Rank": i,
            "Symbol": q["symbol"],
            "Security": name_map.get(q["symbol"], q["symbol"]),
            "Close": q["close"],
            "Return_pct": q["rank_ret"] * 100,
            "High_Date": q["high_date"].strftime("%m/%d"),
        })
    return pd.DataFrame(rows)


def main():
    print(f"\n{'=' * 60}")
    print(f"  📡 SP500 20日新高雷达 — {datetime.today().strftime('%Y-%m-%d')}")
    print(f"{'=' * 60}")

    df = scan_top_strong()

    print(f"\n  {'Rank':<5} {'Symbol':<8} {'Security':<28} {'Close':<10} {'Return%':<10} {'新高日':<8}")
    print(f"  {'-' * 69}")
    for _, r in df.iterrows():
        bar = "🟢" if r["Return_pct"] > 0 else "🔴"
        print(f"  #{int(r['Rank']):<3} {bar} {r['Symbol']:<8} {r['Security'][:26]:<26} {r['Close']:<10.2f} {r['Return_pct']:+8.2f}%  {r['High_Date']}")
    print(f"{'=' * 60}\n")

    return df


if __name__ == "__main__":
    main()
