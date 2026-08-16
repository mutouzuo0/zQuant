# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 11:00:00
# @update_time        : 2026/08/16 09:16:00
# @description : I1 zquant CLI：run/list/report/replay/config/cache/fetch-etf/validate/serve

"""zquant CLI（设计 10.1 三调用面之一; typer + rich, --json 机读）。

命令:
  run -c task.json [--isolate] [--json]      执行回测（阶段 I: 会话+引擎+导出+入库）
  list [--sort sharpe] [--json]              列出历史 run（DB）
  report <run_id> [--out dir]                生成 report.html（9.2）
  replay <run_id> [--json]                   确定性重放 + 差异报告（8.8）
  validate -c task.json                      任务配置校验（3.6 / JSON Schema）
  config check                               配置存在性/密钥状态
  cache clean [--code X] [--all]             清理 parquet 二级缓存（3.7）
  fetch-etf --codes .. --start .. --end ..   下载 ETF 日线（3.9 裁剪版, 可 --demo）
  serve [--with-task task.json]              本地浏览器监控回测（M2-W0, WS 事件流）

结构化异常 → 彩色输出 + 机读（--json 时输出 error 对象, 退出码 1）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from zquant import __version__
from zquant.config import (
    DEFAULT_SECRETS_PATH,
    DEFAULT_SETTINGS_PATH,
    load_secrets,
    load_settings,
)
from zquant.core.errors import ZQuantError
from zquant.engine.replay import replay_run
from zquant.engine.report import render_report
from zquant.engine.runner import run_task
from zquant.engine.session import TaskConfig
from zquant.store.models import init_db
from zquant.store.repo import RunRepo
from zquant.worker.isolate import (
    DEFAULT_TIMEOUT_SECONDS,
    isolate_python_command,
    run_isolated,
)

app = typer.Typer(
    name="zquant",
    help="zQuant 量化回测框架（本地优先 · 事件驱动 · 多平台兼容）",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _print_json(obj: Any) -> None:
    """机读输出（--json）: 不经 rich 换行/标记, 保证 JSON 可解析（T-I01 契约）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ============================================================
# 异常输出（结构化 → 彩色 / 机读, 10.5）
# ============================================================
def _emit_error(exc: Exception, *, json_out: bool) -> None:
    if json_out:
        _print_json({"error": _exc_dict(exc)})
    else:
        if isinstance(exc, ZQuantError):
            console.print(f"[red]✘ {exc.message}[/red]")
            if exc.stage:
                console.print(f"  阶段: [yellow]{exc.stage}[/yellow]")
            if exc.hint:
                console.print(f"  建议: [cyan]{exc.hint}[/cyan]")
            if exc.run_id:
                console.print(f"  run_id: [cyan]{exc.run_id}[/cyan]")
        elif isinstance(exc, ValidationError):
            console.print("[red]✘ 任务配置校验失败:[/red]")
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                console.print(f"  [yellow]{loc}[/yellow]: {err['msg']}")
        else:
            console.print(f"[red]✘ {type(exc).__name__}: {exc}[/red]")
    raise typer.Exit(code=1)


