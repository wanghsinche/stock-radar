"""
pm2 wrapper — 自适应 DST + 假日检查 + 单实例锁
ecosystem 设 Mon 13:30 UTC (21:30 北京)
- 如果还没到 09:30 ET → sleep
- 如果是假日 → 跳过 + TG 通知
- 单实例锁防止 pm2 cron 重复触发导致重复下单
"""

import fcntl
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Add project root
sys.path.insert(0, os.path.dirname(__file__))

from src.holiday import is_us_market_holiday, sleep_until_et

_LOCK_PATH = "/tmp/stock-radar-exec.lock"


def acquire_lock():
    """Acquire a non-blocking exclusive lock. Returns the file handle or None."""
    lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def main():
    print(f"\n{'=' * 50}")
    print(f"  🔄 exec_entry — {datetime.now().isoformat()}")
    print(f"{'=' * 50}")

    lock = acquire_lock()
    if lock is None:
        print(f"  ⏭️ Another exec-trade already running (lock {_LOCK_PATH}), exiting")
        return

    today = date.today()

    # Only run on Monday
    if today.weekday() != 0:
        print(f"  ⏭️ Today is not Monday ({today}), skipping")
        return

    # Holiday check
    if is_us_market_holiday(today):
        print(f"  ⏭️ US market holiday today ({today}), skipping")
        return

    # Wait until open (09:30 ET)
    if not sleep_until_et(9, 30, max_wait=3600):
        print(f"  ⏭️ Too far from US open, cron will retry next week")
        return

    # Execute (places orders at open, then sleeps until 09:40 ET inside to
    # let market orders fill before syncing real positions)
    from src.executor import main as executor_main
    executor_main()


if __name__ == "__main__":
    main()
