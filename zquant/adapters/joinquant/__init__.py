# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 21:33:45
# @update_time        : 2026/08/16 13:02:00
# @description : 聚宽适配器(设计4.6)：官方策略零改动回测; 导入本包即注册 joinquant

"""聚宽适配器(设计4.6)——注入命名空间 + 数据族 + 下单配置族 + 调度族。

导入本包即注册 'joinquant' 到 AdapterRegistry（4.3 一行注册）。
"""

from __future__ import annotations

from zquant.adapters.joinquant.adapter import JoinQuantAdapter

__all__ = ["JoinQuantAdapter"]
