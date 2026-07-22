"""Export public static-site data for the machine account pages.

Two intended runs:
- Saturday: read the generated strategy JSON and publish the coming actions.
- Monday: fetch Webull account/positions/orders and publish execution results.

The website remains fully static. This script is the only place that touches local
strategy files or Webull credentials.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SITE_DATA_DIR = ROOT / "web" / "src" / "data"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _latest_strategy_file() -> Path | None:
    if not DATA_DIR.exists():
        return None
    files = sorted(DATA_DIR.glob("strategy_*.json"), reverse=True)
    return files[0] if files else None


def _round(value: Any, digits: int = 2) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _pct(value: float | None) -> float:
    return round(value * 100, 2) if value is not None else 0.0


def _return_pct(current: float | None, previous: float | None) -> float:
    if not current or not previous:
        return 0.0
    return round((current / previous - 1) * 100, 2)


def _mode_label(mode: str | None) -> str:
    return {
        "stocks": "进攻中",
        "rebalance": "轮动中",
        "spy": "防守中",
    }.get(mode or "", "观察中")


def _strategy_note(strategy: dict[str, Any]) -> str:
    buys = len(strategy.get("buy_list", []))
    sells = len(strategy.get("sell_list", []))
    holds = len(strategy.get("hold_list", []))
    mode = _mode_label(strategy.get("mode"))
    return f"账户状态：{mode}。本周计划买入 {buys} 只，卖出 {sells} 只，继续持有 {holds} 只。"


def _load_site_file(name: str, default: Any, site_data_dir: Path) -> Any:
    return _read_json(site_data_dir / name, default)


def _save_site_file(name: str, data: Any, site_data_dir: Path) -> None:
    _write_json(site_data_dir / name, data)


def export_strategy(strategy_file: Path | None, site_data_dir: Path, initial_capital: float | None) -> None:
    strategy_file = strategy_file or _latest_strategy_file()
    if not strategy_file:
        raise FileNotFoundError("No strategy_*.json found under data/. Run src/strategy.py first.")

    strategy = _read_json(strategy_file, {})

    generated_at = strategy.get("generated_at") or date.today().isoformat()
    signal_date = strategy.get("signal_date") or generated_at
    executed_at = strategy.get("execution_date") or generated_at
    portfolio_value = _round(strategy.get("portfolio_value"), 2)

    plan = {
        "active": True,
        "status": "策略公布",
        "generatedAt": generated_at,
        "signalDate": signal_date,
        "executionDate": executed_at,
        "mode": _mode_label(strategy.get("mode")),
        "nQualified": strategy.get("n_qualified"),
        "portfolioValue": portfolio_value,
        "buyList": strategy.get("buy_list", []),
        "sellList": strategy.get("sell_list", []),
        "holdList": strategy.get("hold_list", []),
    }

    _save_site_file("plan.json", plan, site_data_dir)
    print(f"Exported strategy data from {strategy_file} -> {site_data_dir}")


def _empty_plan() -> dict[str, Any]:
    return {
        "active": False,
        "status": "暂无策略公布",
        "generatedAt": None,
        "signalDate": None,
        "executionDate": None,
        "mode": None,
        "nQualified": None,
        "portfolioValue": None,
        "buyList": [],
        "sellList": [],
        "holdList": [],
    }


def _load_config(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f)


def _pick_float(data: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        parsed = _round(value, 4)
        if parsed is not None:
            return parsed
    return None


def _pick_symbol(data: dict[str, Any]) -> str:
    ticker = data.get("ticker") if isinstance(data.get("ticker"), dict) else {}
    return str(data.get("symbol") or ticker.get("symbol") or "").upper()


def _date_from_order(order: dict[str, Any], fallback: str) -> str:
    raw_date = (
        order.get("filled_time_at")
        or order.get("filledTimeAt")
        or order.get("place_time_at")
        or order.get("placeTimeAt")
        or order.get("create_time")
        or order.get("createTime")
        or fallback
    )
    return str(raw_date)[:10]


def _sort_time_from_order(order: dict[str, Any], fallback: str) -> str:
    raw_time = (
        order.get("filled_time_at")
        or order.get("filledTimeAt")
        or order.get("place_time_at")
        or order.get("placeTimeAt")
        or order.get("create_time")
        or order.get("createTime")
        or fallback
    )
    return str(raw_time)


def _flatten_orders(raw_orders: Any) -> list[dict[str, Any]]:
    if isinstance(raw_orders, dict):
        raw_orders = raw_orders.get("data") or raw_orders.get("orders") or raw_orders.get("items") or []
    if not isinstance(raw_orders, list):
        return []

    flattened = []
    for item in raw_orders:
        if not isinstance(item, dict):
            continue
        nested = item.get("orders")
        if isinstance(nested, list):
            flattened.extend(order for order in nested if isinstance(order, dict))
        else:
            flattened.append(item)
    return flattened


def _attach_realized_pnl(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lots: dict[str, list[dict[str, float]]] = {}
    chronological = sorted(trades, key=lambda item: item.get("_sortTime", item["date"]))

    for trade in chronological:
        symbol = trade["symbol"]
        qty = float(trade["quantity"])
        price = float(trade["price"])
        if trade["action"] == "BUY":
            lots.setdefault(symbol, []).append({"qty": qty, "price": price})
            continue

        remaining = qty
        cost = 0.0
        matched_qty = 0.0
        symbol_lots = lots.setdefault(symbol, [])
        while remaining > 1e-9 and symbol_lots:
            lot = symbol_lots[0]
            take = min(remaining, lot["qty"])
            cost += take * lot["price"]
            matched_qty += take
            remaining -= take
            lot["qty"] -= take
            if lot["qty"] <= 1e-9:
                symbol_lots.pop(0)

        if matched_qty <= 1e-9:
            continue

        proceeds = matched_qty * price
        pnl = proceeds - cost
        trade["realizedPnl"] = round(pnl, 2)
        trade["realizedPnlPct"] = round(pnl / cost * 100, 2) if cost else None
        if remaining > 1e-9:
            trade["note"] = "部分成本匹配"

    for trade in trades:
        trade.pop("_sortTime", None)
    return trades


def _previous_saturday(date_str: str) -> str:
    executed = datetime.fromisoformat(date_str).date()
    days_since_saturday = (executed.weekday() - 5) % 7
    if days_since_saturday == 0:
        days_since_saturday = 7
    return (executed - timedelta(days=days_since_saturday)).isoformat()


def _update_weekly_from_trades(
    site_data_dir: Path,
    trades: list[dict[str, Any]],
    net_value: float | None,
    cumulative_return_pct: float | None,
) -> None:
    if not trades:
        return

    existing = {
        item.get("slug"): item
        for item in _load_site_file("weekly.json", [], site_data_dir)
        if isinstance(item, dict) and item.get("slug")
    }
    grouped: dict[str, dict[str, list[str]]] = {}
    for trade in trades:
        date_str = trade.get("date")
        symbol = trade.get("symbol")
        action = trade.get("action")
        if not date_str or not symbol or action not in {"BUY", "SELL"}:
            continue
        row = grouped.setdefault(date_str, {"buys": [], "sells": []})
        key = "buys" if action == "BUY" else "sells"
        if symbol not in row[key]:
            row[key].append(symbol)

    latest_date = max(grouped)
    weekly = []
    for date_str in sorted(grouped, reverse=True):
        old = existing.get(date_str, {})
        is_latest = date_str == latest_date
        buys = grouped[date_str]["buys"]
        sells = grouped[date_str]["sells"]
        weekly.append({
            "slug": date_str,
            "date": date_str,
            "title": f"{date_str}：交易执行记录",
            "phase": "已执行",
            "publishedAt": old.get("publishedAt") or _previous_saturday(date_str),
            "executedAt": date_str,
            "netValue": round(net_value, 2) if is_latest and net_value is not None else None,
            "weeklyReturnPct": None,
            "cumulativeReturnPct": cumulative_return_pct if is_latest else None,
            "spyReturnPct": None,
            "qqqReturnPct": None,
            "buys": sorted(buys),
            "sells": sorted(sells),
            "holds": [],
            "proofUrl": old.get("proofUrl", ""),
        })

    _save_site_file("weekly.json", weekly, site_data_dir)


def _fetch_webull(config_path: Path, order_days: int) -> tuple[float | None, list[dict[str, Any]], list[dict[str, Any]]]:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    logging.disable(logging.CRITICAL)

    from src.executor import init_webull

    config = _load_config(config_path)
    trade_client, account_id = init_webull(config)

    net_value = None
    balance_res = trade_client.account_v2.get_account_balance(account_id)
    if balance_res.status_code == 200:
        balance = balance_res.json()
        if isinstance(balance, dict):
            for asset in balance.get("account_currency_assets", []):
                if isinstance(asset, dict) and asset.get("currency") == "USD":
                    market_value = _pick_float(asset, ["market_value", "marketValue"])
                    cash_balance = _pick_float(asset, ["cash_balance", "cashBalance"])
                    if market_value is not None and cash_balance is not None:
                        net_value = market_value + cash_balance
                    break
            if net_value is None:
                net_value = _pick_float(balance, [
                    "net_liquidation", "netLiquidation", "total_account_value",
                    "totalAccountValue", "equity", "account_value", "accountValue",
                ])

    positions = []
    pos_res = trade_client.account_v2.get_account_position(account_id)
    if pos_res.status_code == 200:
        raw_positions = pos_res.json()
        if isinstance(raw_positions, dict):
            raw_positions = raw_positions.get("data", [])
        if isinstance(raw_positions, list):
            for item in raw_positions:
                if not isinstance(item, dict):
                    continue
                symbol = _pick_symbol(item)
                qty = _pick_float(item, ["quantity", "qty", "position", "positionQty"])
                if not symbol or not qty:
                    continue
                last_price = _pick_float(item, ["last_price", "lastPrice", "market_price", "marketPrice"])
                cost_price = _pick_float(item, ["cost_price", "costPrice", "avg_cost", "avgCost"])
                market_value = _pick_float(item, ["market_value", "marketValue", "position_value", "positionValue"])
                if market_value is None and last_price is not None:
                    market_value = qty * last_price
                unrealized = _pick_float(item, [
                    "unrealized_profit_loss", "unrealizedProfitLoss",
                    "unrealized_pl", "unrealizedPnl", "pnl",
                ])
                unrealized_pct = _pick_float(item, ["unrealized_pl_rate", "unrealizedPnlRate", "unrealizedProfitLossRate", "pnlRatio"])
                if unrealized_pct is not None and abs(unrealized_pct) < 1:
                    unrealized_pct *= 100
                elif unrealized is not None and cost_price and qty:
                    unrealized_pct = unrealized / (cost_price * qty) * 100
                positions.append({
                    "symbol": symbol,
                    "name": item.get("name") or item.get("securityName") or symbol,
                    "quantity": round(qty, 4),
                    "marketValue": round(market_value or 0, 2),
                    "weightPct": 0,
                    "unrealizedPnl": round(unrealized or 0, 2),
                    "unrealizedPnlPct": round(unrealized_pct or 0, 2),
                    "holdingDays": 0,
                })

    total_market = sum(item["marketValue"] for item in positions)
    if total_market > 0:
        for item in positions:
            item["weightPct"] = round(item["marketValue"] / total_market * 100, 2)

    trades = []
    start = (date.today() - timedelta(days=order_days)).isoformat()
    end = date.today().isoformat()
    try:
        order_res = trade_client.order_v3.get_order_history(account_id, 100, start, end)
        if order_res.status_code == 200:
            for order in _flatten_orders(order_res.json()):
                status = str(order.get("status") or order.get("order_status") or "").upper()
                if status and "FILLED" not in status and "EXECUTED" not in status:
                    continue
                symbol = _pick_symbol(order)
                qty = _pick_float(order, ["filled_quantity", "filledQuantity", "total_quantity", "totalQuantity", "quantity", "qty"])
                price = _pick_float(order, ["filled_price", "filledPrice", "avg_fill_price", "avgFilledPrice", "averagePrice", "price"])
                side = str(order.get("side") or order.get("action") or "").upper()
                trade_date = _date_from_order(order, end)
                if not symbol or not qty or not price or side not in {"BUY", "SELL"}:
                    continue
                trades.append({
                    "date": trade_date,
                    "_sortTime": _sort_time_from_order(order, trade_date),
                    "action": side,
                    "symbol": symbol,
                    "quantity": qty,
                    "price": round(price, 2),
                    "amount": round(qty * price, 2),
                    "realizedPnl": None,
                    "realizedPnlPct": None,
                    "note": "模型交易",
                })
    except Exception as exc:
        print(f"Warning: failed to export order history: {exc}")

    trades = _attach_realized_pnl(trades)

    latest_buy_dates = {}
    for trade in sorted(trades, key=lambda item: item["date"]):
        if trade["action"] == "BUY":
            latest_buy_dates[trade["symbol"]] = trade["date"]
    today = date.today()
    for item in positions:
        buy_date = latest_buy_dates.get(item["symbol"])
        if buy_date:
            try:
                item["holdingDays"] = (today - datetime.fromisoformat(buy_date).date()).days
            except ValueError:
                item["holdingDays"] = 0

    return net_value, positions, sorted(trades, key=lambda item: item["date"], reverse=True)


def export_live(config_path: Path, site_data_dir: Path, initial_capital: float | None, order_days: int) -> None:
    net_value, holdings, trades = _fetch_webull(config_path, order_days)
    latest = _load_site_file("latest.json", {}, site_data_dir)

    if net_value is not None:
        latest["asOf"] = date.today().isoformat()
        latest["netValue"] = round(net_value, 2)
        latest["initialCapital"] = initial_capital
        latest["cumulativeReturnPct"] = round((net_value / initial_capital - 1) * 100, 2) if initial_capital else None
    cumulative = latest.get("cumulativeReturnPct")

    if holdings:
        _save_site_file("holdings.json", holdings, site_data_dir)
    if trades:
        _save_site_file("trades.json", trades, site_data_dir)
        _update_weekly_from_trades(site_data_dir, trades, net_value, cumulative)
    _save_site_file("latest.json", latest, site_data_dir)
    print(f"Exported live Webull data -> {site_data_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export static website data.")
    sub = parser.add_subparsers(dest="mode", required=True)

    strategy_cmd = sub.add_parser("strategy", help="Export Saturday strategy publication data")
    strategy_cmd.add_argument("--strategy-file", type=Path, default=None)
    strategy_cmd.add_argument("--site-data-dir", type=Path, default=SITE_DATA_DIR)
    strategy_cmd.add_argument("--initial-capital", type=float, default=None,
                              help="Optional net-deposit return base. If omitted, cumulative return is hidden.")

    live_cmd = sub.add_parser("live", help="Export Monday live Webull account data")
    live_cmd.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    live_cmd.add_argument("--site-data-dir", type=Path, default=SITE_DATA_DIR)
    live_cmd.add_argument("--initial-capital", type=float, default=None,
                          help="Optional net-deposit return base. If omitted, cumulative return is hidden.")
    live_cmd.add_argument("--order-days", type=int, default=120)

    args = parser.parse_args()
    if args.mode == "strategy":
        export_strategy(args.strategy_file, args.site_data_dir, args.initial_capital)
    elif args.mode == "live":
        export_live(args.config, args.site_data_dir, args.initial_capital, args.order_days)


if __name__ == "__main__":
    main()
