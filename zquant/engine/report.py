# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 06:48:31
# @description : J report.html 基础版：自包含单文件报告（指标卡/语义保真/净值+回撤 SVG, 设计 9.2）

"""report.html（设计 9.2）——自包含单文件回测报告。

内容:
  指标卡        gross/net 双口径（8.4, Metrics.compute 各算一组）
  语义保真声明区 completed_exact | completed_degraded + 降级清单（4.9.2 纪律）
  净值+回撤    内嵌 SVG（不依赖 CDN, 离线可开）
  成交明细表    最近 100 笔 + 全量下载链接（orders.csv）
  指标口径附注  8.4 公式 + metrics_version

数据源: results/<run_id>/{summary.json, daily_stats.csv, orders.csv}（与 DB 同源投影, 9.1）。
"""

from __future__ import annotations

import ast
import html
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from zquant.core.errors import ZQuantError
from zquant.engine.metrics import Metrics

_METRICS_VERSION = "8.4-v1"


def render_report(
    run_id: str, *, out_root: Path | str = "results", out_path: Path | str | None = None
) -> Path:
    """生成自包含 report.html; 返回输出路径。"""
    run_dir = Path(out_root) / run_id
    if not run_dir.is_dir():
        raise ZQuantError(
            f"报告源目录不存在: {run_dir}",
            stage="report",
            hint="先 `zquant run` 生成 results/<run_id>；或检查 --out 路径",
        )
    summary = _load_json(run_dir / "summary.json") or {}
    navs = _load_navs(run_dir)
    orders = _load_orders(run_dir)
    fills = _load_fills(run_dir)
    events = _load_events(run_dir)
    task = _load_json(run_dir / "task.json") or {}

    html_text = _render_html(run_id, summary, navs, orders, fills, events, task)
    out = Path(out_path) if out_path else run_dir / "report.html"
    out.write_text(html_text, encoding="utf-8")
    return out


