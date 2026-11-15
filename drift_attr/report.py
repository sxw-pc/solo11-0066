"""Structured report construction and Markdown rendering."""
from __future__ import annotations
import html
from datetime import date
from pathlib import Path
from typing import Any
from drift_attr.attribution import resolve_windows
from drift_attr.config import AppConfig
from drift_attr.dataset import Dataset, dataset_summary
from drift_attr.schema import (
    AnomalyEvent,
    AnomalyGroup,
    SEVERITIES,
    context_to_dict,
    event_to_dict,
    item_to_dict,
)
from drift_attr.timeseries import BuildResult, series_summary

MISSING_POLICY_NOTES = {
    "zero_fill": (
        "未记录的日期按 0 计入完整日历窗口；"
        "总变化为全部类别贡献之和，贡献率可加和。"
    ),
    "common_dates": (
        "仅在所有可比类别共同可观测的日期上归因；"
        "总变化为可比类别贡献之和，不可比类别不补零、不计入总变化。"
    ),
}
def display_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
def _group_attribution(group: AnomalyGroup, cfg: AppConfig) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for dimension in cfg.attribution.dimensions:
        items = [
            item_to_dict(item)
            for item in group.attribution_items
            if item.attribution_dimension == dimension
        ]
        items.sort(key=lambda item: item["rank"])
        result[dimension] = items
    return result
def _window_dict(group: AnomalyGroup, timeline: list[date], cfg: AppConfig) -> dict[str, Any] | None:
    windows = resolve_windows(timeline, group.date, cfg.attribution)
    if windows is None:
        return None
    return {
        "comparison_start": windows.comparison_start.isoformat(),
        "comparison_end": windows.comparison_end.isoformat(),
        "baseline_start": windows.baseline_start.isoformat(),
        "baseline_end": windows.baseline_end.isoformat(),
        "comparison_periods": len(windows.comparison_dates),
        "baseline_periods": len(windows.baseline_dates),
    }
def _group_dict(
    group: AnomalyGroup, timeline: list[date], cfg: AppConfig
) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "metric": group.metric,
        "dimension_name": group.dimension_name,
        "dimension_value": group.dimension_value,
        "date": group.date.isoformat(),
        "severity": group.severity,
        "detectors": group.detectors,
        "rule_names": group.rule_names,
        "event_count": len(group.events),
        "window_start": group.window_start.isoformat(),
        "window_end": group.window_end.isoformat(),
        "attribution_windows": _window_dict(group, timeline, cfg),
        "attribution_contexts": {
            dimension: context_to_dict(context)
            for dimension, context in group.attribution_contexts.items()
        },
        "narrative": group.narrative,
        "attribution": _group_attribution(group, cfg),
        "raw_events": [event_to_dict(event) for event in group.events],
    }
def build_report(
    *,
    run_id: str,
    config: AppConfig,
    dataset: Dataset,
    build: BuildResult,
    groups: list[AnomalyGroup],
    events: list[AnomalyEvent],
    started_at: str,
    finished_at: str,
    base_dir: Path,
    artifacts: list[dict[str, str]],
) -> dict[str, Any]:
    attribution_items = [item for group in groups for item in group.attribution_items]
    severity_counts = {name: 0 for name in SEVERITIES}
    for group in groups:
        severity_counts[group.severity] += 1
    detector_counts: dict[str, int] = {}
    for event in events:
        detector_counts[event.detector] = detector_counts.get(event.detector, 0) + 1
    report = {
        "schema_version": "1.0",
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": "success",
            "config_path": display_path(config.source_path, base_dir),
            "config_fingerprint": config.fingerprint,
            "rule_fingerprint": config.fingerprint,
            "dataset_path": dataset.path,
            "dataset_fingerprint": dataset.fingerprint,
        },
        "dataset": dataset_summary(dataset),
        "series": {
            **series_summary(build.series),
            "timeline_start": build.timeline[0].isoformat() if build.timeline else None,
            "timeline_end": build.timeline[-1].isoformat() if build.timeline else None,
            "granularity": config.series.granularity,
            "aggregate": config.series.aggregate,
            "fill_missing": config.series.fill_missing,
            "warnings": build.warnings,
        },
        "rules": [
            {
                "name": rule.name,
                "type": rule.detector_type,
                "metrics": list(rule.metrics),
                "severity": rule.severity,
                "params": rule.params,
            }
            for rule in config.detectors
        ],
        "attribution_policy": {
            "dimensions": list(config.attribution.dimensions),
            "top_n": config.attribution.top_n,
            "baseline_window": config.attribution.baseline_window,
            "comparison_window": config.attribution.comparison_window,
            "other_label": config.attribution.other_label,
            "missing_policy": config.attribution.missing_policy,
            "missing_policy_note": MISSING_POLICY_NOTES[config.attribution.missing_policy],
            "formula": "contribution = comparison_sum - baseline_sum; rate = contribution / total_change",
        },
        "summary": {
            "raw_event_count": len(events),
            "anomaly_count": len(groups),
            "attribution_item_count": len(attribution_items),
            "severity_counts": severity_counts,
            "detector_counts": detector_counts,
        },
        "anomalies": [_group_dict(group, build.timeline, config) for group in groups],
        "artifacts": artifacts,
    }
    return report
