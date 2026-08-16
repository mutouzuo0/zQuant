# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:16:00
# @update_time        : 2026/08/16 12:16:00
# @description : g01 平台版（PTrade）：空仓策略——数据/oracle 与 native 同源, 策略 ptrade 化

"""g01 平台版（PTrade, D3 纪律）。

数据构造与六要素 oracle 与 native g01 完全同源（conftest.flat_series +
framework.assert_six, 断言值手算不变）; 仅策略脚本换 ptrade 官方写法:
set_universe + handle_data 空跑。
"""

from __future__ import annotations

from tests.golden.conftest import flat_series  # noqa: F401  (fixture 复用)
from tests.golden.framework import assert_six

from .bridge import run_ptrade_golden

N_DAYS = 250
CODES = ("600000.SH", "510300.SH", "159915.SZ")

PTRADE_G01 = """\
def initialize(context):
    set_universe(['600000.SS', '510300.SS', '159915.SZ'])


def handle_data(context, data):
    pass
"""


def test_g01_ptrade_empty_strategy(gdriver) -> None:
    """3×250 空间曲线（ptrade）: 无订单无成交无费用, nav 全 1.0。"""
    gdriver.add_data({c: flat_series(c, N_DAYS) for c in CODES})
    snap = run_ptrade_golden(gdriver, PTRADE_G01)

    assert len(snap.nav_series) == N_DAYS, "daily_nav 行数必须=交易日数"
    expected_nav = [{"dt": p.dt, "nav": 1.0, "equity": 1_000_000.0} for p in snap.nav_series]
    assert_six(
        snap,
        orders=[],
        fills=[],
        cash=[],
        positions={},
        fees={"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0},
        nav=expected_nav,
        status="completed_exact",
    )


def test_g01_ptrade_no_cash_moves(gdriver) -> None:
    """现金四分类恒等（ptrade 注入面无副作用）。"""
    gdriver.add_data({c: flat_series(c, N_DAYS) for c in CODES})
    run_ptrade_golden(gdriver, PTRADE_G01)

    assert gdriver.account.available_cash == 1_000_000.0
    assert gdriver.account.receivable_cash == 0.0
    assert gdriver.account.frozen_cash == 0.0
    gdriver.account.assert_invariant()
