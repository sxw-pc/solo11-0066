"""Structured report construction and bounded-memory streaming renderers.

The renderers in this module never materialize the full report tree, the full
attribution projection or a complete output string.  Domain objects
(``AnomalyGroup`` / ``AnomalyEvent`` / ``AttributionItem``) stay the single
source of truth: each group is projected into one small view at a time and
pushed through fixed-size text buffers.  The legacy dict-based entry points
(``build_report``, ``attribution_document``, ``report_to_markdown``) are kept
byte-for-byte compatible for callers such as the CLI ``report`` command.
"""
from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, TextIO

from drift_attr.attribution import resolve_windows
from drift_attr.config import AppConfig
from drift_attr.dataset import Dataset, dataset_summary
from drift_attr.schema import (
    AnomalyEvent,
    AnomalyGroup,
    SEVERITIES,
    event_to_dict,
    item_to_dict,
)
from drift_attr.timeseries import BuildResult, series_summary

#: Upper bound on the transient text buffer each streaming artifact keeps.
STREAM_BLOCK_SIZE = 64 * 1024


def display_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Group projection: one view per group, never a full-report duplicate tree.
# ---------------------------------------------------------------------------

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


def group_view(
    group: AnomalyGroup, timeline: list[date], cfg: AppConfig
) -> dict[str, Any]:
    """Project a single domain group into the report-shaped dictionary."""
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
        "narrative": group.narrative,
        "attribution": _group_attribution(group, cfg),
        "raw_events": [event_to_dict(event) for event in group.events],
    }


def iter_group_views(
    groups: list[AnomalyGroup], timeline: list[date], cfg: AppConfig
) -> Iterator[dict[str, Any]]:
    for group in groups:
        yield group_view(group, timeline, cfg)


# ---------------------------------------------------------------------------
# Report context: everything the renderers need without a report dict.
# ---------------------------------------------------------------------------

@dataclass
class ReportContext:
    run_id: str
    config: AppConfig
    dataset: Dataset
    build: BuildResult
    groups: list[AnomalyGroup]
    events: list[AnomalyEvent]
    started_at: str
    finished_at: str
    base_dir: Path
    artifacts: list[dict[str, str]]

    def group_views(self) -> Iterator[dict[str, Any]]:
        return iter_group_views(self.groups, self.build.timeline, self.config)


def make_report_context(
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
) -> ReportContext:
    return ReportContext(
        run_id=run_id, config=config, dataset=dataset, build=build,
        groups=groups, events=events, started_at=started_at,
        finished_at=finished_at, base_dir=base_dir, artifacts=artifacts,
    )


def _header_summary(ctx: ReportContext) -> dict[str, Any]:
    severity_counts = {name: 0 for name in SEVERITIES}
    for group in ctx.groups:
        severity_counts[group.severity] += 1
    detector_counts: dict[str, int] = {}
    for event in ctx.events:
        detector_counts[event.detector] = detector_counts.get(event.detector, 0) + 1
    return {
        "raw_event_count": len(ctx.events),
        "anomaly_count": len(ctx.groups),
        "attribution_item_count": sum(
            len(group.attribution_items) for group in ctx.groups
        ),
        "severity_counts": severity_counts,
        "detector_counts": detector_counts,
    }


def report_header(ctx: ReportContext) -> dict[str, Any]:
    """Build the constant report prefix (everything except ``anomalies``)."""
    config = ctx.config
    build = ctx.build
    return {
        "schema_version": "1.0",
        "run": {
            "run_id": ctx.run_id,
            "started_at": ctx.started_at,
            "finished_at": ctx.finished_at,
            "status": "success",
            "config_path": display_path(config.source_path, ctx.base_dir),
            "config_fingerprint": config.fingerprint,
            "rule_fingerprint": config.fingerprint,
            "dataset_path": ctx.dataset.path,
            "dataset_fingerprint": ctx.dataset.fingerprint,
        },
        "dataset": dataset_summary(ctx.dataset),
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
            "formula": "contribution = comparison_sum - baseline_sum; rate = contribution / total_change",
        },
        "summary": _header_summary(ctx),
        "artifacts": ctx.artifacts,
    }


# ---------------------------------------------------------------------------
# Bounded-memory chunked text sink with incremental SHA-256.
# ---------------------------------------------------------------------------

