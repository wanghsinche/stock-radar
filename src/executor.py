"""
周一 开盘执行:
1. 检查 US 假日
2. 加载策略 JSON
3. 初始化 Webull SDK (paper 模式跳过)
4. 获取当前持仓
5. 对比策略 → 确定实际操作
6. 先卖后买 (paper 模式只打印)
7. 保存持仓 + TG 推送
"""

import json
import os
import sys
import traceback
import uuid
from datetime import date, datetime

import yaml
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.holiday import is_us_market_holiday
from src.strategy import load_strategy, save_positions, save_trade_log, load_trade_log


def load_config():
    path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def init_webull(config: dict):
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

    wc = config["webull"]["prod"]
    api_client = ApiClient(wc["app_key"], wc["app_secret"], "sg")
    api_client.add_endpoint("sg", wc["endpoint"])
    trade_client = TradeClient(api_client)

    account_id = wc.get("account_id", "")
    if not account_id:
        res = trade_client.account_v2.get_account_list()
        if res.status_code == 200:
            accounts = res.json()
            if isinstance(accounts, list) and len(accounts) > 0:
                account_id = accounts[0]["account_id"]
                print(f"  ✓ Auto-detected account_id: {account_id}")
                _save_account_id(account_id)
            else:
                raise RuntimeError(f"No accounts found: {accounts}")
        else:
            raise RuntimeError(f"Failed to get accounts: {res.status_code} {res.text}")

    return trade_client, account_id


def _save_account_id(account_id: str):
    path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(path) as f:
        raw = f.read()
    import re
    raw = re.sub(r'account_id:\s*""', f'account_id: "{account_id}"', raw)
    with open(path, "w") as f:
        f.write(raw)


def get_usd_buying_power(trade_client, account_id: str) -> float:
    try:
        res = trade_client.account_v2.get_account_balance(account_id)
        if res.status_code != 200:
            return 0
        data = res.json()
        for a in data.get("account_currency_assets", []):
            if a.get("currency") == "USD":
                return float(a.get("buying_power", 0))
        return 0
    except Exception:
        return 0


def get_positions(trade_client, account_id: str) -> dict:
    res = trade_client.account_v2.get_account_position(account_id)
    if res.status_code != 200:
        print(f"  ⚠️ Failed to get positions: {res.status_code}")
        return {}
    data = res.json()
    positions = {}
    if isinstance(data, list):
        for pos in data:
            sym = pos.get("symbol") or pos.get("ticker", {}).get("symbol", "")
            qty_raw = pos.get("quantity", "0")
            qty = float(qty_raw) if qty_raw else 0
            if sym and qty > 0:
                positions[sym] = qty
    elif isinstance(data, dict):
        for item in data.get("data", []):
            sym = item.get("symbol") or item.get("ticker", {}).get("symbol", "")
            qty_raw = item.get("quantity", "0")
            qty = float(qty_raw) if qty_raw else 0
            if sym and qty > 0:
                positions[sym] = qty
    return positions


def place_order(trade_client, account_id: str, symbol: str, side: str, quantity: int):
    order = {
        "client_order_id": uuid.uuid4().hex,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": quantity,
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "support_trading_session": "CORE",
    }
    try:
        res = trade_client.order_v3.place_order(account_id, [order])
        if res.status_code == 200:
            return True, res.json()
        return False, {"error": f"HTTP {res.status_code}", "text": res.text}
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "error_msg") and e.error_msg:
            error_msg = e.error_msg
        elif hasattr(e, "message"):
            error_msg = e.message
        return False, {"error": error_msg, "detail": traceback.format_exc()}


def _price_from_strategy(strategy: dict, symbol: str) -> float:
    for q in strategy.get("qualified", []):
        if q["symbol"] == symbol:
            return q.get("close", 0)
    return 0


