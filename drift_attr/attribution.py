"""Equal-window dimension contribution attribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from drift_attr.config import AttributionConfig
from drift_attr.normalize import apply_group_narrative, make_item_id
from drift_attr.schema import AnomalyGroup, AttributionItem, Series


@dataclass(frozen=True)
class Windows:
    comparison_start: date
    comparison_end: date
    baseline_start: date
    baseline_end: date
    comparison_dates: tuple[date, ...]
    baseline_dates: tuple[date, ...]


def resolve_windows(timeline: list[date], event_date: date, cfg: AttributionConfig) -> Windows | None:
    index = None
    for position, period in enumerate(timeline):
        if period <= event_date:
            index = position
        else:
            break
    if index is None:
        return None
    comparison_start = max(0, index - cfg.comparison_window + 1)
    baseline_end = comparison_start - 1
    baseline_start = max(0, baseline_end - cfg.baseline_window + 1)
    if baseline_end < baseline_start:
        return None
    return Windows(
        comparison_start=timeline[comparison_start],
        comparison_end=timeline[index],
        baseline_start=timeline[baseline_start],
        baseline_end=timeline[baseline_end],
        comparison_dates=tuple(timeline[comparison_start : index + 1]),
        baseline_dates=tuple(timeline[baseline_start : baseline_end + 1]),
    )


def _rate(contribution: float, total_change: float) -> float | None:
    if abs(total_change) < 1e-12:
        return None
    return contribution / total_change


def _formula(current: float, baseline: float, contribution: float, total: float) -> str:
    rate = _rate(contribution, total)
    rate_text = "undefined (total_change=0)" if rate is None else f"{rate:.6f}"
    return (
        f"current {current:.6f} - baseline {baseline:.6f} = "
        f"contribution {contribution:+.6f}; rate={rate_text}"
    )


def _explanation(
    dimension: str, value: str, current: float, baseline: float,
    contribution: float, rate: float | None, is_other: bool,
) -> str:
    label = f"{dimension}={value}" if not is_other else f"{dimension} 的其他维度合计"
    rate_text = "不适用（总变化为 0）" if rate is None else f"{rate * 100:.2f}%"
    return (
        f"{label} 在异常窗口为 {current:.2f}，对照窗口为 {baseline:.2f}，"
        f"贡献额 {contribution:+.2f}，贡献率 {rate_text}。"
    )


def _dimension_items(
    group: AnomalyGroup,
    series_list: list[Series],
    timeline: list[date],
    dimension: str,
    cfg: AttributionConfig,
) -> list[AttributionItem]:
    windows = resolve_windows(timeline, group.date, cfg)
    if windows is None:
        return []
    candidates = [
        series
        for series in series_list
        if series.metric == group.metric and series.dimension_name == dimension
    ]
    if not candidates:
        return []
    rows: list[tuple[str, float, float, float]] = []
    for series in sorted(candidates, key=lambda item: item.dimension_value):
        values = {point.date: point.value for point in series.points}
        current = sum(values.get(period, 0.0) for period in windows.comparison_dates)
        baseline = sum(values.get(period, 0.0) for period in windows.baseline_dates)
        rows.append((series.dimension_value, current, baseline, current - baseline))
    total_change = sum(row[3] for row in rows)
    ordered = sorted(rows, key=lambda row: (-abs(row[3]), row[0]))
    selected = ordered[: cfg.top_n]
    remainder = ordered[cfg.top_n :]
    items: list[AttributionItem] = []
    for rank, (value, current, baseline, contribution) in enumerate(selected, start=1):
        rate = _rate(contribution, total_change)
        items.append(
            AttributionItem(
                item_id=make_item_id(group.run_id, group.group_id, dimension, value, rank),
                group_id=group.group_id,
                event_id=group.events[0].event_id,
                attribution_dimension=dimension,
                dimension_value=value,
                current_value=current,
                baseline_value=baseline,
                contribution=contribution,
                contribution_rate=rate,
                rank=rank,
                is_other=False,
                formula=_formula(current, baseline, contribution, total_change),
                explanation=_explanation(
                    dimension, value, current, baseline, contribution, rate, False
                ),
            )
        )
    if remainder:
        current = sum(row[1] for row in remainder)
        baseline = sum(row[2] for row in remainder)
        contribution = sum(row[3] for row in remainder)
        rate = _rate(contribution, total_change)
        rank = len(selected) + 1
        items.append(
            AttributionItem(
                item_id=make_item_id(
                    group.run_id, group.group_id, dimension, cfg.other_label, rank
                ),
                group_id=group.group_id,
                event_id=group.events[0].event_id,
                attribution_dimension=dimension,
                dimension_value=cfg.other_label,
                current_value=current,
                baseline_value=baseline,
                contribution=contribution,
                contribution_rate=rate,
                rank=rank,
                is_other=True,
                formula=_formula(current, baseline, contribution, total_change),
                explanation=_explanation(
                    dimension, cfg.other_label, current, baseline,
                    contribution, rate, True,
                ),
            )
        )
    return items


def attribute_groups(
    groups: list[AnomalyGroup],
    series_list: list[Series],
    timeline: list[date],
    cfg: AttributionConfig,
) -> list[AttributionItem]:
    all_items: list[AttributionItem] = []
    for group in groups:
        items: list[AttributionItem] = []
        for dimension in cfg.dimensions:
            items.extend(_dimension_items(group, series_list, timeline, dimension, cfg))
        group.attribution_items = items
        apply_group_narrative(group)
        all_items.extend(items)
    return all_items
