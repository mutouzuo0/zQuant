# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:33:45
# @description : zquant CLI（设计 10.1 三调用面之一；typer + rich）

"""zquant CLI（设计 10.1 三调用面之一；typer + rich）。

阶段 A 仅实现 `--version` 与 `config check`；其余命令为占位，
在对应里程碑（实施计划 .zcode/plans/zQuant-M0-M1-实施计划.md 的阶段）交付。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from zquant import __version__
from zquant.config import (
    DEFAULT_SECRETS_PATH,
    DEFAULT_SETTINGS_PATH,
    load_secrets,
    load_settings,
)

app = typer.Typer(
    name="zquant",
    help="zQuant 量化回测框架（本地优先 · 事件驱动 · 多平台兼容）",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# 占位命令 → 计划阶段映射（交付后移除）
_PLANNED_PHASE: dict[str, str] = {
    "run": "阶段 I（CLI）依赖阶段 F 引擎内核",
    "list": "阶段 I（依赖阶段 G 持久化）",
    "report": "阶段 H（指标与报告）",
    "replay": "阶段 I（RunManifest 与重放）",
    "fetch-etf": "阶段 E（最小 ETF 日线下载器）",
    "cache": "阶段 D（数据管道与缓存）",
}


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"zquant {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="显示版本号"
    ),
) -> None:
    """zQuant 命令行入口。"""


def _not_implemented(name: str) -> None:
    phase = _PLANNED_PHASE.get(name, "后续里程碑")
    console.print(
        f"[yellow]`zquant {name}` 尚未实现 —— 计划于 {phase} 交付（见 .zcode/plans/）。[/yellow]"
    )
    raise typer.Exit(code=2)


@app.command("config")
def config_check() -> None:
    """检查配置文件存在性与密钥配置状态（设计 3.6）。"""
    table = Table(title="zQuant 配置检查")
    table.add_column("检查项", style="cyan")
    table.add_column("状态")
    table.add_column("说明")

    ok = True
    if DEFAULT_SETTINGS_PATH.exists():
        try:
            load_settings()
            table.add_row("settings.json", "[green]✔[/green]", "加载并通过 pydantic 校验")
        except Exception as exc:  # noqa: BLE001 - 配置检查需兜底展示任何校验错误
            ok = False
            table.add_row("settings.json", "[red]✘[/red]", f"校验失败: {exc}")
    else:
        table.add_row(
            "settings.json",
            "[yellow]缺失[/yellow]",
            "使用内置默认值（可从 settings.example.json 复制）",
        )

    if DEFAULT_SECRETS_PATH.exists():
        secrets = load_secrets()
        has_token = bool(secrets.get("tushare", {}).get("token"))
        table.add_row(
            "secrets.json",
            "[green]✔[/green]" if has_token else "[yellow]✔[/yellow]",
            "tushare token 已配置" if has_token else "存在但 tushare token 为空（下载功能降级）",
        )
    else:
        table.add_row(
            "secrets.json",
            "[yellow]缺失[/yellow]",
            "无密钥配置（akshare 下载可用；tushare 备选通道不可用）",
        )

    for key in Path("config").glob("*.json") if Path("config").exists() else []:
        if key.name not in {"settings.json", "secrets.json"} and not key.name.endswith(
            "example.json"
        ):
            console.print(f"[dim]注意: config/{key.name} 非标准配置文件[/dim]")

    console.print(table)
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def run(
    config: Annotated[Path | None, typer.Option("-c", "--config", help="任务 JSON 路径")] = None,
) -> None:
    """运行一次回测（阶段 I 交付）。"""
    _not_implemented("run")


@app.command()
def list() -> None:
    """列出历史回测记录（阶段 I 交付）。"""
    _not_implemented("list")


@app.command()
def report(run_id: str = typer.Argument(..., help="回测 run_id")) -> None:
    """生成/查看回测报告（阶段 H 交付）。"""
    _not_implemented("report")


@app.command()
def replay(run_id: str = typer.Argument(..., help="回测 run_id")) -> None:
    """按 RunManifest 重放并比对差异（阶段 I 交付）。"""
    _not_implemented("replay")


@app.command("fetch-etf")
def fetch_etf(
    codes: str = typer.Option(..., "--codes", help="逗号分隔的 ETF 代码，如 510300.SH,510500.SH"),
    start: str = typer.Option(..., "--start", help="开始日期 YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="结束日期 YYYY-MM-DD"),
) -> None:
    """下载 ETF 日线数据到本地 CSV（阶段 E 交付）。"""
    _not_implemented("fetch-etf")


@app.command()
def cache(
    code: Annotated[str | None, typer.Option("--code", help="指定标的")] = None,
    all: Annotated[bool, typer.Option("--all", help="清理全部缓存")] = False,
) -> None:
    """清理 parquet 二级缓存（阶段 D 交付）。"""
    _not_implemented("cache")


if __name__ == "__main__":
    app()
