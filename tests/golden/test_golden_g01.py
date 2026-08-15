# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:12:00
# @description : g01 空仓策略：3 标的 × 250 日合成数据，不下单 → 六要素全零/全平

"""黄金用例 g01：空仓策略（测试方案 §5 g01）。

场景：3 标的 × 250 交易日正常价格（gdriver 1e6 初始资金、默认费率）；
策略仅 initialize，不下单不调度。

断言（4.9.2 六要素，FILL_TOL=1e-10）：
- 订单/成交/现金流水 全空；费用全 0；
- 现金恒=初始资金（逐日 nav=1.0）；
- daily_nav 行数=交易日数（250）；status=completed_exact（无降级）。
"""

from __future__ import annotations

from .conftest import flat_series
from .framework import assert_six

N_DAYS = 250
CODES = ("600000.SH", "510300.SH", "159915.SZ")


def test_g01_empty_strategy(gdriver) -> None:
    """3×250 空间曲线：无订单无成交无费用，nav 全 1.0。"""
    gdriver.add_data({c: flat_series(c, N_DAYS) for c in CODES})
    snap = gdriver.run()

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
    assert snap.degradations == []


def test_g01_no_positions_no_cash_moves(gdriver) -> None:
    """现金四分类恒等：available=1e6 且 receivable/frozen 无残留。"""
    gdriver.add_data({c: flat_series(c, N_DAYS) for c in CODES})
    snap = gdriver.run()

    assert snap.cash.available == []
    assert snap.positions == {}
    assert gdriver.account.available_cash == 1_000_000.0
    assert gdriver.account.receivable_cash == 0.0
    assert gdriver.account.frozen_cash == 0.0
    gdriver.account.assert_invariant()
