from calendar import monthcalendar, MONDAY, THURSDAY
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """nth occurrence of weekday in month (1-indexed)."""
    cal = monthcalendar(year, month)
    days = [w[weekday] for w in cal if w[weekday] != 0]
    return date(year, month, days[nth - 1])


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cal = monthcalendar(year, month)
    days = [w[weekday] for w in cal if w[weekday] != 0]
    return date(year, month, days[-1])


def _good_friday(year: int) -> date:
    """Computus — Anonymous Gregorian algorithm."""
    y = year
    a = y % 19
    b = y // 100
    c = y % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month_num = (h + l - 7 * m + 114) // 31
    day_num = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month_num, day_num)
    return easter - timedelta(days=2)


def _observed(d: date) -> date:
    """NYSE observed holiday: if Saturday → Friday before, if Sunday → Monday after."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _fixed_date(year: int, month: int, day: int) -> date:
    return _observed(date(year, month, day))


_NYSE_HOLIDAYS_CACHE: dict[int, set[date]] = {}


def _nyse_holidays(year: int) -> set[date]:
    if year in _NYSE_HOLIDAYS_CACHE:
        return _NYSE_HOLIDAYS_CACHE[year]

    h = {
        _fixed_date(year, 1, 1),  # New Year's
        _nth_weekday(year, 1, MONDAY, 3),  # MLK Day
        _nth_weekday(year, 2, MONDAY, 3),  # Presidents' Day
        _good_friday(year),  # Good Friday
        _last_weekday(year, 5, MONDAY),  # Memorial Day
        _fixed_date(year, 6, 19),  # Juneteenth
        _fixed_date(year, 7, 4),  # Independence Day
        _nth_weekday(year, 9, MONDAY, 1),  # Labor Day
        _nth_weekday(year, 11, THURSDAY, 4),  # Thanksgiving
        _fixed_date(year, 12, 25),  # Christmas
    }

    _NYSE_HOLIDAYS_CACHE[year] = h
    return h


def is_us_market_holiday(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    return d in _nyse_holidays(d.year)


def next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while is_us_market_holiday(d):
        d += timedelta(days=1)
    return d


def us_open_time_beijing(query_date: date = None) -> datetime:
    """Return Beijing datetime for 09:30 ET on query_date."""
    if query_date is None:
        query_date = date.today()
    et = ZoneInfo("America/New_York")
    bj = ZoneInfo("Asia/Shanghai")
    # 09:30 ET on query_date
    open_dt = datetime(query_date.year, query_date.month, query_date.day, 9, 30, tzinfo=et)
    return open_dt.astimezone(bj)


def is_us_market_open_today() -> bool:
    """Check if today is a trading day at 09:30 ET."""
    today = date.today()
    if is_us_market_holiday(today):
        return False
    # Must be within trading hours — we check between 09:00 and 16:00 ET
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now_et <= close_time
