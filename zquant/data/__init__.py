# coding:utf-8
# @author            : 木头左
# @create_time       : 2026/08/16 00:44:00
# @update_time       : 2026/08/16 00:46:00
# @description       : 数据层统一出口：SourceDriver→DataNormalizer→MarketDataProvider 三段式管道（设计 3）

"""数据层统一出口（设计 3）：三段式管道 SourceDriver→DataNormalizer→MarketDataProvider。

导入本包即注册 data.driver，数据/缓存/日历/主数据等组件均可从本包直接引用。
import-linter 契约: data 层禁止 import zquant.engine（品种档案由调用侧注入）。
"""

from __future__ import annotations

from zquant.data.cache import CacheStats, DataCache, cache_key
from zquant.data.calendar import TradeCalendar
from zquant.data.drivers import CsvSourceDriver, SourceDriver, create_driver, register_driver
from zquant.data.master import InstrumentRow, MasterStore
from zquant.data.normalizer import DataNormalizer, LimitMapProvider, to_ms_index
from zquant.data.provider import BAR_DTYPE, Bar, MarketDataProvider

__all__ = [
    "BAR_DTYPE",
    "Bar",
    "CacheStats",
    "CsvSourceDriver",
    "DataCache",
    "DataNormalizer",
    "InstrumentRow",
    "LimitMapProvider",
    "MarketDataProvider",
    "MasterStore",
    "SourceDriver",
    "TradeCalendar",
    "cache_key",
    "create_driver",
    "register_driver",
    "to_ms_index",
]