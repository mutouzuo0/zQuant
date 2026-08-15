# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:33:45
# @description : pytest 全局 fixture（测试方案 §1：seed 固定、临时目录、settings 工厂）

"""pytest 全局 fixture（测试方案 §1：seed 固定、临时目录、settings 工厂）。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

# 仓库根目录（供测试引用已入库的模板/夹具文件）
REPO_ROOT = Path(__file__).resolve().parents[1]

# 确定性纪律（设计 8.8）：所有测试在固定 seed 下运行
RANDOM_SEED = 42


@pytest.fixture(autouse=True)
def _seed_random() -> None:
    """每个用例开始前重置随机种子——引擎内禁止未播种随机。"""
    random.seed(RANDOM_SEED)


@pytest.fixture()
def example_settings_path() -> Path:
    """入库的配置模板（内容稳定，测试可依赖）。"""
    return REPO_ROOT / "config" / "settings.example.json"
