# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:45:00
# @description : 阶段 C 迷你日线驱动(DailyDriver)：调度+归一+资金/T+1 校验+RunSnapshot 生成

"""阶段 C 迷你日线驱动（DailyDriver）——测试侧引擎先导。

职责对齐设计 4.5/4.7/5.4/5.5（日线三时点、order_target_value 归一、T+1 可卖、
现金四分类、逐日净值），但**撮合结果**完全由 MockBroker 程序化裁量
（price/qty/partial/reject/one-word-board），开放匹配语义留待阶段 F BrokerSim。
阶段 F 切换真实 BrokerSim 时，golden 断言不变。

驱动规则（黄金用例语义，4.9.2）：
- 策略动作在 15:00 收盘时段回调（4.7 handle_data 挂 15:00），订单预约**下一交易日
  开盘**撮合（eligible_fill_at=开盘，4.5 target_value 语义）——成交流水记撮合时刻；
- 现金四分类恒等式每次变动后校验（5.5 不变量）；
- 费用统计算入 snap.fees 供六要素费用断言。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from zquant.core.errors import ZQuantError
from zquant.engine.account import Account, Position
from zquant.engine.orders import (
    Fill,
    Order,
    OrderDirection,
    OrderEvent,
    OrderEventType,
    OrderStatus,
    OrderStyle,
    TimeInForce,
)

from .framework import CashLedger, MockBroker, NavPoint, PositionSnapshot, RunSnapshot

__all__ = ["DayBar", "DaySession", "LotFloor", "DailyDriver"]


@dataclass(frozen=True)
class DayBar:
    """单交易日合成 bar（g01-g13 数据构造的最小结构）。"""

    dt: datetime
    date: str  # ISO: 2026-01-05
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    volume: float = 0.0
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False


@dataclass(frozen=True)
class DaySession:
    """一个交易日（三时点语义载体：开盘/收盘/清算）。"""

    bar: DayBar
    codes: tuple[str, ...]


@dataclass(frozen=True)
class LotFloor:
    """整手取整（4.5 归一：floor(q/lot)*lot，B5 LimitRule 语义）。"""

    lot: int = 100
    price_step: float = 0.01

    def floor(self, qty: float) -> float:
        return float(int(max(0.0, qty) // self.lot) * self.lot)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


class DailyDriver:
    """阶段 C 迷你日线引擎驱动（黄金用例专用，见模块 docstring）。

    语义边界：本驱动实现 调度/归一/资金与 T+1 校验/结算/估值；
    撮合结果（成交价、成交量、拒单、一字板）由 MockBroker 程序化裁量。
    """

    def __init__(
        self,
        broker: MockBroker,
        *,
        initial_cash: float = 1_000_000.0,
        initial_positions: dict[str, float] | None = None,
        commission_rate: float = 0.0001,
        commission_min: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        transfer_fee_rate: float = 0.0,
    ) -> None:
        self.broker = broker
        self.run_id = "g-golden"
        self.account = Account(
            run_id=self.run_id, initial_cash=initial_cash, available_cash=initial_cash
        )
        self.commission_rate = commission_rate
        self.commission_min = commission_min
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.sessions: list[DaySession] = []
        self.sessions_by_date: dict[str, DaySession] = {}
        self.data: dict[str, dict[str, DayBar]] = {}
        self._initial_positions = initial_positions or {}
        self._scheduled: list[tuple[str, Callable[[], None]]] = []  # (date, 15:00 action)
        self._pending: list[Order] = []  # 预约下一交易日开盘撮合
        self._orders: list[Order] = []
        self._fills: list[Fill] = []
        self._events: list[OrderEvent] = []
        self._cash = CashLedger()
        self._fees: dict[str, float] = {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0}
        self._navs: list[NavPoint] = []
        self._corp: list[tuple[str, Callable[[], None]]] = []  # (date, apply) 开盘前生效
        self._before_open: list[
            tuple[str, Callable[[], None]]
        ] = []  # g10 盘前回调（当日 bar 不可见）
        self._after_close: list[tuple[str, Callable[[], None]]] = []  # g10 盘后回调（当日已估值）
        self._universe: list[str] = []  # 动态 universe（g09）
        self._history_cache: dict[str, list[DayBar]] = {}  # 懒加载缓存（g09）
        self._load_counts: dict[str, int] = {}  # 每 code 实际加载次数（g09）
        self._phase: str = "before_open"  # 调度相位（g10：before_open 当日 bar 不可见）
        self._div_pay_dates: set[str] = set()  # 分红 pay_date（4.4 到账语义）
        self._delist_map: dict[
            str, str
        ] = {}  # code→delist_date（g12：该日仍交易，次日起无 bar/冻结估值）
        self._degradations: list[str] = []  # 降级清单（g04/g05，4.9.2）
        self._last_close_px: dict[str, float] = {}  # 各 code 最近结算收盘（基准价）
        self._order_seq = 0
        self.status = "completed_exact"
        self.days_run = 0

    # ============ 公开接口（策略脚本） ============

    def add_data(self, bars: dict[str, list[DayBar]]) -> DailyDriver:
        """装载市场数据并生成交易日历（按首个标的的日期轴）与初始持仓。"""
        if not bars:
            return self
        self.data = {code: {_iso(b.dt): b for b in clist} for code, clist in bars.items()}
        self.sessions = [DaySession(bar=b, codes=tuple(self.data)) for b in bars[next(iter(bars))]]
        self.sessions_by_date = {s.bar.date: s for s in self.sessions}
        for code, qty in self._initial_positions.items():
            if not qty:
                continue
            day0 = self.sessions[0].bar
            px = self._bar(code, day0.date).prev_close or day0.open
            self.account.positions[code] = Position(
                code=code, total_qty=qty, avg_cost=px, last_price=px
            )
        return self

    def on(self, date: str, action: Callable[[], None]) -> None:
        """在指定交易日 15:00 收盘时段执行策略动作（4.7 handle_data 挂 15:00）。"""
        self._scheduled.append((date, action))

    def on_day(self, day_index: int, action: Callable[[], None]) -> None:
        """按索引（0-based，相对首个交易日）预约 15:00 动作。"""
        if day_index < 0 or day_index >= len(self.sessions):
            raise ZQuantError(f"day_index {day_index} 超出日历", stage="golden_daily")
        self.on(self.sessions[day_index].bar.date, action)

    def on_day_open(self, date: str, action: Callable[[], None]) -> None:
        """在指定交易日开盘前生效（公司行为/除权除息，3.14 三时点）。"""
        self._corp.append((date, action))

    def on_before_open(self, date: str, action: Callable[[], None]) -> None:
        """盘前调度点（g10）：bar 对策略不可见（cutoff=昨收），账户未含当日成交。"""
        self._before_open.append((date, action))

    def on_after_close(self, date: str, action: Callable[[], None]) -> None:
        """盘后调度点（g10）：当日收盘估值后，账户与成交回报齐全。"""
        self._after_close.append((date, action))

    def set_universe(self, codes: list[str]) -> None:
        """动态 universe（g09）：登记可查询标的；未登记 code 的 history 报错。"""
        self._universe = list(dict.fromkeys(codes))

    def load_count(self, code: str) -> int:
        """懒加载发生次数（g09：A/B 不重复加载 → 二次访问不+1）。"""
        return self._load_counts.get(code, 0)

    def on_dividend_pay(self, date: str) -> None:
        """登记分红 pay_date（4.4：结算阶段⑩应收→可用，g11）。"""
        self._div_pay_dates.add(date)

    def on_delist(self, code: str, delist_date: str) -> None:
        """登记标的退市（g12）：delist_date 为最后交易日（当日仍可交易）。

        次日起无 bar/拒单/估值冻结。
        """
        self._delist_map[code] = delist_date

    def run(self) -> RunSnapshot:
        """逐日驱动（对齐 4.7 三时点 + 5.5 结算序列），返回六要素快照。

        全年日历逐日推进（4.9.2 daily_nav 行数=交易日数），
        无 15:00 回调的日子照常 开盘撮合未成交单→清算→收盘估值。
        """
        actions = dict(self._scheduled)  # date → action（每日至多一条）
        for session in self.sessions:
            self._run_day(session.bar.date, actions.get(session.bar.date))
        return self._build_snapshot()

    # ============ 内部：每日流程（4.7 三时点） ============

    def _run_day(self, date: str, action: Callable[[], None] | None) -> None:
        self.days_run += 1
        self._current_date = date
        self._phase = "before_open"  # 盘前：当日 bar 尚不可见（4.7 时点①）
        # 1) 开盘前：①公司行为生效（3.14 三时点：ex_date 开盘前）②盘前调度点（g10）
        for adate, apply in self._corp:
            if adate == date:
                apply()
        for bdate, cb in self._before_open:
            if bdate == date:
                cb()
        # 2) 开盘：撮合上一日挂单（eligible_fill_at=今日开盘，4.5）
        self._fill_pending(date)
        # 3) 15:00 收盘：策略回调（handle_data 语义；无回调则跳过）
        #    回调前刷新基准价=今日收盘已可见（每日估值基准，4.5 当前价）
        self._phase = "on_daily_close"  # 盘中起当日 bar 可见
        self._refresh_closes(date)
        if action is not None:
            action()
        # 4) 清算：交收 + T+1 移动 + 现金分红到账（4.4/5.1）
        self._sequencing(date)
        # 5) 收盘估值：算净值（4.9.2 nav 要素）；盘后调度点（g10）
        self._mark_to_market(date)
        for adate, cb in self._after_close:
            if adate == date:
                cb()

    def _fill_pending(self, date: str) -> None:
        pending, self._pending = self._pending, []
        for order in pending:
            before = len(self.broker.events)
            self.broker.submit(order)
            # 合并撮合产生的状态迁移事件（fill/partial/expire/reject）；
            # ACCEPTED 已在 _accepted 受理时刻记录（5.3.1），不重复合并。
            self._events.extend(
                ev
                for ev in self.broker.events[before:]
                if ev.event_type is not OrderEventType.ACCEPTED
            )
            self._settle_order(order, date)

    def _settle_order(self, order: Order, date: str) -> None:
        """成交流水、记账（Account apply_fill）与费用核算（8.3.3）。

        过期（一字板/停牌顺延未成交）视为可解释降级（4.9.2 g04/g05）。"""
        if order.status is OrderStatus.REJECTED:
            # 已在受理前拒绝（资金/T+1 预检），事件由 _reject 记录
            return
        if order.status is OrderStatus.EXPIRED:
            if self.status == "completed_exact":
                self.status = "completed_degraded"  # 成交未竟，运行仍完成
            self._degradations.append(
                f"{order.order_id} @{date}: expired({order.time_in_force.value}) "
                f"({order.code} limit_up/停牌未成交)"
            )
        for fill in self.broker.fills:
            if fill.order_id == order.order_id and fill not in self._fills:
                self._fills.append(self._account_fill(order, fill))

    def _sequencing(self, date: str) -> None:
        """日终清算序列（5.5）：现金分红到账（pay_date）→ T+1 今日买入量清零。"""
        if date in self._div_pay_dates:
            amount = self.account.settle_dividend()
            if amount:
                self._cash.post(_dt(date, 16, 0), amount, "dividend_settled")
        self.account.settle_day()

    def _refresh_closes(self, date: str) -> None:
        """收盘后刷新各 code 基准价（_px/_normalize 用，4.5 目标价基准）。

        停牌沿用最近有效收盘（4.6）；退市/数据缺失也沿用（4.6 g12 冻结估值）；
        同时保持持仓 last_price 的估值一致性。
        """
        closes: dict[str, float] = dict(getattr(self, "_last_close_px", {}))
        for code, series in self.data.items():
            if code in self._delist_map and date > self._delist_map[code]:
                continue  # 退市（delist_date 后无 bar）：冻结于最近有效收盘
            bar = series.get(date)
            if bar is None:
                continue  # 数据缺失（如退市后无 bar）：沿用最近有效收盘
            if bar.suspended and code in closes:
                continue  # 停牌：沿用最近有效收盘
            closes[code] = bar.close
        self._last_close_px = closes
        for code in self.data:
            pos = self.account.positions.get(code)
            if pos is not None:
                pos.last_price = closes.get(code, pos.last_price)

    def _is_delisted(self, code: str, date: str) -> bool:
        """delist_date 之后（不含当日）判定为已退市（g12：拒单/冻结估值）。"""
        return code in self._delist_map and date > self._delist_map[code]

    def _mark_to_market(self, date: str) -> None:
        """收盘估值：现金四分类 + 持仓市值（基准价已完成刷新）→ 登记净值点。

        退市/停牌标的沿用冻结价并记入 stale_codes（5.5 g12）；
        open_positions 只计仍上市（当日有 bar）的持仓标的数。
        """
        equity = (
            self.account.available_cash + self.account.receivable_cash + self.account.frozen_cash
        )
        closes = getattr(self, "_last_close_px", {})
        stale: list[str] = []
        open_pos = 0
        for code, pos in self.account.positions.items():
            pos.last_price = closes.get(code, pos.last_price)
            equity += pos.market_value
            series = self.data.get(code)
            today_bar = series.get(date) if series else None
            if today_bar is None or today_bar.suspended:
                stale.append(code)  # 估值沿用冻结/前收盘（退市或停牌）
            else:
                open_pos += 1
        nav = equity / self.account.initial_cash
        self._navs.append(
            NavPoint(
                dt=_dt(date, 15, 0),
                nav=nav,
                equity=equity,
                stale_codes=tuple(sorted(stale)),
                open_positions=open_pos,
            )
        )

    # ============ 订单归一（4.5：四种风格 → qty） ============

    def _raw_px(self, code: str, clazz: str = "close") -> float:
        """当期收盘价基准（4.5 归一的目标价基准；停牌沿用最近有效收盘 4.6）。"""
        return self._frame_px(code, "_last_close_px") if clazz == "close" else 10.0

    def _frame_px(self, code: str, attr: str = "_last_close_px") -> float:
        return getattr(self, attr, {}).get(code, 10.0)

    def _normalize(
        self,
        code: str,
        style: OrderStyle,
        *,
        quantity: float | None = None,
        value: float | None = None,
        target_quantity: float | None = None,
        target_value: float | None = None,
        qty: float | None = None,
    ) -> float:
        """归一为下单 qty（整手取整 floor(q/lot)*lot）。"""
        if style in (OrderStyle.QUANTITY, OrderStyle.MARKET):
            assert qty is not None, "QUANTITY 风格必须给 qty"
            return self._lot_floor(code, qty)
        if style is OrderStyle.VALUE:
            return self._lot_floor(code, (value or 0.0) / self._px(code))
        if style is OrderStyle.TARGET_QUANTITY:
            diff = (target_quantity or 0.0) - self._held(code)
            if diff > 0:
                return self._lot_floor(code, diff)
            return -self._lot_floor(code, abs(diff))
        if style is OrderStyle.TARGET_VALUE:
            mv = self._held(code) * self._px(code)
            diff = (target_value or 0.0) - mv
            if diff > 0:
                return self._lot_floor(code, diff / self._px(code))
            return -self._lot_floor(code, abs(diff) / self._px(code))
        raise ZQuantError(f"未支持风格 {style}", stage="golden_daily")

    def _held(self, code: str) -> float:
        pos = self.account.positions.get(code)
        return pos.total_qty if pos else 0.0

    def _lot_floor(self, code: str, raw: float) -> float:
        return float(int(max(0.0, raw) // 100.0) * 100)

    def _px(self, code: str) -> float:
        """当期成交价基准（阶段 C：最近结算收盘价，default 10.0）。"""
        return self._frame_px(code, "_last_close_px")

    # ============ 下单 API（策略脚本用，4.5 语义） ============

    def order_target_value(self, code: str, value: float) -> Order | None:
        """target_value=目标持仓市值 → 归一 qty（需增持则 BUY，需减持则 SELL）。

        差=0（目标=当前市值）时忽略——不产生订单、不产生事件（g08）。
        """
        qty = self._normalize(code, OrderStyle.TARGET_VALUE, target_value=value)
        if qty == 0:
            return None
        if qty > 0:
            return self._place(code, OrderDirection.BUY, qty, style=OrderStyle.TARGET_VALUE)
        return self._place(code, OrderDirection.SELL, abs(qty), style=OrderStyle.TARGET_VALUE)

    def order_target_quantity(self, code: str, target_qty: float) -> Order | None:
        nq = self._normalize(code, OrderStyle.TARGET_QUANTITY, target_quantity=target_qty)
        if nq == 0:
            return None  # 目标等于当前持仓（g08 同构：忽略）
        if nq > 0:
            return self._place(code, OrderDirection.BUY, nq, style=OrderStyle.TARGET_QUANTITY)
        return self._place(code, OrderDirection.SELL, abs(nq), style=OrderStyle.TARGET_QUANTITY)

    def order_value(self, code: str, value: float) -> Order:
        """value=目标下单金额（买入；value<0 表示等额卖出）。"""
        qty = self._normalize(code, OrderStyle.VALUE, value=abs(value))
        return self._place(
            code,
            OrderDirection.BUY if value >= 0 else OrderDirection.SELL,
            qty,
            style=OrderStyle.VALUE,
        )

    def order(self, code: str, direction: OrderDirection, qty: float) -> Order:
        """quantity=目标股数 → 整手取整后下单。"""
        return self._place(code, direction, self._lot_floor(code, qty), style=OrderStyle.QUANTITY)

    def history(self, code: str, days: int, *, as_of: str | None = None) -> list[DayBar]:
        """可见性查询（g09/g10）：截至 as_of（默认当前交易日收盘，3.13 PIT 语义）的 bars。

        动态 universe 纪律（g09）：未进 universe 的 code 报错；已注册 code 首次访问
        触发懒加载（load_count 计数），再次访问命中缓存不重复解析。
        """
        if code not in self.data:
            raise ZQuantError(f"{code} 无行情数据", stage="golden_daily")
        if self._universe and code not in self._universe:
            raise ZQuantError(f"{code} 不在动态 universe 中", stage="golden_daily")
        cut = as_of or self._current_date or ""
        # 盘前相位：当日 bar 不可见 → cutoff 取前一交易日（4.7 时点①/g10）
        if not as_of and self._phase == "before_open" and self._current_date:
            prev = self._prev_trade_date(self._current_date)
            cut = prev
        # 懒加载缓存：按 code 缓存全历史，count 记录实际加载次数（命中不重复）
        if code not in self._history_cache:
            self._load_counts[code] = self._load_counts.get(code, 0) + 1
            self._history_cache[code] = [
                self.data[code][s.bar.date] for s in self.sessions if s.bar.date in self.data[code]
            ]
        full = self._history_cache[code]
        if cut:
            visible = [b for b in full if b.date <= cut]
        else:
            visible = full  # run 前（无当前日期）视为全历史可见
        return visible[-days:]

    def _place(
        self, code: str, direction: OrderDirection, qty: float, *, style: OrderStyle
    ) -> Order:
        """受理前校验（4.5/5.4）：数量>0 → 未退市 → T+1 可卖 → 可用资金（预估值）。"""
        if qty <= 0:
            return self._reject(code, direction, 1.0, style, reason="empty_qty")
        if self._is_delisted(code, self._current_date):
            return self._reject(
                code, direction, qty, style, reason="suspended/delisted"
            )  # 退市后拒单（g12）
        if direction is OrderDirection.SELL:
            pos = self.account.positions.get(code)
            closeable = pos.closeable_qty if pos else 0.0
            if qty > closeable:
                return self._reject(code, direction, qty, style, reason="t_plus_sell_unavailable")
        if direction is OrderDirection.BUY:
            est = qty * self._px(code) * (1.0 + self.commission_rate)  # 含佣预估
            if est > self.account.available_cash:
                return self._reject(code, direction, qty, style, reason="insufficient_cash")
        return self._accepted(code, direction, qty, style)

    def _accepted(
        self, code: str, direction: OrderDirection, qty: float, style: OrderStyle
    ) -> Order:
        order = self._new_order(code, direction, qty, style)
        order.status = OrderStatus.PENDING
        self._events.append(
            OrderEvent(
                order_id=order.order_id,
                event_type=OrderEventType.ACCEPTED,
                event_time=self._last_action_time(code),
            )
        )
        self._pending.append(order)  # 预约下一交易日开盘撮合
        return order

    def _reject(
        self, code: str, direction: OrderDirection, qty: float, style: OrderStyle, *, reason: str
    ) -> Order:
        order = self._new_order(code, direction, qty, style)
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._events.append(
            OrderEvent(
                order_id=order.order_id,
                event_type=OrderEventType.REJECTED,
                event_time=self._last_action_time(code),
                info_json={"reason": reason},
            )
        )
        return order

    def _new_order(
        self, code: str, direction: OrderDirection, qty: float, style: OrderStyle
    ) -> Order:
        self._order_seq += 1
        oid = f"{self.run_id}-o{self._order_seq}"
        order = Order(
            order_id=oid,
            run_id=self.run_id,
            code=code,
            side=direction,
            style=style,
            qty=qty,
            order_api=style.value,
            submitted_at=self._last_action_time(code),
            eligible_fill_at=self._next_open(),
            time_in_force=TimeInForce.DAY,
        )
        self._orders.append(order)
        return order

    @property
    def order_count(self) -> int:
        """已受理/已拒订单总数（策略断言用，g08）。"""
        return len(self._orders)

    @property
    def event_count(self) -> int:
        """已产生订单事件总数（策略断言用，g08）。"""
        return len(self._events)

    def _fills_of(self, order: Order) -> list[Fill]:
        return [f for f in self._fills if f.order_id == order.order_id]

    def _account_fill(self, order: Order, fill: Fill) -> Fill:
        """成交记账（5.5 apply_fill）与费用核算（8.3.3）。

        Fill 为 frozen dataclass——费用通过 replace() 产出新实例写回快照，
        返回该实例供 _fills 记录（六要素断言以本实例为准）。
        """
        fee_comp, fee_stamp, fee_transfer = self._fees_for(fill)
        fee_fill = replace(
            fill,
            commission=fee_comp,
            stamp_tax=fee_stamp,
            transfer_fee=fee_transfer,
        )
        self._fees["commission"] += fee_comp
        self._fees["stamp_tax"] += fee_stamp
        self._fees["transfer_fee"] += fee_transfer
        self.account.apply_fill(fee_fill)
        when = fee_fill.fill_time
        if fee_fill.side is OrderDirection.BUY:
            self._cash.post(when, -(fee_fill.amount + fee_fill.total_fee), f"buy {fee_fill.code}")
        else:
            self._cash.post(when, fee_fill.amount - fee_fill.total_fee, f"sell {fee_fill.code}")
        return fee_fill

    def _fees_for(self, fill: Fill) -> tuple[float, float, float]:
        commission = max(self.commission_min, fill.amount * self.commission_rate)
        stamp = fill.amount * self.stamp_tax_rate if fill.side is OrderDirection.SELL else 0.0
        transfer = fill.amount * self.transfer_fee_rate
        return commission, stamp, transfer

    def _last_action_time(self, code: str) -> datetime:
        """策略动作时刻：当前交易日 15:00（4.7 日线挂 15:00）。"""
        if self._current_date:
            return _dt(self._current_date, 15, 0)
        return datetime(2000, 1, 1, 15, 0)

    def _next_open(self) -> datetime:
        """下一交易日开盘时刻（4.5：订单预约次日开盘撮合，g13 精确到时刻级）。

        当前交易日 15:00 挂单 → eligible_fill_at = 下一交易日 09:30。
        """
        if not self._current_date:
            return datetime(2000, 1, 1, 9, 30)  # run 前占位（不应出现在快照）
        for session in self.sessions:
            if session.bar.date > self._current_date:
                return _dt(session.bar.date, 9, 30)
        return datetime(2000, 1, 1, 9, 30)  # 末日后无下一交易日（不应出现在快照）

    # ============ 内务 ============

    def _prev_trade_date(self, date: str) -> str:
        """前一交易日（g10 盘前 cutoff 用；首日返回自身）。"""
        dates = [s.bar.date for s in self.sessions]
        try:
            i = dates.index(date)
        except ValueError:
            return date
        return dates[i - 1] if i > 0 else date

    def _bar(self, code: str, date: str) -> DayBar:
        try:
            return self.data[code][date]
        except KeyError:
            raise ZQuantError(f"缺数据 {code} @ {date}", stage="golden_daily") from None

    def _build_snapshot(self) -> RunSnapshot:
        status = self.status
        if status == "completed_exact" and self._degradations:
            status = "completed_degraded"
        return RunSnapshot(
            orders=list(self._orders),
            fills=list(self._fills),
            order_events=list(self._events),
            cash=self._cash,
            positions={
                code: PositionSnapshot(
                    code=code,
                    total_qty=p.total_qty,
                    avg_cost=p.avg_cost,
                    last_price=p.last_price,
                    market_value=p.market_value,
                )
                for code, p in self.account.positions.items()
            },
            nav_series=self._navs,
            fees=dict(self._fees),
            status=status,
            degradations=list(self._degradations),
        )

    _current_date: str = ""


def _dt(date: str, hour: int, minute: int) -> datetime:
    return datetime.strptime(date, "%Y-%m-%d").replace(hour=hour, minute=minute)