def _tg_push(msg: str):
    from telegram import Bot
    from telegram.error import TelegramError
    import asyncio
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return
    bot = Bot(token=token)
    try:
        asyncio.run(bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"))
    except TelegramError as e:
        print(f"  ✗ TG push failed: {e}")


def main():
    load_dotenv()
    config = load_config()
    wc = config.get("webull", {})
    paper = wc.get("paper_trading", True)
    today = date.today()

    print(f"\n{'=' * 60}")
    print(f"  {'🧪' if paper else '💰'} 策略执行 — {today}  ({'Paper' if paper else 'PROD'})")
    print(f"{'=' * 60}")

    # 1. Holiday check
    if is_us_market_holiday(today):
        msg = f"⏸️ <b>US 假日</b> {today}，今日跳过执行"
        print(f"  {msg}")
        _tg_push(msg)
        return

    # 2. Load strategy
    date_str = today.strftime("%Y-%m-%d")
    strategy = load_strategy(date_str)
    if strategy is None:
        strategy = load_strategy()
        if strategy is None:
            msg = f"⚠️ <b>未找到策略文件</b>，无法执行"
            print(f"  {msg}")
            _tg_push(msg)
            return
    # 无论策略从哪个文件加载，都必须校验执行日匹配
    if strategy.get("execution_date") != date_str:
        msg = (f"⚠️ <b>策略日期不匹配</b>: 策略执行日 {strategy.get('execution_date')}, "
               f"今天 {date_str}，跳过")
        print(f"  {msg}")
        _tg_push(msg)
        return

    print(f"  ✓ 加载策略: mode={strategy['mode']}, buy={len(strategy['buy_list'])}, "
          f"sell={len(strategy['sell_list'])}, hold={len(strategy['hold_list'])}")
    print(f"  📋 {strategy['reason']}")

    # 3. Get current positions
    webull_positions = {}
    if not paper:
        trade_client, account_id = init_webull(config)
        webull_positions = get_positions(trade_client, account_id)
        print(f"  ✓ Webull 持仓: {len(webull_positions)} 只")
    else:
        trade_client = account_id = None
        print(f"  📋 Paper 模式: 跳过查询持仓")

    # 4. Determine actual actions
    webull_held = set(webull_positions.keys())
    strategy_sell = set(strategy["sell_list"])
    strategy_hold = set(strategy["hold_list"])

    if strategy["mode"] == "spy":
        actual_sell = list(webull_held)
        actual_buy = ["SPY"] if "SPY" not in webull_held else []
    else:
        actual_sell = sorted(strategy_sell & webull_held)
        actual_buy = [s for s in strategy["buy_list"] if s not in webull_held]

    actual_hold = sorted(strategy_hold & webull_held)

    print(f"\n  📊 执行计划:")
    print(f"    卖出 {len(actual_sell)} 只: {', '.join(actual_sell[:10]) or '无'}")
    print(f"    买入 {len(actual_buy)} 只: {', '.join(actual_buy[:10]) or '无'}")
    print(f"    持有 {len(actual_hold)} 只: {', '.join(actual_hold[:10]) or '无'}")

    # 5. Execute
    trade_log = load_trade_log()
    results = {"bought": [], "sold": [], "failed": []}

    if paper:
        print(f"\n  🧪 Paper 模式 — 仅模拟")
        results["sold"] = list(actual_sell)
        results["bought"] = list(actual_buy)
    else:
        print(f"\n  💰 PROD 模式 — 开始下单")
        for symbol in actual_sell:
            qty = int(webull_positions.get(symbol, 0))
            if qty <= 0:
                continue
            print(f"    → 卖出 {symbol} x{qty}")
            ok, resp = place_order(trade_client, account_id, symbol, "SELL", qty)
            if ok:
                results["sold"].append(symbol)
                trade_log.append({"date": str(today), "action": "SELL", "symbol": symbol,
                                  "qty": qty, "status": "placed"})
            else:
                results["failed"].append({"symbol": symbol, "action": "SELL", "error": resp})
                print(f"    ✗ 卖出 {symbol} 失败: {resp}")

        usd_bp = get_usd_buying_power(trade_client, account_id)
        print(f"    💵 USD 购买力: ${usd_bp:,.2f}")

        initial_cash_per_stock = 2000
        for symbol in actual_buy:
            price = _price_from_strategy(strategy, symbol)
            if price <= 0:
                price = 200
            qty = int(initial_cash_per_stock / price)
            if qty == 0:
                print(f"    ⚠️ 跳过 {symbol}: ${price:.0f} > ${initial_cash_per_stock}, 买不起1股")
                results["failed"].append({"symbol": symbol, "action": "BUY",
                                          "error": "Price exceeds per-stock budget"})
                continue
            estimated = qty * price * 1.05  # 5% buffer for market order slippage
            if estimated > usd_bp:
                print(f"    ⚠️ 跳过 {symbol}: 预计 ${estimated:.0f} > 购买力 ${usd_bp:.0f}")
                results["failed"].append({"symbol": symbol, "action": "BUY",
                                          "error": "Insufficient BP"})
                continue
            print(f"    → 买入 {symbol} x{qty} (~${price:.2f})")
            ok, resp = place_order(trade_client, account_id, symbol, "BUY", qty)
            if ok:
                results["bought"].append(symbol)
                trade_log.append({"date": str(today), "action": "BUY", "symbol": symbol,
                                  "qty": qty, "status": "placed"})
            else:
                results["failed"].append({"symbol": symbol, "action": "BUY", "error": resp})
                print(f"    ✗ 买入 {symbol} 失败: {resp}")

    save_trade_log(trade_log)

    # 6. Save positions state for next strategy run
    new_positions = {}
    for s in webull_positions:
        if s not in actual_sell:
            new_positions[s] = webull_positions[s]

    actual_buy_cost = 0
    for s in actual_buy:
        price = _price_from_strategy(strategy, s)
        if price <= 0:
            price = 200
        qty = int(2000 / price)
        if qty == 0:
            continue
        new_positions[s] = qty
        actual_buy_cost += qty * price

    from src.strategy import load_last_positions
    pos_state = load_last_positions()

    try:
        if not paper:
            live_pos = get_positions(trade_client, account_id)
            if live_pos:
                pos_state["positions"] = live_pos
            else:
                # Fallback: use estimated positions
                pos_state["positions"] = new_positions
            live_bp = get_usd_buying_power(trade_client, account_id)
            if live_bp > 0:
                pos_state["cash"] = live_bp
        else:
            pos_state["positions"] = new_positions
            pos_state["cash"] = strategy.get("cash_available", 0) - actual_buy_cost
    except Exception as e:
        # Fallback on error: use estimated positions
        print(f"  ⚠️ 获取实时持仓失败 ({e})，使用估算数据")
        pos_state["positions"] = new_positions
        if paper:
            pos_state["cash"] = strategy.get("cash_available", 0) - actual_buy_cost

    pos_state["spy_mode"] = strategy.get("spy_mode", False)
    pos_state["peak_value"] = strategy.get("peak_value", 20000)
    pos_state["weeks_since_stock_entry"] = 0 if not strategy.get("spy_mode") else 999
    if not strategy.get("spy_mode"):
        old_weeks = pos_state.get("weeks_since_stock_entry", 0)
        if old_weeks < 999:
            pos_state["weeks_since_stock_entry"] = old_weeks + 1
    save_positions(pos_state)

    # 7. TG push
    emoji = "🟢" if len(results["failed"]) == 0 else "🟡"
    lines = [
        f"{emoji} <b>策略执行报告 — {today}</b>",
        f"📋 模式: {strategy['mode'].upper()}",
        f"",
    ]
    if results["bought"]:
        lines.append(f"📗 <b>已买入</b> ({len(results['bought'])}):")
        lines.append(f"  {', '.join(results['bought'])}")
        lines.append("")
    if results["sold"]:
        lines.append(f"📕 <b>已卖出</b> ({len(results['sold'])}):")
        lines.append(f"  {', '.join(results['sold'])}")
        lines.append("")
    if results["failed"]:
        lines.append(f"❌ <b>失败</b> ({len(results['failed'])}):")
        for f in results["failed"]:
            lines.append(f"  {f['symbol']} ({f['action']})")
        lines.append("")
    lines.append(f"💰 组合估值: ${strategy.get('portfolio_value', 0):,.0f}")
    lines.append("")
    if paper:
        lines.append("🧪 <i>Paper 模式 — 未下真实订单，存钱后设 paper_trading: false</i>")

    msg = "\n".join(lines)
    print(f"\n{'-' * 40}")
    print(msg)
    print(f"{'-' * 40}")
    _tg_push(msg)


if __name__ == "__main__":
    main()
