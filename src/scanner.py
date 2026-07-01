"""
SP500 Top 20 强势股票雷达 — 扫描 SP500 成分股，按 20 日涨幅排行
"""

from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def fetch_sp500_symbols() -> list[str]:
    resp = requests.get(_SP500_URL, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    symbols = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    print(f"  ✓ Fetched {len(symbols)} SP500 constituents")
    return symbols


def scan_top_strong(top_n: int = 20, lookback_days: int = 20) -> pd.DataFrame:
    symbols = fetch_sp500_symbols()
    end = datetime.today()
    start = end - timedelta(days=lookback_days * 2)

    print(f"  Downloading {len(symbols)} tickers...")
    data = yf.download(
        symbols,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )

    close = data["Close"].dropna(axis=1, how="all")
    lookback = min(lookback_days, len(close) - 1)
    returns = ((close.iloc[-1] / close.iloc[-1 - lookback] - 1) * 100).dropna()
    returns = returns.sort_values(ascending=False)

    top = returns.head(top_n).reset_index()
    top.columns = ["Symbol", "Return_pct"]
    top["Rank"] = range(1, len(top) + 1)

    close_last = close.iloc[-1]
    top["Close"] = top["Symbol"].map(lambda s: close_last.get(s, None))

    print(f"  ✓ Top {top_n} stocks identified")
    return top


def main():
    print(f"\n{'=' * 60}")
    print(f"  📡 SP500 Top 20 强势股票雷达 — {datetime.today().strftime('%Y-%m-%d')}")
    print(f"{'=' * 60}")

    df = scan_top_strong()

    print(f"\n  {'Rank':<6} {'Symbol':<8} {'Close':<10} {'Return%':<10}")
    print(f"  {'-' * 34}")
    for _, r in df.iterrows():
        bar = "🟢" if r["Return_pct"] > 0 else "🔴"
        print(f"  #{int(r['Rank']):<4} {bar} {r['Symbol']:<8} {r['Close']:<10.2f} {r['Return_pct']:+8.2f}%")
    print(f"{'=' * 60}\n")

    return df


if __name__ == "__main__":
    main()
