# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:40:00
# @update_time        : 2026/08/16 02:40:00
# @description : F7 ResultStore：事件日志唯一来源（event_seq 信封）+ 批量写缓冲（设计 5.6/8.7）

"""ResultStore（设计 5.6/8.7）——回测事件日志的唯一持久化来源 + 写缓冲。

  发布: 每条记录（Fill/OrderEvent/DailyNav/CorpAction/Log）→ {event_seq 单调递增,
        committed:false} 进写缓冲; M4 实时可视直接消费 event_seq 信封（6.3 不加传输层）。
  finalize: 全部记录定稿（指标等最后覆写时点, G/H 阶段接入）。
  缓冲: 三条件触发 flush（条数/时间/内存上限, 8.7 参数化）; 关键时点强制 flush;
        背压语义由调用方（引擎）实现同步阻塞（宁慢不丢）。

并发纪律: 主循环单线程写; 只追加（append-only, 8.3.7）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class StoredRecord:
    """事件日志单条（event_seq 信封, 6.3）。"""

    event_seq: int  # 单调递增（确定性, 8.8）
    kind: str  # fill / order_event / daily_nav / corp_action / log / metric
    payload: dict[str, Any]  # 业务字段
    committed: bool = False  # 是否已 flush 落库（M4/WS 可识别进度）


@dataclass
class FlushPolicy:
    """写缓冲触发条件（8.7 参数化）。"""

    batch_size: int = 500  # 条数
    flush_interval_ms: int = 500  # 时间
    buffer_max_rows: int = 50_000  # 内存上限（达此值主循环应背压等待）


class ResultStore:
    """事件日志唯一来源（append-only）。"""

    def __init__(
        self,
        policy: FlushPolicy | None = None,
        *,
        flush_hook: Callable[[list[StoredRecord]], None] | None = None,
    ) -> None:
        self.policy = policy or FlushPolicy()
        self._flush_hook = flush_hook  # 落库钩子（G 阶段 WriteBuffer/repo 接入）
        self._records: list[StoredRecord] = []
        self._seq = 0
        self._flushed: list[StoredRecord] = []

    # ------------------------------------------------------------------
    def emit(self, kind: str, payload: dict[str, Any]) -> int:
        """发布一条事件; 返回 event_seq（M4 推送给前端）。"""
        self._seq += 1
        rec = StoredRecord(event_seq=self._seq, kind=kind, payload=dict(payload))
        self._records.append(rec)
        self._maybe_flush()
        return self._seq

    def _maybe_flush(self) -> None:
        if len(self._records) >= self.policy.batch_size:
            self.flush()

    def flush(self) -> int:
        """强制刷出已发布记录（关键时点/批量满/结束时调用, 8.7）。"""
        if not self._records:
            return 0
        batch = self._records
        self._records = []
        for rec in batch:
            rec.committed = True
        if self._flush_hook is not None:
            self._flush_hook(batch)
        self._flushed.extend(batch)
        return len(batch)

    # ------------------------------------------------------------------
    def finalize(self, *, force_flush: bool = True) -> list[StoredRecord]:
        """结束回测: 强制 flush, 返回全部已定稿记录（H 阶段在此覆写 metrics）。"""
        if force_flush:
            self.flush()
        return list(self._flushed)

    @property
    def buffered_count(self) -> int:
        return len(self._records)

    @property
    def total_count(self) -> int:
        return self._seq

    def all_records(self) -> list[StoredRecord]:
        return list(self._flushed) + list(self._records)

    def next_seq(self) -> int:
        return self._seq + 1