class ChunkedTextWriter:
    """Text sink that flushes fixed-size UTF-8 blocks and hashes them.

    The buffer never grows beyond :data:`STREAM_BLOCK_SIZE` plus the size of a
    single caller fragment, so peak extra memory is independent of the total
    result size.  ``sha256_hex`` always reflects every flushed byte, i.e. the
    exact final file content.
    """

    def __init__(
        self, handle: TextIO, *, block_size: int = STREAM_BLOCK_SIZE
    ) -> None:
        self._handle = handle
        self._block_size = block_size
        self._buffer = ""
        self._digest = hashlib.sha256()

    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        while len(self._buffer) >= self._block_size:
            self._flush(self._buffer[: self._block_size])
            self._buffer = self._buffer[self._block_size :]

    def _flush(self, block: str) -> None:
        data = block.encode("utf-8")
        self._handle.write(block)
        self._digest.update(data)

    def close(self) -> None:
        if self._buffer:
            self._flush(self._buffer)
            self._buffer = ""
        self._handle.flush()

    @property
    def sha256_hex(self) -> str:
        return self._digest.hexdigest()


# ---------------------------------------------------------------------------
# Streaming JSON (indent=2, sort_keys=True, ensure_ascii=False, trailing \n).
# ---------------------------------------------------------------------------

class _LazyArray:
    """Marker for arrays whose elements are produced lazily one at a time."""

    def __init__(self, producer: Iterator[Any]) -> None:
        self.producer = producer


def _write_json_value(
    writer: ChunkedTextWriter, value: Any, level: int
) -> None:
    indent = "  " * level
    child_indent = "  " * (level + 1)
    if isinstance(value, dict):
        if not value:
            writer.write("{}")
            return
        writer.write("{\n")
        for index, key in enumerate(sorted(value)):
            writer.write(child_indent + json.dumps(str(key), ensure_ascii=False) + ": ")
            _write_json_value(writer, value[key], level + 1)
            if index != len(value) - 1:
                writer.write(",")
            writer.write("\n")
        writer.write(indent + "}")
    elif isinstance(value, _LazyArray) or isinstance(value, list):
        producer = value.producer if isinstance(value, _LazyArray) else iter(value)
        first = True
        writer.write("[")
        for element in producer:
            if not first:
                writer.write(",")
            writer.write("\n" + child_indent)
            _write_json_value(writer, element, level + 1)
            first = False
        if first:
            writer.write("]")
        else:
            writer.write("\n" + indent + "]")
    else:
        writer.write(json.dumps(value, ensure_ascii=False))


def write_report_json(writer: ChunkedTextWriter, ctx: ReportContext) -> None:
    """Stream the full report JSON without holding a complete report tree."""
    header = report_header(ctx)
    payload = dict(header)
    payload["anomalies"] = _LazyArray(ctx.group_views())
    _write_json_value(writer, payload, 0)
    writer.write("\n")


def _attribution_rows(ctx: ReportContext) -> Iterator[dict[str, Any]]:
    for view in ctx.group_views():
        for dimension, items in view["attribution"].items():
            for item in items:
                yield {
                    "group_id": view["group_id"],
                    "metric": view["metric"],
                    "dimension_name": view["dimension_name"],
                    "dimension_value": view["dimension_value"],
                    "date": view["date"],
                    "attribution_dimension": dimension,
                    **item,
                }


def write_attribution_json(writer: ChunkedTextWriter, ctx: ReportContext) -> None:
    """Stream the standalone attribution JSON, row by row."""
    payload = {
        "schema_version": "1.0",
        "run_id": ctx.run_id,
        "config_fingerprint": ctx.config.fingerprint,
        "dataset_fingerprint": ctx.dataset.fingerprint,
        "items": _LazyArray(_attribution_rows(ctx)),
    }
    _write_json_value(writer, payload, 0)
    writer.write("\n")


# ---------------------------------------------------------------------------
# Materialized builders (legacy contract, explicit caller opt-in).
# ---------------------------------------------------------------------------

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
    ctx = make_report_context(
        run_id=run_id, config=config, dataset=dataset, build=build,
        groups=groups, events=events, started_at=started_at,
        finished_at=finished_at, base_dir=base_dir, artifacts=artifacts,
    )
    report = report_header(ctx)
    report["anomalies"] = [
        group_view(group, build.timeline, config) for group in groups
    ]
    return report


