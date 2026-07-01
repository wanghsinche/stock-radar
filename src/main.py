#!/usr/bin/env python3
"""
SP500 Top 20 强势股票雷达 — 入口
"""

import os
import sys

import yaml
from dotenv import load_dotenv


def load_config():
    path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    load_dotenv()

    from src.notifier import send_telegram
    from src.scanner import scan_top_strong

    print(f"\n{'=' * 60}")
    print(f"  📡 SP500 Top 20 强势股票雷达")
    print(f"{'=' * 60}")

    config = load_config()
    top_n = config.get("radar", {}).get("top_n", 20)

    df = scan_top_strong(top_n=top_n)

    print(f"\n{'=' * 60}")
    print(f"  📤 Sending to Telegram...")
    send_telegram(df, config)
    print(f"{'=' * 60}\n")

    return df


if __name__ == "__main__":
    main()