def _exc_dict(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ZQuantError):
        return exc.to_dict()
    if isinstance(exc, ValidationError):
        return {
            "type": "ValidationError",
            "message": str(exc),
            "errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        }
    return {"type": type(exc).__name__, "message": str(exc)}


def _load_task(path: str | None, *, json_out: bool) -> TaskConfig:
    if path is None:
        raise typer.BadParameter("缺少 -c/--config（任务 JSON 路径）")
    p = Path(path)
    if not p.is_file():
        _emit_error(
            ZQuantError(
                f"任务文件不存在: {p}",
                stage="cli",
                hint="检查路径; 示例见 configs/demo_dual_ma.json",
            ),
            json_out=json_out,
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return TaskConfig.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        _emit_error(exc, json_out=json_out)
    raise AssertionError("unreachable")  # pragma: no cover


# ============================================================
# 回调
# ============================================================
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


# ============================================================
# config check（设计 3.6）
# ============================================================
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


# ============================================================
# run（阶段 I: 会话+引擎+导出+入库; --isolate 子进程隔离）
# ============================================================
@app.command()
def run(
    config: Annotated[str | None, typer.Option("-c", "--config", help="任务 JSON 路径")] = None,
    isolate: Annotated[bool, typer.Option("--isolate", help="subprocess 隔离执行（2.4）")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="隔离超时秒数（默认 1h）")
    ] = None,
) -> None:
    """运行一次回测（会话装配 → 引擎驱动 → 导出 → DB 入库）。"""
    if isolate:
        _run_isolated(config, json_out=json_out, timeout=timeout)
        return
    try:
        task = _load_task(config, json_out=json_out)
        settings = load_settings()
        result = run_task(task, settings=settings)
    except (typer.Exit, typer.BadParameter):
        raise  # _load_task/_emit_error 已输出结构化错误并置退出码
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)

    if json_out:
        summary = {
            "run_id": result.run_id,
            "status": result.status,
            "manifest_hash": result.manifest_hash,
            "out_dir": str(result.out_dir),
            "nav_points": len(result.bundle.navs),
            "orders": len(result.bundle.orders),
            "fills": len(result.bundle.fills),
            "fees": result.bundle.fees,
            "degradations": result.bundle.degradations,
            "error_log": result.error_log,
        }
        _print_json(summary)
    else:
        _print_run_summary(result)


def _run_isolated(config: str | None, *, json_out: bool, timeout: float | None) -> None:
    """--isolate: 净化环境子进程重跑（T-X03）; 超时进程树终止。"""
    try:
        args = ["run", "--json"]
        if config:
            args += ["-c", config]
        wr = run_isolated(
            isolate_python_command(args),
            timeout_seconds=timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
        )
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    if json_out:
        payload: dict[str, Any] = {
            "isolated": True,
            "timed_out": wr.timed_out,
            "returncode": wr.returncode,
        }
        if wr.stdout.strip():
            try:
                payload["run"] = json.loads(wr.stdout)
            except json.JSONDecodeError:
                payload["stdout"] = wr.stdout
        if wr.stderr.strip():
            payload["stderr"] = wr.stderr
        _print_json(payload)
    else:
        if wr.timed_out:
            console.print(f"[red]✘ 隔离运行超时（returncode={wr.returncode}）[/red]")
        elif wr.returncode != 0:
            console.print(f"[red]✘ 隔离运行失败（returncode={wr.returncode}）[/red]")
        if wr.stdout.strip():
            console.print(wr.stdout)
        if wr.stderr.strip():
            console.print(wr.stderr, style="yellow")
    raise typer.Exit(code=1 if (wr.timed_out or wr.returncode != 0) else 0)


def _print_run_summary(result: Any) -> None:
    table = Table(title=f"回测完成 · {result.run_id}")
    table.add_column("项")
    table.add_column("值")
    table.add_row("状态", f"[green]{result.status}[/green]")
    table.add_row("manifest_hash", result.manifest_hash[:16] + "…")
    table.add_row("净值点数", str(len(result.bundle.navs)))
    table.add_row("订单 / 成交", f"{len(result.bundle.orders)} / {len(result.bundle.fills)}")
    table.add_row(
        "费用",
        f"佣金 {result.bundle.fees.get('commission', 0):.2f} · "
        f"印花税 {result.bundle.fees.get('stamp_tax', 0):.2f}",
    )
    table.add_row("导出目录", str(result.out_dir))
    if result.bundle.degradations:
        table.add_row("降级", f"[yellow]{len(result.bundle.degradations)} 条[/yellow]")
    if result.error_log:
        table.add_row("入库错误", f"[red]{result.error_log}[/red]")
    console.print(table)


