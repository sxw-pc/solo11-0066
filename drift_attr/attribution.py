"""Equal-window dimension contribution attribution with missing-data policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from drift_attr.config import AttributionConfig
from drift_attr.normalize import apply_group_narrative, make_item_id
from drift_attr.schema import (
    AnomalyGroup,
    AttributionContext,
    AttributionItem,
    Point,
    Series,
)

REASON_NO_BASELINE = "对照窗口无有效观测（类别新出现或基期整段缺测）"
REASON_NO_COMPARISON = "异常窗口无有效观测（类别消失或当前整段缺测）"
REASON_NO_OBSERVATIONS = "对照窗口与异常窗口均无有效观测"
REASON_NO_COMMON_BASELINE = "对照窗口不存在所有可比类别共同可观测的日期"
REASON_NO_COMMON_COMPARISON = "异常窗口不存在所有可比类别共同可观测的日期"
REASON_NO_COMPARABLE = "没有在对照与异常窗口均有有效观测的类别"
REASON_NO_CANDIDATES = "该维度没有匹配的序列"
REASON_NO_WINDOW = "事件日期之前的历史不足以构成对照窗口"


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


def _incomparable_formula(policy: str, reason: str) -> str:
    return f"incomparable under missing_policy={policy}: {reason}"


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


def _incomparable_explanation(dimension: str, value: str, policy: str, reason: str) -> str:
    return (
        f"{dimension}={value} 按 {policy} 口径不可比：{reason}。"
        "该类别不参与贡献额、贡献率与总变化计算。"
    )


@dataclass
class _SeriesView:
    """One candidate series plus its observation state inside both windows."""

    series: Series
    points: dict[date, Point]
    baseline_observed: int
    baseline_imputed: int
    comparison_observed: int
    comparison_imputed: int

    @property
    def dimension_value(self) -> str:
        return self.series.dimension_value


def _view(series: Series, windows: Windows) -> _SeriesView:
    points = {point.date: point for point in series.points}

    def count(window_dates: tuple[date, ...], imputed: bool) -> int:
        return sum(
            1
            for period in window_dates
            if period in points and points[period].imputed == imputed
        )

    return _SeriesView(
        series=series,
        points=points,
        baseline_observed=count(windows.baseline_dates, False),
        baseline_imputed=count(windows.baseline_dates, True),
        comparison_observed=count(windows.comparison_dates, False),
        comparison_imputed=count(windows.comparison_dates, True),
    )


def _coverage(observed: int, imputed: int, expected: int, used: int) -> dict[str, object]:
    return {
        "expected": expected,
        "observed": observed,
        "imputed": imputed,
        "missing": expected - observed - imputed,
        "used": used,
        "coverage": round(observed / expected, 6) if expected else None,
    }


def _view_coverage(view: _SeriesView, windows: Windows, used_baseline: int, used_comparison: int) -> dict[str, object]:
    return {
        "baseline": _coverage(
            view.baseline_observed, view.baseline_imputed,
            len(windows.baseline_dates), used_baseline,
        ),
        "comparison": _coverage(
            view.comparison_observed, view.comparison_imputed,
            len(windows.comparison_dates), used_comparison,
        ),
    }


def _merged_coverage(views: list[_SeriesView], windows: Windows, used_baseline: int, used_comparison: int) -> dict[str, object]:
    def merge(observed: str, imputed: str, expected: int, used: int) -> dict[str, object]:
        return _coverage(
            sum(getattr(view, observed) for view in views),
            sum(getattr(view, imputed) for view in views),
            expected * len(views),
            used * len(views),
        )

    return {
        "baseline": merge(
            "baseline_observed", "baseline_imputed",
            len(windows.baseline_dates), used_baseline,
        ),
        "comparison": merge(
            "comparison_observed", "comparison_imputed",
            len(windows.comparison_dates), used_comparison,
        ),
    }


def _ok_item(
    group: AnomalyGroup, dimension: str, value: str, rank: int, is_other: bool,
    current: float, baseline: float, total_change: float,
    coverage: dict[str, object],
) -> AttributionItem:
    contribution = current - baseline
    rate = _rate(contribution, total_change)
    return AttributionItem(
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
        is_other=is_other,
        formula=_formula(current, baseline, contribution, total_change),
        explanation=_explanation(dimension, value, current, baseline, contribution, rate, is_other),
        coverage=coverage,
    )


def _incomparable_item(
    group: AnomalyGroup, dimension: str, value: str, rank: int,
    reason: str, coverage: dict[str, object], cfg: AttributionConfig,
) -> AttributionItem:
    return AttributionItem(
        item_id=make_item_id(group.run_id, group.group_id, dimension, value, rank),
        group_id=group.group_id,
        event_id=group.events[0].event_id,
        attribution_dimension=dimension,
        dimension_value=value,
        current_value=None,
        baseline_value=None,
        contribution=None,
        contribution_rate=None,
        rank=rank,
        is_other=False,
        formula=_incomparable_formula(cfg.missing_policy, reason),
        explanation=_incomparable_explanation(dimension, value, cfg.missing_policy, reason),
        status="incomparable",
        status_reason=reason,
        coverage=coverage,
    )


@dataclass
class _Ranked:
    """A comparable row: summed values plus the view they came from."""

    view: _SeriesView
    current: float
    baseline: float

    @property
    def contribution(self) -> float:
        return self.current - self.baseline


def _rank_items(
    group: AnomalyGroup,
    rows: list[_Ranked],
    incomparable: list[tuple[_SeriesView, str]],
    windows: Windows,
    dimension: str,
    cfg: AttributionConfig,
    used_baseline: int,
    used_comparison: int,
) -> list[AttributionItem]:
    """Rank comparable rows, fold the remainder into 其他, append incomparable rows."""
    total_change = sum(row.contribution for row in rows)
    ordered = sorted(rows, key=lambda row: (-abs(row.contribution), row.view.dimension_value))
    selected = ordered[: cfg.top_n]
    remainder = ordered[cfg.top_n :]
    items: list[AttributionItem] = []
    for rank, row in enumerate(selected, start=1):
        items.append(
            _ok_item(
                group, dimension, row.view.dimension_value, rank, False,
                row.current, row.baseline, total_change,
                _view_coverage(row.view, windows, used_baseline, used_comparison),
            )
        )
    rank = len(selected) + 1
    if remainder:
        items.append(
            _ok_item(
                group, dimension, cfg.other_label, rank, True,
                sum(row.current for row in remainder),
                sum(row.baseline for row in remainder),
                total_change,
                _merged_coverage(
                    [row.view for row in remainder], windows, used_baseline, used_comparison
                ),
            )
        )
        rank += 1
    for view, reason in sorted(incomparable, key=lambda entry: entry[0].dimension_value):
        items.append(
            _incomparable_item(
                group, dimension, view.dimension_value, rank, reason,
                _view_coverage(view, windows, 0, 0), cfg,
            )
        )
        rank += 1
    return items


def _zero_fill_items(
    group: AnomalyGroup,
    views: list[_SeriesView],
    windows: Windows,
    dimension: str,
    cfg: AttributionConfig,
) -> tuple[list[AttributionItem], AttributionContext]:
    """缺测即零：累计完整日历窗口，未记录的日期按 0 计入。"""
    rows = [
        _Ranked(
            view=view,
            current=sum(
                view.points[period].value if period in view.points else 0.0
                for period in windows.comparison_dates
            ),
            baseline=sum(
                view.points[period].value if period in view.points else 0.0
                for period in windows.baseline_dates
            ),
        )
        for view in views
    ]
    items = _rank_items(
        group, rows, [], windows, dimension, cfg,
        used_baseline=len(windows.baseline_dates),
        used_comparison=len(windows.comparison_dates),
    )
    context = AttributionContext(
        attribution_dimension=dimension,
        missing_policy=cfg.missing_policy,
        status="ok",
        status_reason=None,
        baseline_dates=windows.baseline_dates,
        comparison_dates=windows.comparison_dates,
        total_change=sum(row.contribution for row in rows),
    )
    return items, context


def _absence_reason(view: _SeriesView) -> str:
    if not view.baseline_observed and not view.comparison_observed:
        return REASON_NO_OBSERVATIONS
    if not view.baseline_observed:
        return REASON_NO_BASELINE
    return REASON_NO_COMPARISON


def _common_dates_items(
    group: AnomalyGroup,
    views: list[_SeriesView],
    windows: Windows,
    dimension: str,
    cfg: AttributionConfig,
) -> tuple[list[AttributionItem], AttributionContext]:
    """共同日期交集：只在所有可比类别共同可观测的日期上归因。"""
    comparable: list[_SeriesView] = []
    incomparable: list[tuple[_SeriesView, str]] = []
    for view in views:
        if view.baseline_observed and view.comparison_observed:
            comparable.append(view)
        else:
            incomparable.append((view, _absence_reason(view)))

    def observed_dates(view: _SeriesView, window_dates: tuple[date, ...]) -> set[date]:
        return {
            period
            for period in window_dates
            if period in view.points and not view.points[period].imputed
        }

    common_baseline: set[date] = set()
    common_comparison: set[date] = set()
    dimension_reason: str | None = None
    if not comparable:
        dimension_reason = REASON_NO_COMPARABLE
    else:
        common_baseline = set.intersection(
            *(observed_dates(view, windows.baseline_dates) for view in comparable)
        )
        common_comparison = set.intersection(
            *(observed_dates(view, windows.comparison_dates) for view in comparable)
        )
        if not common_baseline:
            dimension_reason = REASON_NO_COMMON_BASELINE
        elif not common_comparison:
            dimension_reason = REASON_NO_COMMON_COMPARISON
    if dimension_reason is not None:
        incomparable = incomparable + [
            (view, dimension_reason) for view in comparable
        ]
        items = _rank_items(group, [], incomparable, windows, dimension, cfg, 0, 0)
        context = AttributionContext(
            attribution_dimension=dimension,
            missing_policy=cfg.missing_policy,
            status="incomparable",
            status_reason=dimension_reason,
            baseline_dates=(),
            comparison_dates=(),
            total_change=None,
        )
        return items, context
    baseline_dates = tuple(
        period for period in windows.baseline_dates if period in common_baseline
    )
    comparison_dates = tuple(
        period for period in windows.comparison_dates if period in common_comparison
    )
    rows = [
        _Ranked(
            view=view,
            current=sum(view.points[period].value for period in comparison_dates),
            baseline=sum(view.points[period].value for period in baseline_dates),
        )
        for view in comparable
    ]
    items = _rank_items(
        group, rows, incomparable, windows, dimension, cfg,
        used_baseline=len(baseline_dates),
        used_comparison=len(comparison_dates),
    )
    context = AttributionContext(
        attribution_dimension=dimension,
        missing_policy=cfg.missing_policy,
        status="ok",
        status_reason=None,
        baseline_dates=baseline_dates,
        comparison_dates=comparison_dates,
        total_change=sum(row.contribution for row in rows),
    )
    return items, context


def _dimension_items(
    group: AnomalyGroup,
    series_list: list[Series],
    timeline: list[date],
    dimension: str,
    cfg: AttributionConfig,
) -> tuple[list[AttributionItem], AttributionContext]:
    windows = resolve_windows(timeline, group.date, cfg)
    if windows is None:
        return [], AttributionContext(
            attribution_dimension=dimension,
            missing_policy=cfg.missing_policy,
            status="incomparable",
            status_reason=REASON_NO_WINDOW,
            baseline_dates=(),
            comparison_dates=(),
            total_change=None,
        )
    candidates = [
        series
        for series in series_list
        if series.metric == group.metric and series.dimension_name == dimension
    ]
    if not candidates:
        return [], AttributionContext(
            attribution_dimension=dimension,
            missing_policy=cfg.missing_policy,
            status="incomparable",
            status_reason=REASON_NO_CANDIDATES,
            baseline_dates=(),
            comparison_dates=(),
            total_change=None,
        )
    views = [
        _view(series, windows)
        for series in sorted(candidates, key=lambda item: item.dimension_value)
    ]
    if cfg.missing_policy == "common_dates":
        return _common_dates_items(group, views, windows, dimension, cfg)
    return _zero_fill_items(group, views, windows, dimension, cfg)


def attribute_groups(
    groups: list[AnomalyGroup],
    series_list: list[Series],
    timeline: list[date],
    cfg: AttributionConfig,
) -> list[AttributionItem]:
    all_items: list[AttributionItem] = []
    for group in groups:
        items: list[AttributionItem] = []
        contexts: dict[str, AttributionContext] = {}
        for dimension in cfg.dimensions:
            dimension_items, context = _dimension_items(
                group, series_list, timeline, dimension, cfg
            )
            items.extend(dimension_items)
            contexts[dimension] = context
        group.attribution_items = items
        group.attribution_contexts = contexts
        apply_group_narrative(group)
        all_items.extend(items)
    return all_items
