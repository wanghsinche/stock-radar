"""
pm2 wrapper — 自适应 DST + 假日检查
ecosystem 设 Mon 13:30 UTC (21:30 北京)
- 如果还没到 09:30 ET → sleep
- 如果是假日 → 跳过 + TG 通知
"""

import time
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Add project root
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.holiday import is_us_market_holiday, us_open_time_beijing


def wait_until_open():
    """Sleep until 09:30 ET today."""
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    open_bj = us_open_time_beijing(date.today())

    if now_bj < open_bj:
        wait_sec = (open_bj - now_bj).total_seconds()
        print(f"  ⏳ Waiting {wait_sec:.0f}s until US open (09:30 ET = {open_bj.strftime('%H:%M')} Beijing)")
        if wait_sec > 3600:
            print(f"     Too long to wait (>1h), will exit. Cron should retry next week.")
            return False
        time.sleep(wait_sec)
    return True


def main():
    print(f"\n{'=' * 50}")
    print(f"  🔄 exec_entry — {datetime.now().isoformat()}")
    print(f"{'=' * 50}")

    today = date.today()

    # Only run on Monday
    if today.weekday() != 0:
        print(f"  ⏭️ Today is not Monday ({today}), skipping")
        return

    # Holiday check
    if is_us_market_holiday(today):
        print(f"  ⏭️ US market holiday today ({today}), skipping")
        return

    # Wait until open
    if not wait_until_open():
        print(f"  ⏭️ Exiting, cron will retry next week")
        return

    # Execute
    from src.executor import main as executor_main
    executor_main()


if __name__ == "__main__":
    main()
