"""Unified daily market data access.

All providers return the same shape: timezone-naive daily DatetimeIndex,
ascending rows, original input symbols as columns, and float Open/Close values.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv


_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
_LSE_BASE_URL = "https://api.londonstrategicedge.com"
_HEADERS = {
    "User-Agent": "stock-radar/0.1",
    "Content-Type": "application/json",
    "Prefer": "count=none",
}

load_dotenv()


@dataclass(frozen=True)
class PriceData:
    close: pd.DataFrame
    open: pd.DataFrame


def _load_market_data_config() -> dict:
    if not os.path.exists(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH) as f:
        config = yaml.safe_load(f) or {}
    return config.get("market_data", {}) or {}


def get_provider_name(provider: str | None = None) -> str:
    cfg = _load_market_data_config()
    value = provider or os.getenv("MARKET_DATA_PROVIDER") or cfg.get("provider") or "yahoo"
    return str(value).strip().lower()


def load_daily_prices(
    symbols: Iterable[str],
    start: date | datetime | str,
    end: date | datetime | str,
    provider: str | None = None,
) -> PriceData:
    """Load daily Open/Close prices from the configured provider."""
    unique_symbols = sorted(set(symbols))
    provider_name = get_provider_name(provider)
    if provider_name in {"yahoo", "yf", "yfinance"}:
        return _load_yahoo_daily_prices(unique_symbols, start, end)
    if provider_name in {"lse", "londonstrategicedge", "london_strategic_edge"}:
        return _load_lse_daily_prices(unique_symbols, start, end)
    raise ValueError(f"Unsupported market data provider: {provider_name}")


def _normalize_date(value: date | datetime | str) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _normalize_matrix(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=symbols)
    out = df.copy()
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out.sort_index()
    out = out.loc[~out.index.duplicated(keep="last")]
    out = out.reindex(columns=symbols)
    return out.astype(float)


def _load_yahoo_daily_prices(symbols: list[str], start, end) -> PriceData:
    import yfinance as yf

    data = yf.download(
        symbols,
        start=_normalize_date(start),
        end=_normalize_date(end),
        progress=False,
        auto_adjust=True,
    )
    if data.empty:
        empty = pd.DataFrame(columns=symbols)
        return PriceData(close=empty, open=empty)

    def extract(field: str) -> pd.DataFrame:
        values = data[field] if field in data else pd.DataFrame(index=data.index)
        if isinstance(values, pd.Series):
            values = values.to_frame(symbols[0] if len(symbols) == 1 else field)
        if isinstance(values.columns, pd.MultiIndex):
            values = values.droplevel(0, axis=1)
        return _normalize_matrix(values.dropna(axis=1, how="all"), symbols)

    close = extract("Close")
    open_prices = extract("Open")
    common = close.columns.intersection(open_prices.columns)
    return PriceData(close=close[common], open=open_prices[common])


def _lse_symbol(symbol: str) -> str:
    return symbol.replace("-", ".")


def _get_lse_api_key() -> str:
    cfg = _load_market_data_config()
    api_key = (
        os.getenv("LSE_API_KEY")
        or cfg.get("lse_api_key")
        or cfg.get("api_key")
    )
    if not api_key:
        raise RuntimeError("LSE_API_KEY or market_data.api_key is required for the LSE provider")
    return api_key


def _load_lse_daily_prices(symbols: list[str], start, end) -> PriceData:
    start_date = _normalize_date(start)
    end_date = _normalize_date(end)
    api_key = _get_lse_api_key()
    headers = {**_HEADERS, "x-api-key": api_key}
    rows: list[dict] = []
    symbol_map = {_lse_symbol(sym): sym for sym in symbols}
    lse_symbols = list(symbol_map.keys())

    # Keep each request under PostgREST URL/row limits while still avoiding one
    # request per ticker for the S&P 500 universe.
    chunk_size = int(os.getenv("LSE_CHUNK_SIZE", "75"))
    page_size = int(os.getenv("LSE_PAGE_SIZE", "10000"))
    for i in range(0, len(lse_symbols), chunk_size):
        chunk = lse_symbols[i:i + chunk_size]
        offset = 0
        while True:
            params = {
                "select": "timestamp,symbol,open,close",
                "symbol": f"in.({','.join(chunk)})",
                "and": f"(timestamp.gte.{start_date},timestamp.lt.{end_date})",
                "order": "timestamp.asc",
                "limit": str(page_size),
                "offset": str(offset),
            }
            page = _lse_get_json("x_candles_1d", params, headers)
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

    if not rows:
        empty = pd.DataFrame(columns=symbols)
        return PriceData(close=empty, open=empty)

    raw = pd.DataFrame(rows)
    raw["date"] = pd.to_datetime(raw["timestamp"].str.slice(0, 10))
    raw["symbol"] = raw["symbol"].map(symbol_map)
    raw = raw.dropna(subset=["symbol"])
    raw["open"] = pd.to_numeric(raw["open"], errors="coerce")
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw = raw.drop_duplicates(subset=["date", "symbol"], keep="last")

    close = raw.pivot(index="date", columns="symbol", values="close")
    open_prices = raw.pivot(index="date", columns="symbol", values="open")
    close = _normalize_matrix(close.dropna(axis=1, how="all"), symbols)
    open_prices = _normalize_matrix(open_prices.dropna(axis=1, how="all"), symbols)
    if "SPY" in close.columns:
        calendar = close["SPY"].notna()
        close = close.loc[calendar]
        open_prices = open_prices.loc[calendar]
    common = close.columns.intersection(open_prices.columns)
    return PriceData(close=close[common], open=open_prices[common])


def _lse_get_json(path: str, params: dict, headers: dict) -> list[dict]:
    url = f"{_LSE_BASE_URL}/{path}"
    for attempt in range(5):
        resp = requests.get(url, params=params, headers=headers, timeout=60)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else min(30, 2 ** attempt * 3)
        time.sleep(wait)
    resp.raise_for_status()
    return []