def attribution_document(report: dict[str, Any]) -> dict[str, Any]:
    run = report["run"]
    rows: list[dict[str, Any]] = []
    for group in report["anomalies"]:
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
        "items": rows,
    }


# ---------------------------------------------------------------------------
# Markdown rendering: line generators shared by streaming and full-dict paths.
# ---------------------------------------------------------------------------

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


def _markdown_group_lines(group: dict[str, Any]) -> Iterator[str]:
    yield (
        f"### {_md(group['date'])} · {_md(group['metric'])} · "
        f"{_md(group['dimension_name'])}={_md(group['dimension_value'])}"
    )
    yield ""
    yield f"- 严重度：{_md(group['severity'])}"
    yield f"- 命中检测器：{_md(', '.join(group['detectors']))}"
    yield f"- 规则：{_md(', '.join(group['rule_names']))}"
    yield f"- 归因窗口：{_md(_window_text(group['attribution_windows']))}"
    yield f"- 解释：{_md(group['narrative'])}"
    yield ""
    yield "| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 其他项 |"
    yield "| ---: | --- | ---: | ---: | ---: | ---: | --- |"
    for dimension, items in group["attribution"].items():
        for item in items:
            rate = item["contribution_rate"]
            rate_text = "n/a" if rate is None else f"{rate * 100:.2f}%"
            yield (
                f"| {item['rank']} | {_md(dimension)}={_md(item['dimension_value'])} | "
                f"{item['current_value']} | {item['baseline_value']} | "
                f"{item['contribution']:+.6f} | {rate_text} | "
                f"{'是' if item['is_other'] else '否'} |"
            )
    yield ""
    yield "证据："
    yield ""
    for event in group["raw_events"]:
        evidence = event.get("evidence", {}).get("summary", "")
        yield (
            f"- `{_md(event['rule_name'])}` / {_md(event['detector'])}："
            f"{_md(evidence)}（score={event['score']}）"
        )
    yield ""


def iter_markdown_lines(
    ctx: ReportContext,
) -> Iterator[str]:
    """Yield Markdown report lines one at a time, group by group."""
    run = report_header(ctx)["run"]
    summary = _header_summary(ctx)
    yield "# 漂移归因台分析报告"
    yield ""
    yield f"- 运行 ID：`{_md(run['run_id'])}`"
    yield f"- 状态：{_md(run['status'])}"
    yield f"- 开始时间：{_md(run['started_at'])}"
    yield f"- 结束时间：{_md(run['finished_at'])}"
    yield f"- 配置指纹：`{_md(run['config_fingerprint'])}`"
    yield f"- 数据指纹：`{_md(run['dataset_fingerprint'])}`"
    yield ""
    yield "## 概要"
    yield ""
    yield "| 指标 | 值 |"
    yield "| --- | --- |"
    yield f"| 原始检测事件 | {summary['raw_event_count']} |"
    yield f"| 合并异常位置 | {summary['anomaly_count']} |"
    yield f"| 归因明细 | {summary['attribution_item_count']} |"
    yield f"| 严重度分布 | {_md(summary['severity_counts'])} |"
    yield ""
    yield "## 异常与归因"
    yield ""
    if not ctx.groups:
        yield "本次运行没有检测到异常。"
        yield ""
    for view in ctx.group_views():
        for line in _markdown_group_lines(view):
            yield line
    yield "## 产物"
    yield ""
    for artifact in ctx.artifacts:
        yield f"- {_md(artifact['kind'])}: `{_md(artifact['path'])}`"
    yield ""
    yield "本报告由本地模板生成，不包含外部模型调用或远程资源引用。"
    yield ""


def write_markdown(writer: ChunkedTextWriter, ctx: ReportContext) -> None:
    # Equivalent to "\n".join(lines): no trailing newline beyond the last line.
    first = True
    for line in iter_markdown_lines(ctx):
        if not first:
            writer.write("\n")
        writer.write(line)
        first = False


def report_to_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    summary = report["summary"]
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
        "",
        "## 异常与归因",
        "",
    ]
    if not report["anomalies"]:
        lines.extend(["本次运行没有检测到异常。", ""])
    for group in report["anomalies"]:
        lines.extend(_markdown_group_lines(group))
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
