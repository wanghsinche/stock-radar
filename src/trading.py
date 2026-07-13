"""
共享交易逻辑 — backtest 和 strategy 共用此模块，避免逻辑分叉
所有函数为纯函数（无副作用），逻辑以 backtest 为准
"""

DEFAULT_PARAMS = {
    "buy_top": 10,
    "top_n": 20,
    "initial_cash_per_stock": 2000,
    "dd_switch_to_spy": 0.15,
    "reentry_min_qual": 30,
    "reentry_spy_ma": 50,
    "spy_cooldown_weeks": 4,
    "min_qualify_half": 15,
}


def calc_buy_count(n_qual: int, buy_top: int = 10, min_qualify_half: int = 15) -> int:
    """根据合格股票数量决定买多少只。

    规则:
        - n_qual < min_qualify_half (15) → 不买
        - n_qual >= min_qualify_half → 买 buy_top (10) 只
    """
    if n_qual < min_qualify_half:
        return 0
    return buy_top


def calc_sell_list(held_symbols: set, top20_set: set) -> list[str]:
    """返回需要卖出的股票列表：持仓中不在 top20 的股票。"""
    return sorted(held_symbols - top20_set)


def is_spy_entry_trigger(
    spy_mode: bool,
    dd_from_peak: float,
    weeks_since_stock_entry: int,
    dd_switch_to_spy: float = 0.15,
    spy_cooldown_weeks: int = 4,
) -> bool:
    """判断是否应该切换到 SPY 模式（认输）。

    条件: 非SPY模式 + 回撤超过阈值 + 冷却期已满
    """
    return (
        not spy_mode
        and dd_from_peak < -dd_switch_to_spy
        and weeks_since_stock_entry >= spy_cooldown_weeks
    )


def is_spy_exit_trigger(
    spy_mode: bool,
    n_qual: int,
    spy_price: float | None,
    spy_sma: float | None,
    reentry_min_qual: int = 30,
) -> bool:
    """判断是否应该从 SPY 模式切回股票。

    条件: SPY模式中 + 合格数达标 + SPY 站上均线
    """
    if not spy_mode:
        return False
    spy_above_ma = (
        spy_sma is not None
        and spy_price is not None
        and spy_price > spy_sma
    )
    return n_qual >= reentry_min_qual and spy_above_ma


def increment_cooldown(spy_mode: bool, weeks_since_stock_entry: int) -> int:
    """递增冷却期计数器。

    非SPY模式且 < 999 时递增，否则不变。
    """
    if not spy_mode and weeks_since_stock_entry < 999:
        return weeks_since_stock_entry + 1
    return weeks_since_stock_entry


def calc_drawdown(current_value: float, peak_value: float) -> tuple[float, float]:
    """计算回撤。返回 (更新后的 peak, drawdown)。"""
    if current_value > peak_value:
        peak_value = current_value
    dd = current_value / peak_value - 1
    return peak_value, dd


def calc_portfolio_value(
    cash: float,
    positions: dict,
    prices: dict,
    spy_mode: bool = False,
    spy_shares: float = 0,
    spy_price: float = 0,
) -> float:
    """计算组合总价值。"""
    if spy_mode:
        return cash + spy_shares * spy_price
    val = cash
    for sym, shares in positions.items():
        p = prices.get(sym)
        if p is not None and shares > 0:
            val += shares * p
    return val


def calc_shares_to_buy(price: float, budget: float = 2000) -> int:
    """根据价格和预算计算可买股数。"""
    if price <= 0:
        return 0
    return int(budget / price)
