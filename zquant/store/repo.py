# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:16:00
# @update_time        : 2026/08/16 03:16:00
# @description : G3 store/repo.py：RunRepo（run 创建/快照复用/软删除/purge）+ DetailRepo（批量插入）

"""仓储层（设计 8.3/8.7）——SQL 访问的唯一入口。

RunRepo    核心元数据: run 创建、策略快照 sha256 复用、状态更新、软删除、
           purge --force 按序物理删除（8.1 级联走 Repo）。
DetailRepo 明细批量插入（executemany, 8.7）: orders/order_events/fills/navs。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from zquant.core.errors import ZQuantError
from zquant.store.models import (
    BacktestDailyNav,
    BacktestMetrics,
    BacktestRun,
    Fill,
    Order,
    OrderEvent,
    RunManifest,
    StrategySnapshot,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class RunRepo:
    """回测运行核心元数据仓储。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------
    def get_or_create_snapshot(
        self, *, file_name: str, code_text: str, sha256: str
    ) -> tuple[StrategySnapshot, bool]:
        """策略快照: 同 sha256 复用（T-S04）, 否则新建。返回 (快照, 是否新建)。"""
        with Session(self.engine, expire_on_commit=False) as s:
            snap = s.execute(
                select(StrategySnapshot).where(StrategySnapshot.sha256 == sha256)
            ).scalar_one_or_none()
            if snap is not None:
                return snap, False
            snap = StrategySnapshot(
                file_name=file_name,
                code_text=code_text,
                sha256=sha256,
                line_count=code_text.count("\n") + 1,
            )
            s.add(snap)
            s.commit()
            s.refresh(snap)
            return snap, True

    def create_run(
        self,
        *,
        run_id: str,
        task_name: str,
        platform: str,
        snapshot_id: int,
        params_json: str,
        status: str = "running",
        manifest_hash: str | None = None,
        zquant_version: str = "",
        parent_run_id: str | None = None,
    ) -> BacktestRun:
        """创建 run（params_json 必须已脱敏, 3.6/8.3.1）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            run = BacktestRun(
                id=run_id,
                task_name=task_name,
                platform=platform,
                strategy_snapshot_id=snapshot_id,
                parent_run_id=parent_run_id,
                params_json=params_json,
                status=status,
                manifest_hash=manifest_hash,
                zquant_version=zquant_version,
            )
            s.add(run)
            s.commit()
            return run

    def update_status(self, run_id: str, status: str, *, error_log: str | None = None) -> None:
        with Session(self.engine, expire_on_commit=False) as s:
            vals: dict[str, Any] = {"status": status, "finished_at": datetime.now().astimezone()}
            if error_log is not None:
                vals["error_log"] = error_log
            s.execute(update(BacktestRun).where(BacktestRun.id == run_id).values(**vals))
            s.commit()

    def get(self, run_id: str) -> BacktestRun | None:
        with Session(self.engine, expire_on_commit=False) as s:
            return s.get(BacktestRun, run_id)

    # ------------------------------------------------------------------
    def soft_delete(self, run_id: str) -> None:
        """软删除（deleted_at 标记, 8.1 级联优先软删）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run_id)
                .values(deleted_at=datetime.now().astimezone())
            )
            s.commit()

    def purge_run(self, run_id: str, *, force: bool = False) -> int:
        """物理删除该 run 全量记录（明细→元数据; 需 force, 8.1）。"""
        if not force:
            raise ZQuantError(
                f"物理删除需 --force: {run_id}",
                stage="store",
                hint="默认软删除（soft_delete）; purge 会永久清除明细与指标",
            )
        with Session(self.engine, expire_on_commit=False) as s:
            order_ids = [
                r[0] for r in s.execute(select(Order.order_id).where(Order.run_id == run_id))
            ]
            s.execute(delete(OrderEvent).where(OrderEvent.run_id == run_id))
            s.execute(delete(Fill).where(Fill.run_id == run_id))
            s.execute(delete(Order).where(Order.run_id == run_id))
            s.execute(delete(BacktestDailyNav).where(BacktestDailyNav.run_id == run_id))
            s.execute(delete(BacktestMetrics).where(BacktestMetrics.run_id == run_id))
            s.execute(delete(RunManifest).where(RunManifest.run_id == run_id))
            run = s.get(BacktestRun, run_id)
            if run is not None:
                s.delete(run)
            s.commit()
        return len(order_ids)


class DetailRepo:
    """明细批量写入（8.7 executemany; 与 WriteBuffer 配合）。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def insert_orders(self, rows: Sequence[dict[str, Any]]) -> int:
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(Order), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_events(self, rows: Sequence[dict[str, Any]]) -> int:
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(OrderEvent), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_fills(self, rows: Sequence[dict[str, Any]]) -> int:
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(Fill), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_navs(self, rows: Sequence[dict[str, Any]]) -> int:
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(BacktestDailyNav), [dict(r) for r in rows])
            s.commit()
        return len(rows)
