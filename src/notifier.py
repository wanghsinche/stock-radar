"""
Telegram 推送 — 格式化 Top 20 结果并发送到 Telegram
"""

import asyncio
import os
from datetime import datetime

import yaml
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def format_message(df, title: str = None) -> str:
    date_str = datetime.today().strftime("%Y-%m-%d")
    lines = [f"📡 <b>SP500 Top 20 强势股票雷达</b>", f"📅 {date_str}", ""]

    for _, r in df.iterrows():
        bar = "🟢" if r["Return_pct"] > 0 else "🔴"
        name = r.get("Security", "")[:20]
        high_date = r.get("High_Date", "")
        lines.append(f"  #{int(r['Rank'])}  {bar} <b>{r['Symbol']}</b>  {r['Return_pct']:+6.2f}%")
        lines.append(f"      {name}  ┊ 新高{high_date}")

    lines.append("")
    lines.append("🤖 #stock-radar")
    return "\n".join(lines)


def _get_env():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def send_telegram(df, config: dict = None):
    token, chat_id = _get_env()
    if not chat_id:
        print("  ⚠️ TELEGRAM_CHAT_ID not set, skipping")
        return

    msg = format_message(df)
    bot = Bot(token=token)
    try:
        asyncio.run(bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"))
        print(f"  ✓ Telegram message sent to {chat_id}")
    except TelegramError as e:
        print(f"  ✗ Telegram send failed: {e}")


def send_photo(photo_path: str, caption: str = ""):
    token, chat_id = _get_env()
    if not chat_id:
        print("  ⚠️ TELEGRAM_CHAT_ID not set, skipping")
        return

    if not os.path.exists(photo_path):
        print(f"  ⚠️ Photo not found: {photo_path}")
        return

    bot = Bot(token=token)
    try:
        with open(photo_path, "rb") as f:
            asyncio.run(bot.send_photo(chat_id=chat_id, photo=f, caption=caption, parse_mode="HTML"))
        print(f"  ✓ Photo sent to {chat_id}")
    except TelegramError as e:
        print(f"  ✗ Photo send failed: {e}")


def main(df=None):
    if df is None:
        from src.scanner import scan_top_strong
        df = scan_top_strong()

    config = load_config()
    send_telegram(df, config)


if __name__ == "__main__":
    main()