# ============================================================
# list（DB 读取, 8.3.1）
# ============================================================
@app.command("list")
def list_runs(
    sort: Annotated[str, typer.Option("--sort", help="排序: started_at|sharpe")] = "started_at",
    limit: Annotated[int, typer.Option("--limit", help="条数上限")] = 50,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """列出历史回测记录（DB, 排除软删除）。"""
    try:
        settings = load_settings()
        repo = RunRepo(init_db(settings.database.url))
        runs = repo.list_runs(sort_by=sort, limit=limit)
    except (typer.Exit, typer.BadParameter):
        raise
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)

    def _iso(dt: Any) -> str:
        return dt.isoformat(timespec="seconds") if dt else ""

    if json_out:
        rows = [
            {
                **r,
                "started_at": _iso(r.get("started_at")),
                "finished_at": _iso(r.get("finished_at")),
            }
            for r in runs
        ]
        _print_json(rows)
        return
    table = Table(title="历史回测")
    table.add_column("run_id")
    table.add_column("任务")
    table.add_column("状态")
    table.add_column("夏普")
    table.add_column("开始时间")
    for r in runs:
        status_mark = (
            f"[green]{r['status']}[/green]"
            if "completed" in r["status"]
            else f"[red]{r['status']}[/red]"
        )
        table.add_row(
            r["run_id"],
            r["task_name"],
            status_mark,
            f"{r['sharpe']:.3f}" if r.get("sharpe") is not None else "—",
            _iso(r.get("started_at")),
        )
    console.print(table)


