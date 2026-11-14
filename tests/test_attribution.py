from datetime import date, timedelta

import pytest

from drift_attr.attribution import attribute_groups, resolve_windows
from drift_attr.config import AttributionConfig
from helpers import make_event, make_group, make_series


def timeline(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def test_top_n_other_sorting_and_rates():
    start = date(2025, 1, 1)
    series = [
        make_series([10, 10, 10, 20, 20, 0], dimension_value="region_alpha", start=start),
        make_series([0, 5, 3, 1, 1, 0], dimension_value="region_beta", start=start),
        make_series([0, 10, 10, 0, 0, 0], dimension_value="region_gamma", start=start),
        make_series([0, 0, 0, 5, 5, 0], dimension_value="region_delta", start=start),
    ]
    group = make_group(make_event(day=date(2025, 1, 5)))
    cfg = AttributionConfig(
        dimensions=("channel",), top_n=2, baseline_window=2,
        comparison_window=2, other_label="其他",
    )
    items = attribute_groups([group], series, timeline(start, 6), cfg)
    assert [(item.rank, item.dimension_value, item.is_other) for item in items] == [
        (1, "region_alpha", False),
        (2, "region_gamma", False),
        (3, "其他", True),
    ]
    alpha, gamma, other = items
    assert (alpha.contribution, alpha.contribution_rate) == (20, 5.0)
    assert (gamma.contribution, gamma.contribution_rate) == (-20, -5.0)
    assert (other.contribution, other.contribution_rate) == (4, 1.0)
    assert other.current_value == 12 and other.baseline_value == 8
    assert "current" in alpha.formula and "baseline" in alpha.formula
    assert "异常窗口" in alpha.explanation
    assert "其他维度合计" in other.explanation
    assert group.narrative and "channel" in group.narrative


def test_total_change_zero_has_no_rate():
    start = date(2025, 2, 1)
    series = [
        make_series([10, 10, 20, 20], dimension_value="region_alpha", start=start),
        make_series([10, 10, 0, 0], dimension_value="region_beta", start=start),
    ]
    group = make_group(make_event(day=date(2025, 2, 4)))
    cfg = AttributionConfig(("channel",), 3, 2, 2, "其他")
    items = attribute_groups([group], series, timeline(start, 4), cfg)
    assert {item.contribution for item in items} == {20, -20}
    assert all(item.contribution_rate is None for item in items)
    assert all("undefined" in item.formula for item in items)
    assert "不适用" in group.narrative


def test_missing_dimension_values_are_treated_as_zero():
    start = date(2025, 3, 1)
    series = [
        make_series([5, 5, 5, 5], dimension_value="region_alpha", start=start),
        make_series([0, 10, 20, 0], dimension_value="region_beta", start=start),
    ]
    group = make_group(make_event(day=date(2025, 3, 4)))
    cfg = AttributionConfig(("channel",), 5, 2, 2, "其他")
    items = attribute_groups([group], series, timeline(start, 4), cfg)
    alpha = next(item for item in items if item.dimension_value == "region_alpha")
    assert (alpha.current_value, alpha.baseline_value, alpha.contribution) == (10, 10, 0)
    assert alpha.contribution_rate == 0.0


def test_no_dimension_candidates_and_windows():
    start = date(2025, 4, 1)
    group = make_group(make_event(day=date(2025, 4, 4)))
    cfg = AttributionConfig(("region",), 3, 2, 2, "其他")
    assert attribute_groups([group], [make_series([1, 2, 3, 4])], timeline(start, 4), cfg) == []
    windows = resolve_windows(timeline(start, 4), date(2025, 4, 4), cfg)
    assert windows is not None
    assert (windows.comparison_start, windows.comparison_end) == (date(2025, 4, 3), date(2025, 4, 4))
    assert (windows.baseline_start, windows.baseline_end) == (date(2025, 4, 1), date(2025, 4, 2))
    assert resolve_windows(timeline(start, 4), date(2024, 12, 31), cfg) is None
    assert group.attribution_items == []


def test_top_n_larger_than_dimensions_has_no_other():
    start = date(2025, 5, 1)
    group = make_group(make_event(day=date(2025, 5, 3)))
    cfg = AttributionConfig(("channel",), 10, 1, 2, "其他")
    items = attribute_groups(
        [group],
        [
            make_series([5, 5, 6], dimension_value="region_alpha", start=start),
            make_series([1, 2, 3], dimension_value="region_beta", start=start),
        ],
        timeline(start, 3),
        cfg,
    )
    assert len(items) == 2
    assert not any(item.is_other for item in items)
    assert sum(item.contribution for item in items) == pytest.approx(10)
