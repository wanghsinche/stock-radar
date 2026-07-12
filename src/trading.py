"""
共享交易逻辑 — 减仓判断、SPY切换等
backtest 和 strategy 共用此模块，避免逻辑分叉
"""


def calc_buy_count(n_qual: int, buy_top: int = 10, min_qualify_half: int = 15) -> int:
    """根据合格股票数量决定买多少只。

    规则:
        - n_qual < min_qualify_half (15) → 不买
        - n_qual >= min_qualify_half → 买 buy_top (10) 只
    """
    if n_qual < min_qualify_half:
        return 0
    return buy_top
