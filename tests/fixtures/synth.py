# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 06:48:31
# @description : 测试合成数据构造：写 tushare 源格式 CSV（CsvSourceDriver 可嗅探读取, 3.5）

"""合成日线数据构造器（runner/session/replay 测试共用）。

产出 tushare 源格式（ts_code/trade_date/OHLC/vol/amount）CSV 到
data_root/kline/etf/day/{code}.csv（3.12 布局, CsvSourceDriver 可嗅探）;
价格序列默认平坦 + 可配 drift/seed（确定性 8.8）。
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

TUSHARE_COLS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")


def trade_days(start: date, n: int) -> list[date]:
    """n 个交易日（跳过周六日; 合成数据用, 节假日不处理）。"""
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def write_etf_csv(
    data_root: Path,
    code: str,
    *,
    start: str = "2020-01-02",
    n: int = 250,
    base_price: float = 10.0,
    drift: float = 0.0002,
    volatility: float = 0.005,
    seed: int = 42,
    name: str = "ETF",
) -> Path:
    """生成并落盘 tushare 源格式 ETF 日线 CSV; 返回文件路径。"""
    rng = random.Random(seed)
    start_d = date.fromisoformat(start)
    days = trade_days(start_d, n)
    rows: list[dict[str, object]] = []
    price = base_price
    for dt in days:
        ret = drift + rng.gauss(0, volatility)
        close = max(0.1, price * (1 + ret))
        open_ = price * (1 + rng.gauss(0, volatility) * 0.3)
        high = max(open_, close) * (1 + abs(rng.gauss(0, volatility) * 0.2))
        low = min(open_, close) * (1 - abs(rng.gauss(0, volatility) * 0.2))
        vol = int(rng.uniform(5e6, 5e7))
        amount = round(vol * close, 2)
        rows.append(
            {
                "ts_code": code,
                "trade_date": dt.strftime("%Y%m%d"),
                "open": round(open_, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": vol,
                "amount": amount,
            }
        )
        price = close
    df = pd.DataFrame(rows, columns=TUSHARE_COLS)
    path = data_root / "kline" / "etf" / "day" / f"{code}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def flat_etf_csv(data_root: Path, code: str, *, n: int = 120, price: float = 10.0) -> Path:
    """平坦价格序列（价格恒定 → 净值可手算, 确定性断言）。"""
    start_d = date(2020, 1, 2)
    days = trade_days(start_d, n)
    df = pd.DataFrame(
        [
            {
                "ts_code": code,
                "trade_date": d.strftime("%Y%m%d"),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "vol": 10_000_000,
                "amount": round(price * 10_000_000, 2),
            }
            for d in days
        ],
        columns=TUSHARE_COLS,
    )
    path = data_root / "kline" / "etf" / "day" / f"{code}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path
