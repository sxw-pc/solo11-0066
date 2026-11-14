"""Deterministic period aggregation, date alignment and missing-value filling."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from drift_attr.config import SeriesConfig
from drift_attr.dataset import Dataset, MetricRow
from drift_attr.schema import Point, Series


@dataclass
class BuildResult:
    series: list[Series]
    timeline: list[date]
    warnings: list[str] = field(default_factory=list)

    @property
    def imputed_points(self) -> int:
        return sum(series.fill_count for series in self.series)


def bucket_date(value: date, cfg: SeriesConfig) -> date:
    """Return the label date for a daily or weekly period."""
    if cfg.granularity == "day":
        return value
    if cfg.week_start == "sunday":
        offset = (value.weekday() + 1) % 7
    else:
        offset = value.weekday()
    return value - timedelta(days=offset)


def build_timeline(dataset: Dataset, cfg: SeriesConfig) -> list[date]:
    dates = [row.date for row in dataset.rows]
    first = bucket_date(min(dates), cfg)
    last = bucket_date(max(dates), cfg)
    step = timedelta(days=1 if cfg.granularity == "day" else 7)
    timeline: list[date] = []
    current = first
    while current <= last:
        timeline.append(current)
        current += step
    return timeline


def _aggregate(values: list[float], method: str) -> float:
    if method == "mean":
        return statistics.fmean(values)
    return float(sum(values))


def _group_rows(dataset: Dataset, cfg: SeriesConfig) -> dict[tuple[str, str, str], dict[date, list[float]]]:
    groups: dict[tuple[str, str, str], dict[date, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in dataset.rows:
        key = (row.metric, row.dimension_name, row.dimension_value)
        groups[key][bucket_date(row.date, cfg)].append(row.value)
    return groups


def _fill_series(
    metric: str,
    dimension_name: str,
    dimension_value: str,
    buckets: dict[date, list[float]],
    timeline: list[date],
    cfg: SeriesConfig,
) -> tuple[Series, str | None]:
    observed = {
        period: _aggregate(values, cfg.aggregate)
        for period, values in buckets.items()
    }
    points: list[Point] = []
    fill_count = 0
    previous: float | None = None
    for period in timeline:
        if period in observed:
            value = observed[period]
            points.append(Point(period, value, False))
            previous = value
            continue
        if cfg.fill_missing == "drop":
            continue
        if cfg.fill_missing == "previous":
            if previous is None:
                continue
            points.append(Point(period, previous, True))
            fill_count += 1
            continue
        points.append(Point(period, 0.0, True))
        fill_count += 1
    warning: str | None = None
    if fill_count:
        warning = (
            f"序列 {metric}/{dimension_name}={dimension_value} 有 "
            f"{fill_count} 个周期按 {cfg.fill_missing} 策略补值"
        )
    return (
        Series(
            metric=metric,
            dimension_name=dimension_name,
            dimension_value=dimension_value,
            points=points,
            fill_count=fill_count,
        ),
        warning,
    )


def build_series(dataset: Dataset, cfg: SeriesConfig) -> BuildResult:
    """Aggregate all rows and align every series to the same period timeline."""
    groups = _group_rows(dataset, cfg)
    timeline = build_timeline(dataset, cfg)
    series_list: list[Series] = []
    warnings: list[str] = []
    for key in sorted(groups):
        series, warning = _fill_series(*key, groups[key], timeline, cfg)
        series_list.append(series)
        if warning:
            warnings.append(warning)
    if cfg.fill_missing == "previous":
        leading_drop = [
            series.label
            for series in series_list
            if series.points and series.points[0].date != timeline[0]
        ]
        if leading_drop:
            warnings.append(
                "前向填充无法覆盖序列开头，已丢弃前导空段: " + ", ".join(leading_drop)
            )
    return BuildResult(series=series_list, timeline=timeline, warnings=warnings)


def series_summary(series_list: list[Series]) -> dict[str, object]:
    return {
        "series_count": len(series_list),
        "point_count": sum(len(series.points) for series in series_list),
        "imputed_points": sum(series.fill_count for series in series_list),
        "series": [
            {
                "metric": series.metric,
                "dimension_name": series.dimension_name,
                "dimension_value": series.dimension_value,
                "points": len(series.points),
                "imputed": series.fill_count,
            }
            for series in series_list
        ],
    }