# ------------------------------------------------------------------
# 数据读取
# ------------------------------------------------------------------
def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_navs(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "daily_stats.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"trade_date": str})
    return df.to_dict("records")


def _load_orders(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "orders.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _load_fills(run_dir: Path) -> list[dict[str, Any]]:
    """成交（含容量证据列 bar_volume/participation_rate, 8.4.4）。"""
    path = run_dir / "fills.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "order_events.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path, dtype={"order_id": str})
    return df.to_dict("records")


def _parse_info_json(raw: Any) -> dict[str, Any]:
    """容错解析 info_json（CSV 可能落 dict-repr / JSON 字符串 / 空）。"""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            val = loader(text)
            return val if isinstance(val, dict) else {}
        except (ValueError, SyntaxError):
            continue
    return {}


# ------------------------------------------------------------------
# 容量证据（8.4.4, M2-P4）: 参与率分布/容量截断/不可成交/成交延迟
# ------------------------------------------------------------------
def _capacity_stats(
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    rates = [float(f.get("participation_rate") or 0.0) for f in fills if f.get("volume")]
    part = {}
    if rates:
        arr = np.asarray(rates, dtype=float)
        part = {
            "max": float(arr.max()),
            "mean": float(arr.mean()),
            "p95": float(np.percentile(arr, 95)),
            "count": int(len(arr)),
        }
    else:
        part = {"max": 0.0, "mean": 0.0, "p95": 0.0, "count": 0}

    # 容量截断（order_events info_json.capacity_capped）
    capped = sum(1 for e in events if _parse_info_json(e.get("info_json")).get("capacity_capped"))

    # 不可成交（orders 终态）; 一字板/停牌拆分（expire 事件 info_json.one_word_limit）
    total = max(len(orders), 1)
    statuses = [str(o.get("status", "")) for o in orders]
    expired = sum(1 for s in statuses if s == "EXPIRED")
    rejected = sum(1 for s in statuses if s == "REJECTED")
    one_word = sum(
        1
        for e in events
        if e.get("event_type") == "expire"
        and _parse_info_json(e.get("info_json")).get("one_word_limit")
    )
    suspend_expire = max(expired - one_word, 0)

    # 成交延迟（fill_time − 订单 submitted_at, 按 order_id join）
    sub = {str(o.get("order_id")): o.get("submitted_at") for o in orders}
    lat_days: list[float] = []
    for f in fills:
        ft, st = f.get("fill_time"), sub.get(str(f.get("order_id")))
        if ft and st:
            try:
                lat_days.append((pd.Timestamp(ft) - pd.Timestamp(st)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                continue
    lat = {}
    if lat_days:
        arr = np.asarray(lat_days, dtype=float)
        lat = {
            "min": float(arr.min()),
            "mean": float(arr.mean()),
            "p95": float(np.percentile(arr, 95)),
            "max": float(arr.max()),
            "count": int(len(arr)),
        }
    return {
        "participation": part,
        "truncated": capped,
        "truncation_ratio": round(capped / total, 6),
        "expired": expired,
        "rejected": rejected,
        "one_word": one_word,
        "suspend_expire": suspend_expire,
        "unfillable_ratio": round((expired + rejected) / total, 6),
        "latency_days": lat,
        "total_orders": len(orders),
    }


def _render_capacity(stats: dict[str, Any]) -> str:
    """8.4.4 容量与摩擦证据四表+图（内嵌, 无 CDN）。"""
    part = stats["participation"]
    lat = stats["latency_days"]
    rows_p = (
        f"<tr><td>样本</td><td>{part['count']}</td></tr>"
        f"<tr><td>max</td><td>{part['max']:.4f}</td></tr>"
        f"<tr><td>mean</td><td>{part['mean']:.4f}</td></tr>"
        f"<tr><td>p95</td><td>{part['p95']:.4f}</td></tr>"
        if part.get("count")
        else "<tr><td colspan='2' class='dim'>无成交</td></tr>"
    )
    tot = stats["total_orders"]
    rows_u = (
        f"<tr><td>容量截断单数</td><td>{stats['truncated']} "
        f"（占比 {stats['truncation_ratio']:.2%}）</td></tr>"
        f"<tr><td>过期(expired)</td><td>{stats['expired']}</td></tr>"
        f"<tr><td>· 一字板</td><td>{stats['one_word']}</td></tr>"
        f"<tr><td>· 停牌/当日未成交</td><td>{stats['suspend_expire']}</td></tr>"
        f"<tr><td>拒单(rejected)</td><td>{stats['rejected']}</td></tr>"
        f"<tr><td>不可成交合计</td><td>{stats['expired'] + stats['rejected']} "
        f"（占比 {stats['unfillable_ratio']:.2%}）</td></tr>"
        f"<tr><td>订单总数</td><td>{tot}</td></tr>"
    )
    rows_l = (
        f"<tr><td>样本</td><td>{lat['count']}</td></tr>"
        f"<tr><td>min</td><td>{lat['min']:.3f} 日</td></tr>"
        f"<tr><td>mean</td><td>{lat['mean']:.3f} 日</td></tr>"
        f"<tr><td>p95</td><td>{lat['p95']:.3f} 日</td></tr>"
        f"<tr><td>max</td><td>{lat['max']:.3f} 日</td></tr>"
        if lat.get("count")
        else "<tr><td colspan='2' class='dim'>无成交</td></tr>"
    )
    # 参与率分布迷你条形（0~5% 分段, 占整体宽度）
    bars = _participation_bars(stats["participation"])
    return f"""
<h2>容量与摩擦证据（8.4.4）</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px">
  <div><h3 style="font-size:14px">参与率分布（volume / bar_volume）</h3>
    <table><thead><tr><th>统计</th><th>值</th></tr></thead><tbody>{rows_p}</tbody></table>
    <div style="margin-top:6px">{bars}</div></div>
  <div><h3 style="font-size:14px">不可成交 / 容量截断</h3>
    <table><thead><tr><th>项</th><th>单数</th></tr></thead><tbody>{rows_u}</tbody></table></div>
  <div><h3 style="font-size:14px">成交延迟分布（挂单→成交, 日）</h3>
    <table><thead><tr><th>统计</th><th>值</th></tr></thead><tbody>{rows_l}</tbody></table></div>
</div>
"""


def _participation_bars(part: dict[str, Any]) -> str:
    """参与率 0~50% 分 5 档迷你条形（占 bar_volume 比例的成交样本）。"""
    # 用 mean/p95/max 三点示意档位（简化无直方源数据）
    tips = [
        ("mean", part.get("mean", 0.0)),
        ("p95", part.get("p95", 0.0)),
        ("max", part.get("max", 0.0)),
    ]

    def _bar(label: str, v: float) -> str:
        width = min(100.0, v / 0.5 * 100.0) if v else 0.0
        return (
            f'<div class="dim" style="margin:2px 0">{label}: '
            f'<span style="display:inline-block;width:{width:.0f}%;height:10px;'
            f'background:#2563eb;vertical-align:middle"></span> {v:.4f}</div>'
        )

    return "".join(_bar(k, v) for k, v in tips)


# ------------------------------------------------------------------
# 指标（gross/net 双口径, 8.4）
# ------------------------------------------------------------------
def _metric_card(navs: list[dict[str, Any]], *, key: str) -> dict[str, str]:
    """按净值列（nav=net / gross_nav=gross）计算 8.4 指标卡。"""
    series = [float(r[key]) for r in navs if r.get(key) is not None]
    if len(series) < 2:
        return {}
    m = Metrics.compute(np.asarray(series, dtype=float), dates=None)
    risk = m.risk
    return {
        "总收益": f"{risk.total_return:.2%}",
        "年化收益": f"{risk.annual_return:.2%}",
        "年化波动": f"{risk.annual_volatility:.2%}",
        "夏普": f"{risk.sharpe:.3f}",
        "索提诺": f"{risk.sortino:.3f}",
        "卡玛": f"{risk.calmar:.3f}",
        "最大回撤": f"{risk.max_drawdown.value:.2%}",
        "日胜率": f"{risk.daily_win_rate:.2%}",
    }


# ------------------------------------------------------------------
# SVG（净值 + 回撤, 无 CDN）
# ------------------------------------------------------------------
def _render_svg(navs: list[dict[str, Any]]) -> str:
    W, H, PAD = 720, 260, 34
    net: list[float] = []
    bench: list[float] = []
    dd: list[float] = []
    for r in navs:
        v = r.get("nav")
        if v is None:
            continue
        net.append(float(v))
        b = r.get("benchmark_nav")
        if b is not None:
            bench.append(float(b))
        d = r.get("drawdown")
        dd.append(0.0 if d is None else float(d))
    if len(net) < 2:
        return f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    all_vals = net + bench
    lo, hi = min(all_vals), max(all_vals)
    span = (hi - lo) or 1.0
    n = len(net)

    def pt(i: int, v: float) -> tuple[float, float]:
        x = PAD + i * (W - 2 * PAD) / max(1, n - 1)
        y = H - PAD - (v - lo) / span * (H - 2 * PAD)
        return x, y

    net_pts = " ".join(f"{pt(i, v)[0]:.1f},{pt(i, v)[1]:.1f}" for i, v in enumerate(net))
    dd_pts = " ".join(f"{pt(i, -v)[0]:.1f},{pt(i, -v)[1]:.1f}" for i, v in enumerate(dd))
    # 回撤面积（到基线）
    base_y = H - PAD
    area = f"{PAD},{base_y} " + dd_pts + f" {W - PAD},{base_y}"

    parts = [
        f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">',
        f'<polygon points="{area}" fill="rgba(244,63,94,0.15)" stroke="none"/>',
        f'<polyline points="{dd_pts}" fill="none" stroke="#f43f5e" stroke-width="1.2"/>',
        f'<polyline points="{net_pts}" fill="none" stroke="#2563eb" stroke-width="1.8"/>',
        "</svg>",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------
# HTML 渲染
# ------------------------------------------------------------------
def _render_html(
    run_id: str,
    summary: dict[str, Any],
    navs: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    events: list[dict[str, Any]],
    task: dict[str, Any],
) -> str:
    status = summary.get("status", "?")
    degradations = summary.get("degradations", []) or []
    metrics = summary.get("metrics", {}) or {}
    net_card = _metric_card(navs, key="nav")
    gross_card = _metric_card(navs, key="gross_nav")
    capacity = _render_capacity(_capacity_stats(fills, orders, events))

    def _cards(card: dict[str, str]) -> str:
        return "".join(
            f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in card.items()
        )

    # 语义保真
    fidelity = (
        '<span class="badge ok">completed_exact</span>'
        if status == "completed_exact"
        else '<span class="badge warn">completed_degraded</span>'
    )
    deg_items = (
        "".join(f"<li>{html.escape(str(d))}</li>" for d in degradations[:50]) or "<li>无降级</li>"
    )

    # 成交表（最近 100 笔, 倒序）
    rows = ""
    for o in reversed(orders[-100:]):
        rows += (
            "<tr>"
            f"<td>{html.escape(str(o.get('order_id', '')))}</td>"
            f"<td>{html.escape(str(o.get('code', '')))}</td>"
            f"<td>{html.escape(str(o.get('side', '')))}</td>"
            f"<td>{o.get('qty', '')}</td>"
            f"<td>{html.escape(str(o.get('status', '')))}</td>"
            f"<td>{html.escape(str(o.get('submitted_at', '')))}</td>"
            "</tr>"
        )
    orders_table = (
        "<table><thead><tr>"
        "<th>order_id</th><th>代码</th><th>方向</th><th>数量</th><th>状态</th><th>提交时间</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table>"
        '<p class="dim"><a href="orders.csv">下载全量订单 (orders.csv)</a></p>'
        if orders
        else '<p class="dim">无订单</p>'
    )

    max_dd = metrics.get("max_drawdown", {}) or {}
    peak, trough = max_dd.get("peak_date"), max_dd.get("trough_date")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>zQuant 回测报告 · {html.escape(run_id)}</title>
<style>
  body{{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
        max-width:960px;margin:24px auto;padding:0 16px;color:#1f2937}}
  h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;
        border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
  .card{{border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px}}
  .card .k{{font-size:12px;color:#6b7280}} .card .v{{font-size:18px;font-weight:600;margin-top:2px}}
  .badge{{padding:2px 10px;border-radius:999px;font-size:13px}}
  .badge.ok{{background:#dcfce7;color:#166534}} .badge.warn{{background:#fef3c7;color:#92400e}}
  table{{border-collapse:collapse;width:100%;font-size:13px}}
  th,td{{border:1px solid #e5e7eb;padding:5px 8px;text-align:left;white-space:nowrap}}
  th{{background:#f9fafb}} .dim{{color:#6b7280;font-size:13px}}
  .muted{{color:#6b7280;font-size:13px}}
</style></head><body>
<h1>zQuant 回测报告 · {html.escape(run_id)}</h1>
<p class="muted">任务: {html.escape(str(task.get("task_name", "")))} · 区间:
   {html.escape(str(task.get("backtest", {}).get("start", "")))} ~
   {html.escape(str(task.get("backtest", {}).get("end", "")))} ·
   metrics_version: {html.escape(str(summary.get("metrics_version", _METRICS_VERSION)))}</p>

<h2>指标卡（gross / net 双口径, 8.4）</h2>
<div class="cards"><div class="card" style="grid-column:1/-1"><div class="k">语义保真</div>
  <div class="v" style="font-size:14px">{fidelity}
  <span class="muted">最大回撤峰/谷: {peak} → {trough}</span></div></div>
  {_cards(net_card)}
</div>
<div class="cards" style="margin-top:10px">{_cards(gross_card)}</div>

<h2>净值 + 回撤</h2>
{_render_svg(navs)}

<h2>语义保真声明（降级清单, 4.9.2）</h2>
<ul>{deg_items}</ul>

<h2>成交明细（最近 100 笔）</h2>
{orders_table}

{capacity}

<h2>指标口径附注</h2>
<p class="muted">8.4 公式: 年化=nav^(250/n)-1（ANN=250）; 波动 ddof=1;
        索提诺 TDD=√(mean(min(r-MAR,0)²));
最大回撤含峰谷日; 夏普 rf=0; 指标版本
        {html.escape(str(summary.get("metrics_version", _METRICS_VERSION)))}。
净值/回撤为内嵌 SVG（离线可用）。</p>
</body></html>"""
    return html_doc