def attribution_document(report: dict[str, Any]) -> dict[str, Any]:
    run = report["run"]
    rows: list[dict[str, Any]] = []
    contexts: dict[str, dict[str, Any]] = {}
    for group in report["anomalies"]:
        contexts[group["group_id"]] = group["attribution_contexts"]
        for dimension, items in group["attribution"].items():
            for item in items:
                rows.append(
                    {
                        "group_id": group["group_id"],
                        "metric": group["metric"],
                        "dimension_name": group["dimension_name"],
                        "dimension_value": group["dimension_value"],
                        "date": group["date"],
                        "attribution_dimension": dimension,
                        **item,
                    }
                )
    return {
        "schema_version": "1.0",
        "run_id": run["run_id"],
        "config_fingerprint": run["config_fingerprint"],
        "dataset_fingerprint": run["dataset_fingerprint"],
        "missing_policy": report["attribution_policy"]["missing_policy"],
        "items": rows,
        "contexts": contexts,
    }
def _md(value: object) -> str:
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ")
def _window_text(window: dict[str, Any] | None) -> str:
    if not window:
        return "n/a"
    return (
        f"对照 {window['baseline_start']} ~ {window['baseline_end']} "
        f"({window['baseline_periods']} 期) / 异常 "
        f"{window['comparison_start']} ~ {window['comparison_end']} "
        f"({window['comparison_periods']} 期)"
    )
def _coverage_text(coverage: dict[str, Any]) -> str:
    def side(stats: dict[str, Any] | None) -> str:
        if not stats:
            return "n/a"
        text = f"{stats['observed']}/{stats['expected']}"
        if stats.get("imputed"):
            text += f"(补{stats['imputed']})"
        return text
    if not coverage:
        return "n/a"
    return f"{side(coverage.get('baseline'))} / {side(coverage.get('comparison'))}"
def _context_lines(group: dict[str, Any]) -> list[str]:
    window = group["attribution_windows"] or {}
    lines: list[str] = []
    for dimension, context in group["attribution_contexts"].items():
        baseline_dates = "、".join(context["baseline_dates"]) or "（空）"
        comparison_dates = "、".join(context["comparison_dates"]) or "（空）"
        baseline_expected = window.get("baseline_periods", "?")
        comparison_expected = window.get("comparison_periods", "?")
        if context["status"] == "ok":
            status = "正常"
        else:
            status = f"不可比（{context['status_reason']}）"
        lines.append(
            f"- 维度 {_md(dimension)} 实际归因日期：对照 {_md(baseline_dates)} "
            f"（{context['baseline_periods']}/{baseline_expected} 期）→ 异常 "
            f"{_md(comparison_dates)}（{context['comparison_periods']}/{comparison_expected} 期）；"
            f"状态：{_md(status)}"
        )
    return lines
def report_to_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    summary = report["summary"]
    policy = report["attribution_policy"]
    lines = [
        "# 漂移归因台分析报告",
        "",
        f"- 运行 ID：`{_md(run['run_id'])}`",
        f"- 状态：{_md(run['status'])}",
        f"- 开始时间：{_md(run['started_at'])}",
        f"- 结束时间：{_md(run['finished_at'])}",
        f"- 配置指纹：`{_md(run['config_fingerprint'])}`",
        f"- 数据指纹：`{_md(run['dataset_fingerprint'])}`",
        "",
        "## 概要",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 原始检测事件 | {summary['raw_event_count']} |",
        f"| 合并异常位置 | {summary['anomaly_count']} |",
        f"| 归因明细 | {summary['attribution_item_count']} |",
        f"| 严重度分布 | {_md(summary['severity_counts'])} |",
        f"| 缺测口径 | {_md(policy['missing_policy'])}：{_md(policy['missing_policy_note'])} |",
        "",
        "## 异常与归因",
        "",
    ]
    if not report["anomalies"]:
        lines.extend(["本次运行没有检测到异常。", ""])
    for group in report["anomalies"]:
        lines.extend(
            [
                f"### {_md(group['date'])} · {_md(group['metric'])} · "
                f"{_md(group['dimension_name'])}={_md(group['dimension_value'])}",
                "",
                f"- 严重度：{_md(group['severity'])}",
                f"- 命中检测器：{_md(', '.join(group['detectors']))}",
                f"- 规则：{_md(', '.join(group['rule_names']))}",
                f"- 归因窗口：{_md(_window_text(group['attribution_windows']))}",
                *_context_lines(group),
                f"- 解释：{_md(group['narrative'])}",
                "",
                "| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 覆盖（对照/异常） | 状态 | 其他项 |",
                "| ---: | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for dimension, items in group["attribution"].items():
            for item in items:
                rate = item["contribution_rate"]
                rate_text = "n/a" if rate is None else f"{rate * 100:.2f}%"
                if item["status"] == "ok":
                    current_text = str(item["current_value"])
                    baseline_text = str(item["baseline_value"])
                    contribution_text = f"{item['contribution']:+.6f}"
                    status_text = "正常"
                else:
                    current_text = baseline_text = contribution_text = "—"
                    status_text = f"不可比：{item['status_reason']}"
                lines.append(
                    f"| {item['rank']} | {_md(dimension)}={_md(item['dimension_value'])} | "
                    f"{current_text} | {baseline_text} | "
                    f"{contribution_text} | {rate_text} | "
                    f"{_md(_coverage_text(item['coverage']))} | {_md(status_text)} | "
                    f"{'是' if item['is_other'] else '否'} |"
                )
        lines.extend(["", "证据：", ""])
        for event in group["raw_events"]:
            evidence = event.get("evidence", {}).get("summary", "")
            lines.append(
                f"- `{_md(event['rule_name'])}` / {_md(event['detector'])}："
                f"{_md(evidence)}（score={event['score']}）"
            )
        lines.append("")
    lines.extend(
        [
            "## 产物",
            "",
            *[
                f"- {_md(artifact['kind'])}: `{_md(artifact['path'])}`"
                for artifact in report["artifacts"]
            ],
            "",
            "本报告由本地模板生成，不包含外部模型调用或远程资源引用。",
            "",
        ]
    )
    return "\n".join(lines)