# ============================================================
# report（9.2）
# ============================================================
@app.command()
def report(
    run_id: str = typer.Argument(..., help="回测 run_id"),
    out: Annotated[
        str | None, typer.Option("--out", help="输出路径（默认 results/<run_id>/report.html）")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """生成自包含 report.html（指标卡/语义保真/净值+回撤 SVG/成交表）。"""
    try:
        path = render_report(run_id, out_path=out)
    except (typer.Exit, typer.BadParameter):
        raise
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)
    if json_out:
        _print_json({"run_id": run_id, "report": str(path)})
    else:
        console.print(f"[green]✔[/green] 报告已生成: [cyan]{path}[/cyan]")


# ============================================================
# replay（8.8 确定性重放 + 差异定位）
# ============================================================
@app.command()
def replay(
    run_id: str = typer.Argument(..., help="回测 run_id"),
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """按 RunManifest 重放并 diff（orders/fills 逐笔、nav 逐点、manifest_hash）。"""
    try:
        settings = load_settings()
        rep = replay_run(run_id, settings=settings)
    except (typer.Exit, typer.BadParameter):
        raise
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)

    if json_out:
        _print_json(rep.to_dict())
        return
    if rep.identical:
        mh = (rep.new_manifest_hash or "")[:12]
        console.print(f"[green]✔[/green] 重放一致（manifest_hash: [cyan]{mh}…[/cyan]）")
    else:
        console.print(f"[yellow]✘ 重放存在差异[/yellow]（manifest 一致: {rep.manifest_identical}）")
        for d in rep.diffs[:50]:
            console.print(f"  [{d.section}] {d.key}: {d.detail}（{d.old} → {d.new}）")


# ============================================================
# validate（3.6 / JSON Schema）
# ============================================================
@app.command()
def validate(
    config: Annotated[str | None, typer.Option("-c", "--config", help="任务 JSON 路径")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """校验任务配置（pydantic 全字段, 3.6; schema 见 docs/schema/task.schema.json）。"""
    try:
        task = _load_task(config, json_out=json_out)
        # 额外业务校验: 日期合法性 / universe 非空（pydantic 已保证, 此处仅给出友好输出）
        start, end = task.backtest.start, task.backtest.end
        if start > end:
            _emit_error(
                ZQuantError(
                    f"回测区间非法: start({start}) > end({end})",
                    stage="validate",
                    hint="start 必须不晚于 end（YYYY-MM-DD）",
                ),
                json_out=json_out,
            )
    except (typer.Exit, typer.BadParameter):
        raise  # _load_task/_emit_error 已输出结构化错误并置退出码
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)
    if json_out:
        _print_json({"valid": True, "task_name": task.task_name})
    else:
        console.print(
            f"[green]✔[/green] 任务配置有效: [cyan]{task.task_name}[/cyan]"
            f"（universe {len(task.universe)} 标的, {task.backtest.start} ~ {task.backtest.end}）"
        )


# ============================================================
# fetch-etf（3.9 裁剪版）
# ============================================================
_DEMO_ETF_CODES = "510300.SH,510500.SH,159915.SZ"


@app.command("fetch-etf")
def fetch_etf(
    codes: Annotated[
        str | None, typer.Option("--codes", help="逗号分隔 ETF 代码, 如 510300.SH,510500.SH")
    ] = None,
    start: Annotated[str | None, typer.Option("--start", help="开始日期 YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="结束日期 YYYY-MM-DD")] = None,
    demo: Annotated[
        bool, typer.Option("--demo", help="一键重建演示数据（3 只主流 ETF 2020 至今）")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """下载 ETF 日线到本地 CSV（幂等/去重/原子写, 3.9）。"""
    from zquant.data.fetch_etf import make_downloader

    try:
        settings = load_settings()
        if demo:
            codes = codes or _DEMO_ETF_CODES
            start = start or "2020-01-01"
            end = end or date.today().isoformat()
        if not codes or not start or not end:
            raise typer.BadParameter("需要 --codes/--start/--end（或 --demo 一键演示数据）")
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        downloader = make_downloader(Path(settings.data.local_csv.root_path))
        reports = downloader.download(code_list, date.fromisoformat(start), date.fromisoformat(end))
    except (typer.Exit, typer.BadParameter):
        raise
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)

    if json_out:
        _print_json(
            [
                {
                    "code": r.code,
                    "status": r.status,
                    "added_rows": r.added_rows,
                    "dedup_removed": r.dedup_removed,
                    "range": [r.merged_start, r.merged_end],
                    "reason": r.reason,
                }
                for r in reports
            ]
        )
        return
    table = Table(title="ETF 下载报告")
    table.add_column("代码")
    table.add_column("状态")
    table.add_column("新增行")
    table.add_column("去重")
    table.add_column("区间")
    table.add_column("说明")
    mark = {
        "ok": "[green]ok[/green]",
        "skipped": "[yellow]skipped[/yellow]",
        "failed": "[red]failed[/red]",
    }
    for r in reports:
        table.add_row(
            r.code,
            mark.get(r.status, r.status),
            str(r.added_rows),
            str(r.dedup_removed),
            f"{r.merged_start} ~ {r.merged_end}",
            r.reason,
        )
    console.print(table)


# ============================================================
# cache clean（3.7）
# ============================================================
@app.command()
def cache(
    code: Annotated[str | None, typer.Option("--code", help="指定标的")] = None,
    all: Annotated[bool, typer.Option("--all", help="清理全部缓存")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="机读输出")] = False,
) -> None:
    """清理 parquet 二级缓存（缺省需 --all 或 --code）。"""
    from zquant.data.cache import DataCache

    try:
        settings = load_settings()
        cache_obj = DataCache(settings.data.cache.parquet_dir, enabled=True)
        if not all and not code:
            raise typer.BadParameter("需要 --all 或 --code 指定清理范围")
        removed = cache_obj.clean(code)
    except (typer.Exit, typer.BadParameter):
        raise
    except ZQuantError as exc:
        _emit_error(exc, json_out=json_out)
    except Exception as exc:  # noqa: BLE001
        _emit_error(exc, json_out=json_out)
    if json_out:
        _print_json({"removed": removed})
    else:
        console.print(f"[green]✔[/green] 已清理 {removed} 个 parquet 缓存")


# ============================================================
# serve（M2-W0 最小可视版; 设计 7 章引言, 仅 127.0.0.1 本地可信）
# ============================================================
@app.command()
def serve(
    with_task: Annotated[
        str | None,
        typer.Option("--with-task", help="任务 JSON 路径; 提供则启动回测并实时推送到浏览器"),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="监听地址（默认仅本地）")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="监听端口（M4 规划沿用 8501）")] = 8501,
) -> None:
    """Web 最小可视版: 浏览器实时监控 native 回测（WS 事件流, 6.3 信封）。"""
    from zquant.server.run_local import run_serve

    run_serve(with_task=with_task, host=host, port=port)


if __name__ == "__main__":
    app()
