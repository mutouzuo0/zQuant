# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:10:00
# @update_time        : 2026/08/16 06:48:31
# @description : F3 UnifiedBacktestEngine：日内十阶段主循环（设计 5.1/6.4）

"""UnifiedBacktestEngine（设计 5.1）——统一回测主循环。

日内十阶段严格顺序（每交易日）:
  ① SessionStart        账户/日状态就绪, ControlSignal 检查（pause/stop）
  ② 公司行为开盘前生效    送转/拆股改数量稀释成本; 现金分红计 receivable（3.14）
  ③ T+1 释放             昨日买入转可卖（settle_day）
  ④ before_open 调度     盘前回调（当日 bar 不可见）
  ⑤ 开盘撮合             处理各标的当日 bar: BrokerSim + 成交入账（阶段⑥回调已含⑤）
  ⑥ 策略回调             日线 15:00（handle_data 语义; 账户已含⑤成交, 防重复下单）
  ⑦ 盘中                （日线无）
  ⑧ on_daily_close 调度  日线降级折叠点（strict_schedule 校验在 Scheduler）
  ⑨ 估值 mark_to_market  raw_close 估值 + DailyNav（停牌/退市 stale_price 标记）
  ⑩ 分红到账             pay_date: receivable → available

终态: completed_exact / completed_degraded / stopped / error。
控制: 每 bar 边界检查 ControlSignal（pause gate 挂起、stop 终止）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Protocol

from zquant.core.errors import ZQuantError
from zquant.engine.broker import BrokerSim, MatchOutcome
from zquant.engine.models.bar import MinimalBar
from zquant.engine.orderbook import OpenOrderBook

_OPEN: time = time(9, 30)
_CLOSE: time = time(15, 0)


@dataclass
class StageTrace:
    """阶段顺序追踪（T-E04 断言: 每交易日应完整命中 ①-⑩ 且顺序正确）。"""

    order: list[str] = field(default_factory=list)

    def hit(self, stage: str) -> None:
        self.order.append(stage)


@dataclass
class ControlSignal:
    """回测控制（6.4: 暂停门 / 停止旗）。"""

    pause_requested: bool = False
    stop_requested: bool = False
    gate_open: bool = True  # pause 挂起时 False, 主循环原地等待（M4 语义占位）


class SessionPort(Protocol):
    """引擎 → 会话的依赖口（数据/账户/回调, F5 BacktestSession 实现）。"""

    def trading_days(self) -> list[datetime]: ...
    def bar_at(self, code: str, dt: datetime) -> MinimalBar | None: ...
    def apply_open_actions(self, dt: datetime) -> list[str]: ...  # 阶段②: 返回公司行为说明
    def release_t1(self) -> None: ...  # 阶段③
    def run_before_open(self, dt: datetime) -> None: ...  # 阶段④
    def run_strategy(self, dt: datetime) -> list[Any]: ...  # 阶段⑥: 返回 Order 列表
    def run_on_close(self, dt: datetime) -> None: ...  # 阶段⑧
    def mark_to_market(self, dt: datetime) -> Any: ...  # 阶段⑨: 返回 NavPoint
    def settle_dividends(self) -> None: ...  # 阶段⑩
    def orders_to_book(self, orders: list[Any]) -> None: ...  # 策略产出 → 账本受理
    def profile_of(self, code: str) -> Any: ...  # InstrumentProfile
    def universe(self) -> list[str]: ...
    def available_cash(self) -> float: ...
    def record_event(self, ev: Any) -> None: ...  # 订单事件入流水
    def account_apply_fill(self, fill: Any) -> None: ...  # 成交入账（含费用）
    def finalize(self) -> Any: ...  # 返回结果快照


class UnifiedBacktestEngine:
    """统一回测引擎（设计 5.1 十阶段）。"""

    def __init__(self, session: SessionPort, *, broker: BrokerSim | None = None) -> None:
        self.session = session
        self.broker = broker or BrokerSim()
        # 待撮合账本与会话共享同一实例（orders_to_book 受理 → 此处撮合, 5.3.1）;
        # 无 order_book 的桩会话（T-E04）回退到引擎自有账本。
        self.order_book: Any = getattr(session, "order_book", None)
        if self.order_book is None:
            self.order_book = OpenOrderBook()
        self.control = ControlSignal()
        self.trace = StageTrace()
        self.degradations: list[str] = []
        self.status = "completed_exact"
        self._navs: list[Any] = []

    # ------------------------------------------------------------------
    def run(self) -> Any:
        """跑完整回测; 返回会话的最终快照。"""
        try:
            for dt in self.session.trading_days():
                if self.control.stop_requested:
                    self.status = "stopped"
                    break
                self._run_day(dt)
        except ZQuantError:
            self.status = "error"
            raise
        if self.status == "completed_exact" and self.degradations:
            self.status = "completed_degraded"
        return self.session.finalize()

    def _run_day(self, dt: datetime) -> None:
        self.trace.hit("session_start")  # ①
        if not self.control.gate_open:
            self.trace.hit("paused_gate")  # M4 挂起占位
            return
        # ② 公司行为开盘前生效（送转/拆股改数量, 分红计应收, 3.14）
        self.trace.hit("corp_open")
        self.session.apply_open_actions(dt)
        # ③ T+1 释放（昨日买入转可卖）
        self.trace.hit("t1_release")
        self.session.release_t1()
        # ④ before_open 调度（当日 bar 不可见）
        self.trace.hit("before_open")
        self.session.run_before_open(dt)
        # ⑤ 开盘撮合: 各标的当日 bar → BrokerSim → 成交入账
        self.trace.hit("open_match")
        self._match_open(dt)
        # ⑥ 策略回调（日线 15:00, 账户已含⑤成交）
        self.trace.hit("strategy")
        orders = self.session.run_strategy(dt)
        if orders:
            self.session.orders_to_book(orders)
        # ⑦ 盘中（日线无, 占位）
        self.trace.hit("intraday_none")
        # ⑧ on_daily_close 调度（日线降级折叠点）
        self.trace.hit("on_close")
        self.session.run_on_close(dt)
        # ⑨ 估值 mark_to_market（raw_close）+ DailyNav
        self.trace.hit("mark_to_market")
        nav = self.session.mark_to_market(dt)
        self._navs.append(nav)
        # ⑩ 分红到账（pay_date: receivable → available）
        self.trace.hit("dividend_settle")
        self.session.settle_dividends()

    # ------------------------------------------------------------------
    def _match_open(self, dt: datetime) -> None:
        for code in sorted(self.session.universe()):
            bar = self.session.bar_at(code, dt)
            if bar is None:
                continue
            profile = self.session.profile_of(code)
            outcomes = self.broker.process_orders(self.order_book, bar, profile)
            for oc in outcomes:
                self._apply_outcome(oc)

    def _apply_outcome(self, oc: MatchOutcome) -> None:
        if oc.one_word_board:
            self.degradations.append(f"{oc.order.order_id} @one_word: {oc.order.code} 一字板未成交")
        for ev in oc.events:
            self.session.record_event(ev)
        if oc.fill is not None:
            self.session.account_apply_fill(oc.fill)
        self.order_book.drop_order(oc.order.order_id)

    @property
    def daily_nav_rows(self) -> int:
        return len(self._navs)
