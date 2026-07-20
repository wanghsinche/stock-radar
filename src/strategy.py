"""
周六 06:00 策略生成:
1. 扫描 SP500 → 20日高候选池
2. 读取上次持仓
3. 对比确定 buy/sell/hold 清单
4. 检查回撤 → SPY 切换判断
5. 写入 data/strategy_YYYY-MM-DD.json
6. TG 推送策略预览
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import yaml
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BEIJING = timezone(timedelta(hours=8))

from src.scanner import fetch_sp500_constituents, qualify_20day_highs
from src.notifier import send_telegram, format_message
from src.trading import (
    calc_buy_count,
    calc_sell_list,
    is_spy_entry_trigger,
    is_spy_exit_trigger,
    increment_cooldown,
    calc_drawdown,
    calc_portfolio_value,
    calc_shares_to_buy,
    DEFAULT_PARAMS,
)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_LAST_POS = os.path.join(_DATA_DIR, "last_positions.json")
_TRADE_LOG = os.path.join(_DATA_DIR, "trade_log.json")
_PERF_HISTORY = os.path.join(_DATA_DIR, "performance_history.json")


def load_config():
    path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_last_positions() -> dict:
    if not os.path.exists(_LAST_POS):
        return {"cash": 20000, "positions": {}, "peak_value": 20000, "spy_mode": False,
                "weeks_since_stock_entry": 999}
    with open(_LAST_POS) as f:
        return json.load(f)


def save_positions(data: dict):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_LAST_POS, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_trade_log() -> list:
    if not os.path.exists(_TRADE_LOG):
        return []
    with open(_TRADE_LOG) as f:
        return json.load(f)


def save_trade_log(log: list):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_TRADE_LOG, "w") as f:
        json.dump(log, f, indent=2, default=str)


def load_perf_history() -> list:
    if not os.path.exists(_PERF_HISTORY):
        return []
    with open(_PERF_HISTORY) as f:
        return json.load(f)


def save_perf_history(history: list):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_PERF_HISTORY, "w") as f:
        json.dump(history, f, indent=2, default=str)


def update_risk_alert(date_str: str, portfolio_value: float, spy_price: float | None, n_qual: int, dd_from_peak: float) -> dict:
    history = [h for h in load_perf_history() if h.get("date") != date_str]
    row = {
        "date": date_str,
        "portfolio_value": round(portfolio_value, 2),
        "spy_price": round(float(spy_price), 4) if spy_price is not None and pd.notna(spy_price) else None,
        "n_qualified": n_qual,
        "drawdown_pct": round(dd_from_peak * 100, 2),
    }
    history.append(row)
    history = sorted(history, key=lambda x: x["date"])[-104:]
    save_perf_history(history)

    alert = {
        "enabled": False,
        "status": "insufficient_history",
        "message": "相对强弱历史不足",
        "relative_strength": None,
        "relative_strength_ma13": None,
        "underperform_spy_13w": None,
        "n_qualified": n_qual,
        "drawdown_pct": round(dd_from_peak * 100, 2),
    }
    valid = [h for h in history if h.get("portfolio_value") and h.get("spy_price")]
    if len(valid) < 13:
        return alert

    latest = valid[-1]
    rs_values = [h["portfolio_value"] / h["spy_price"] for h in valid[-13:]]
    rs = latest["portfolio_value"] / latest["spy_price"]
    rs_ma13 = sum(rs_values) / len(rs_values)
    past = valid[-13]
    strategy_13w = latest["portfolio_value"] / past["portfolio_value"] - 1
    spy_13w = latest["spy_price"] / past["spy_price"] - 1
    weak_rs = rs < rs_ma13
    underperform = strategy_13w < spy_13w
    risk_on = weak_rs and underperform

    alert.update({
        "enabled": risk_on,
        "status": "risk_on" if risk_on else "normal",
        "message": "动量策略相对 SPY 转弱" if risk_on else "相对强弱正常",
        "relative_strength": round(rs, 4),
        "relative_strength_ma13": round(rs_ma13, 4),
        "strategy_13w_pct": round(strategy_13w * 100, 2),
        "spy_13w_pct": round(spy_13w * 100, 2),
        "underperform_spy_13w": underperform,
    })
    return alert


def load_strategy(date_str: str = None) -> dict:
    """Load existing strategy for a given date (default: latest)."""
    if not os.path.exists(_DATA_DIR):
        return None
    files = sorted([f for f in os.listdir(_DATA_DIR) if f.startswith("strategy_") and f.endswith(".json")],
                   reverse=True)
    if not files:
        return None
    target = f"strategy_{date_str}.json" if date_str else files[0]
    path = os.path.join(_DATA_DIR, target)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def main():
    load_dotenv()
    config = load_config()
    top_n = config.get("radar", {}).get("top_n", 20)
    buy_top = 10
    initial_cash_per_stock = 2000

    beijing_now = datetime.now(BEIJING)
    current_year = beijing_now.year
    print(f"\n{'=' * 60}")
    print(f"  🧠 SP500 20日新高 — 策略生成 {beijing_now.strftime('%Y-%m-%d')}")
    print(f"{'=' * 60}")

    # 1. Fetch SP500 + data
    constituents = fetch_sp500_constituents()
    symbols = constituents["Symbol"].tolist()
    name_map = dict(zip(constituents["Symbol"], constituents["Security"]))

    end = beijing_now
    start = end - timedelta(days=60)
    print(f"  Downloading {len(symbols)} tickers...")
    import yfinance as yf
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

    avail = [s for s in symbols if s in close.columns]

    # 2. Qualify
    qualifiers = qualify_20day_highs(close, avail)
    print(f"  ✓ {len(qualifiers)} stocks qualified")

    # 3. Build pool
    top20 = qualifiers[:top_n]
    top20_set = {q["symbol"] for q in top20}
    top10 = [q["symbol"] for q in top20[:buy_top]]
    mid10 = [q["symbol"] for q in top20[buy_top:top_n]]
    n_qual = len(qualifiers)

    # 4. Load last positions
    last = load_last_positions()
    held_symbols = set(last.get("positions", {}).keys())
    last_spy_mode = last.get("spy_mode", False)
    peak_value = last.get("peak_value", 20000)

    # 5. Compute current value (approximate using latest close prices)
    prices = {}
    for sym, shares in last.get("positions", {}).items():
        if sym in close.columns:
            price = close[sym].iloc[-1]
            if pd.notna(price):
                prices[sym] = price
    current_val = calc_portfolio_value(last.get("cash", 20000), last.get("positions", {}), prices)

    peak_value, dd_from_peak = calc_drawdown(current_val, peak_value)
    if last.get("budget_year") == current_year:
        position_budget = last.get("position_budget", initial_cash_per_stock)
    else:
        position_budget = max(1, int(current_val / buy_top))

    # 6. Determine mode
    spy_mode = last_spy_mode
    weeks_since_stock_entry = last.get("weeks_since_stock_entry", 999)

    spy_price = None
    spy_sma50 = None
    if "SPY" in close.columns:
        spy_close = close["SPY"].dropna()
        spy_price = spy_close.iloc[-1] if len(spy_close) > 0 else None
        spy_sma50 = spy_close.rolling(50).mean().iloc[-1] if len(spy_close) >= 50 else None

    mode_reason = ""
    force_rebalance = False
    spy_entry = is_spy_entry_trigger(spy_mode, dd_from_peak, weeks_since_stock_entry)
    reentry_ready = is_spy_exit_trigger(True, n_qual, spy_price, spy_sma50)
    if spy_entry and reentry_ready:
        spy_mode = False
        weeks_since_stock_entry = 0
        force_rebalance = True
        mode_reason = f"回撤 {dd_from_peak*100:.1f}% > 15%, 但合格数 {n_qual} ≥ 30 且 SPY > 50MA, 跳过 SPY 并强制换仓"
    elif spy_entry:
        spy_mode = True
        weeks_since_stock_entry = 999
        mode_reason = f"回撤 {dd_from_peak*100:.1f}% > 15%, 切换到 SPY"
    elif is_spy_exit_trigger(spy_mode, n_qual, spy_price, spy_sma50):
        spy_mode = False
        weeks_since_stock_entry = 0
        mode_reason = f"合格数 {n_qual} ≥ 30 且 SPY > 50MA, 转回股票"
    elif spy_mode:
        spy_above_ma = spy_sma50 is not None and spy_price is not None and spy_price > spy_sma50
        mode_reason = f"SPY 模式中 (合格数 {n_qual}, SPY > 50MA: {spy_above_ma if spy_price else 'N/A'})"

    weeks_since_stock_entry = increment_cooldown(spy_mode, weeks_since_stock_entry)

    # 7. Determine buy/sell/hold lists
    if spy_mode:
        sell_list = sorted(held_symbols)
        buy_list = []
        hold_list = []
        strategy_name = "spy"
    else:
        sell_list = sorted(held_symbols) if force_rebalance else calc_sell_list(held_symbols, top20_set)
        n_buy = calc_buy_count(n_qual, buy_top)

        buy_candidates = top10 if force_rebalance else [s for s in top10 if s not in held_symbols]
        buy_list = buy_candidates[:n_buy]
        hold_list = [] if force_rebalance else [s for s in mid10 if s in held_symbols] + [s for s in top10 if s in held_symbols]
        strategy_name = "rebalance" if force_rebalance else "stocks"

    # 8. Determine available funds
    cash_available = last.get("cash", 0)
    for s in sell_list:
        if s in last.get("positions", {}):
            p = close[s].iloc[-1] if s in close.columns else 0
            cash_available += last["positions"][s] * p if pd.notna(p) else 0

    remaining = cash_available
    can_buy_n = 0
    for sym in buy_list:
        p = close[sym].iloc[-1] if sym in close.columns else 0
        if pd.notna(p) and p > 0:
            n = calc_shares_to_buy(p, position_budget)
            if n == 0:
                continue
            cost = n * p
            if remaining >= cost:
                remaining -= cost
                can_buy_n += 1
            else:
                break
        else:
            cost = position_budget
            if remaining >= cost:
                remaining -= cost
                can_buy_n += 1

    # 9. Build strategy JSON
    next_monday = beijing_now + timedelta(days=(7 - beijing_now.weekday()) % 7 or 7)
    date_str = beijing_now.strftime("%Y-%m-%d")
    exec_date = next_monday.strftime("%Y-%m-%d")
    risk_alert = update_risk_alert(date_str, current_val, spy_price, n_qual, dd_from_peak)

    strategy = {
        "generated_at": date_str,
        "execution_date": exec_date,
        "mode": strategy_name,
        "reason": mode_reason,
        "n_qualified": n_qual,
        "qualified": [{"symbol": q["symbol"], "ret_20d": round(q["ret_20d"] * 100, 2),
                       "close": round(q["close"], 2), "high_date": str(q["high_date"].date())}
                      for q in qualifiers],
        "top20": [q["symbol"] for q in top20],
        "buy_list": buy_list[:can_buy_n],
        "sell_list": sell_list,
        "hold_list": hold_list,
        "cash_available": round(cash_available, 2),
        "position_budget": position_budget,
        "budget_year": current_year,
        "portfolio_value": round(current_val, 2),
        "peak_value": round(peak_value, 2),
        "drawdown_pct": round(dd_from_peak * 100, 2),
        "spy_mode": spy_mode,
        "risk_alert": risk_alert,
    }

    # 10. Save
    os.makedirs(_DATA_DIR, exist_ok=True)
    strategy_path = os.path.join(_DATA_DIR, f"strategy_{date_str}.json")
    with open(strategy_path, "w") as f:
        json.dump(strategy, f, indent=2, default=str)
    print(f"  ✓ Strategy saved -> {strategy_path}")

    # 11. TG push
    msg = _format_strategy_msg(strategy, name_map)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if chat_id:
        from telegram import Bot
        from telegram.error import TelegramError
        import asyncio
        bot = Bot(token=token)
        try:
            asyncio.run(bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"))
            print(f"  ✓ Strategy sent to Telegram")
        except TelegramError as e:
            print(f"  ✗ Telegram send failed: {e}")

    print(f"{'=' * 60}\n")
    return strategy


def _format_strategy_msg(strategy: dict, name_map: dict) -> str:
    emoji = "🟢" if strategy["mode"] == "stocks" else ("🔵" if strategy["mode"] == "spy" else "🟡")
    lines = [
        f"{emoji} <b>周策略 {strategy['generated_at']}</b>",
        f"📅 执行日: {strategy['execution_date']}",
        f"💰 组合: ${strategy['portfolio_value']:,.0f}  (回撤 {strategy['drawdown_pct']:+.2f}%)",
        f"📊 模式: <b>{strategy['mode'].upper()}</b>  — {strategy['reason']}",
        f"🎯 合格数: {strategy['n_qualified']}",
    ]

    alert = strategy.get("risk_alert", {})
    if alert:
        icon = "⚠️" if alert.get("enabled") else "✅"
        lines.append(f"{icon} 风险: {alert.get('message', 'N/A')}")
        if alert.get("relative_strength") is not None:
            lines.append(
                f"   RS {alert['relative_strength']:.4f} / MA13 {alert['relative_strength_ma13']:.4f}; "
                f"13周 策略 {alert['strategy_13w_pct']:+.2f}% vs SPY {alert['spy_13w_pct']:+.2f}%"
            )
    lines.append("")

    if strategy["buy_list"]:
        lines.append("📗 <b>买入</b>")
        for s in strategy["buy_list"]:
            name = name_map.get(s, s)
            lines.append(f"  • {s}  {name}")
        lines.append("")

    if strategy["hold_list"]:
        lines.append("📘 <b>持有</b>")
        for s in strategy["hold_list"]:
            name = name_map.get(s, s)
            lines.append(f"  • {s}  {name}")
        lines.append("")

    if strategy["sell_list"]:
        lines.append("📕 <b>卖出</b>")
        for s in strategy["sell_list"]:
            name = name_map.get(s, s)
            lines.append(f"  • {s}  {name}")
        lines.append("")

    if not strategy["sell_list"] and not strategy["buy_list"]:
        lines.append("  ⏸️ 无操作")
        lines.append("")

    lines.append("🤖 #stock-radar-strategy")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
