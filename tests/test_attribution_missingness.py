"""Missing-data attribution policies: true zero vs imputed vs unobserved."""

from datetime import date, timedelta

import pytest

from drift_attr import store
from drift_attr.attribution import (
    REASON_NO_BASELINE,
    REASON_NO_COMMON_BASELINE,
    REASON_NO_COMPARABLE,
    REASON_NO_COMPARISON,
    attribute_groups,
)
from drift_attr.config import AttributionConfig, DatasetConfig, SeriesConfig, load_config
from drift_attr.dataset import load_dataset
from drift_attr.engine import analyze
from drift_attr.errors import DriftError
from drift_attr.schema import Point, Series, item_to_dict
from drift_attr.timeseries import build_series
from drift_attr.web import create_app
from helpers import config_dict, make_event, make_group, write_config, write_csv

START = date(2025, 1, 1)


def timeline(count: int, start: date = START) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def sparse_series(
    entries: list[tuple[int, float, bool]],
    *,
    metric: str = "orders",
    dimension_name: str = "channel",
    dimension_value: str = "alpha",
    start: date = START,
) -> Series:
    """Build a series from (day_offset, value, imputed) triples; absent offsets stay unobserved."""
    return Series(
        metric=metric,
        dimension_name=dimension_name,
        dimension_value=dimension_value,
        points=[
            Point(start + timedelta(days=offset), value, imputed)
            for offset, value, imputed in entries
        ],
    )


def full_series(values: list[float], *, dimension_value: str = "alpha", start: date = START) -> Series:
    return sparse_series(
        [(index, value, False) for index, value in enumerate(values)],
        dimension_value=dimension_value,
        start=start,
    )


def make_cfg(policy: str = "zero_fill", *, top_n: int = 5, baseline: int = 4, comparison: int = 4) -> AttributionConfig:
    return AttributionConfig(
        dimensions=("channel",),
        top_n=top_n,
        baseline_window=baseline,
        comparison_window=comparison,
        other_label="其他",
        missing_policy=policy,
    )


def group_on(day: date):
    return make_group(make_event(day=day))


def by_value(items):
    return {item.dimension_value: item for item in items}


def test_zero_fill_distinguishes_true_zero_from_missing():
    # alpha records real 0.0 in the comparison window; beta has no records there.
    series = [
        sparse_series([(0, 5, False), (1, 5, False), (2, 0.0, False), (3, 0.0, False)], dimension_value="alpha"),
        sparse_series([(0, 5, False), (1, 5, False)], dimension_value="beta"),
    ]
    group = group_on(date(2025, 1, 4))
    items = attribute_groups(
        [group], series, timeline(4), make_cfg(baseline=2, comparison=2)
    )
    assert group.attribution_contexts["channel"].status == "ok"
    views = by_value(items)
    alpha, beta = views["alpha"], views["beta"]
    # 缺测即零口径下数值相同，但覆盖统计把“真零”与“无观测”分开。
    assert (alpha.current_value, alpha.baseline_value, alpha.contribution) == (0.0, 10.0, -10.0)
    assert (beta.current_value, beta.baseline_value, beta.contribution) == (0.0, 10.0, -10.0)
    assert alpha.status == "ok" and beta.status == "ok"
    assert alpha.coverage["comparison"] == {
        "expected": 2, "observed": 2, "imputed": 0, "missing": 2 - 2,
        "used": 2, "coverage": 1.0,
    }
    assert beta.coverage["comparison"] == {
        "expected": 2, "observed": 0, "imputed": 0, "missing": 2,
        "used": 2, "coverage": 0.0,
    }
    # 真零不得变成未知：alpha 的异常窗口值是真实的 0.0 而不是 None。
    assert alpha.current_value == 0.0
    payload = item_to_dict(beta)
    assert payload["status"] == "ok"
    assert payload["coverage"]["comparison"]["missing"] == 2


def test_zero_fill_uses_imputed_values_but_flags_them():
    series = [
        sparse_series(
            [(0, 5, False), (1, 5, False), (2, 7, True), (3, 7, False)],
            dimension_value="alpha",
        ),
    ]
    group = group_on(date(2025, 1, 4))
    items = attribute_groups(
        [group], series, timeline(4), make_cfg(baseline=2, comparison=2)
    )
    (alpha,) = items
    assert alpha.current_value == 14.0  # 插补值计入求和
    assert alpha.coverage["comparison"]["observed"] == 1
    assert alpha.coverage["comparison"]["imputed"] == 1
    assert alpha.coverage["comparison"]["missing"] == 0


def test_policies_lead_to_opposite_business_readings():
    # 基期 4 天里甲缺 2 天，当前 4 天里乙缺 2 天（任务书示例）。
    alpha = sparse_series(
        [(0, 10, False), (2, 10, False)]
        + [(offset, 10, False) for offset in range(4, 8)],
        dimension_value="alpha",
    )
    beta = sparse_series(
        [(offset, 4, False) for offset in range(4)]
        + [(5, 4, False), (7, 4, False)],
        dimension_value="beta",
    )
    days = timeline(8)
    zero_items = attribute_groups(
        [group_on(date(2025, 1, 8))], [alpha, beta], days, make_cfg("zero_fill")
    )
    zero = by_value(zero_items)
    # 缺测即零：甲的基期被缺测低估而显得上升，乙的当前被低估而显得下降。
    assert zero["alpha"].contribution == 40.0 - 20.0
    assert zero["beta"].contribution == 8.0 - 16.0
    assert zero["alpha"].rank == 1

    common_group = group_on(date(2025, 1, 8))
    common_items = attribute_groups(
        [common_group], [alpha, beta], days, make_cfg("common_dates")
    )
    context = common_group.attribution_contexts["channel"]
    # 共同日期交集：只剩基期 {d0,d2} 与当前 {d5,d7}，两类别贡献都变为 0。
    assert context.status == "ok"
    assert context.baseline_dates == (date(2025, 1, 1), date(2025, 1, 3))
    assert context.comparison_dates == (date(2025, 1, 6), date(2025, 1, 8))
    assert context.total_change == 0.0
    assert {item.contribution for item in common_items} == {0.0}
    assert all(item.contribution_rate is None for item in common_items)
    common = by_value(common_items)
    assert common["alpha"].coverage["baseline"] == {
        "expected": 4, "observed": 2, "imputed": 0, "missing": 2,
        "used": 2, "coverage": 0.5,
    }
    assert common["beta"].coverage["comparison"]["used"] == 2


def test_common_dates_single_shared_day_flips_ranking():
    # zero_fill 看 alpha 暴涨 +609；共同交集只剩 1 天后 beta 才是主导（+99 vs +9）。
    alpha = sparse_series(
        [(0, 1, False), (4, 10, False), (5, 200, False), (6, 200, False), (7, 200, False)],
        dimension_value="alpha",
    )
    beta = sparse_series(
        [(0, 1, False), (1, 1, False), (2, 1, False), (3, 1, False), (4, 100, False)],
        dimension_value="beta",
    )
    days = timeline(8)
    zero_items = attribute_groups(
        [group_on(date(2025, 1, 8))], [alpha, beta], days, make_cfg("zero_fill")
    )
    assert [item.dimension_value for item in zero_items] == ["alpha", "beta"]
    assert zero_items[0].contribution == 609.0

    common_group = group_on(date(2025, 1, 8))
    common_items = attribute_groups(
        [common_group], [alpha, beta], days, make_cfg("common_dates")
    )
    context = common_group.attribution_contexts["channel"]
    assert context.baseline_dates == (date(2025, 1, 1),)
    assert context.comparison_dates == (date(2025, 1, 5),)
    assert [item.dimension_value for item in common_items] == ["beta", "alpha"]
    assert common_items[0].contribution == 99.0
    assert common_items[1].contribution == 9.0
    assert context.total_change == 108.0


def test_common_dates_marks_new_and_disappeared_categories():
    stable = full_series([10, 10, 10, 10, 15, 15, 15, 15], dimension_value="stable")
    new_cat = sparse_series(
        [(4, 8, False), (5, 8, False), (6, 8, False), (7, 8, False)],
        dimension_value="new_cat",
    )
    gone_cat = sparse_series(
        [(0, 5, False), (1, 5, False), (2, 5, False), (3, 5, False)],
        dimension_value="gone_cat",
    )
    days = timeline(8)
    group = group_on(date(2025, 1, 8))
    items = attribute_groups(
        [group], [stable, new_cat, gone_cat], days, make_cfg("common_dates")
    )
    views = by_value(items)
    stable_item, new_item, gone_item = views["stable"], views["new_cat"], views["gone_cat"]
    assert stable_item.status == "ok"
    assert stable_item.contribution == 20.0
    # 总变化只由可比类别构成：未知值不暗中补零，贡献率仍能闭合到 100%。
    assert stable_item.contribution_rate == 1.0
    assert group.attribution_contexts["channel"].total_change == 20.0
    for item, reason in ((new_item, REASON_NO_BASELINE), (gone_item, REASON_NO_COMPARISON)):
        assert item.status == "incomparable"
        assert item.status_reason == reason
        assert item.current_value is None
        assert item.baseline_value is None
        assert item.contribution is None
        assert item.contribution_rate is None
        assert "incomparable" in item.formula
    assert new_item.coverage["baseline"]["observed"] == 0
    assert new_item.coverage["comparison"]["observed"] == 4
    assert [item.rank for item in items] == [1, 2, 3]

    # 同一数据在缺测即零口径下会伪造出 +32 / -20 的“业务变化”。
    zero_items = attribute_groups(
        [group_on(date(2025, 1, 8))], [stable, new_cat, gone_cat], days, make_cfg("zero_fill")
    )
    zero = by_value(zero_items)
    assert zero["new_cat"].contribution == 32.0
    assert zero["new_cat"].baseline_value == 0.0
    assert zero["new_cat"].coverage["baseline"]["missing"] == 4
    assert zero["gone_cat"].contribution == -20.0


def test_common_dates_empty_intersection_is_incomparable():
    alpha = sparse_series(
        [(0, 7, False)] + [(offset, 3, False) for offset in range(4, 8)],
        dimension_value="alpha",
    )
    beta = sparse_series(
        [(1, 9, False)] + [(offset, 4, False) for offset in range(4, 8)],
        dimension_value="beta",
    )
    group = group_on(date(2025, 1, 8))
    items = attribute_groups(
        [group], [alpha, beta], timeline(8), make_cfg("common_dates")
    )
    context = group.attribution_contexts["channel"]
    assert context.status == "incomparable"
    assert context.status_reason == REASON_NO_COMMON_BASELINE
    assert context.baseline_dates == () and context.comparison_dates == ()
    assert context.total_change is None
    assert [item.rank for item in items] == [1, 2]
    for item in items:
        assert item.status == "incomparable"
        assert item.status_reason == REASON_NO_COMMON_BASELINE
        assert item.current_value is None
        assert item.contribution is None
        assert item.coverage["baseline"]["used"] == 0
    assert "不可比" in group.narrative


def test_common_dates_without_any_comparable_category():
    alpha = sparse_series([(offset, 5, False) for offset in range(4)], dimension_value="alpha")
    beta = sparse_series(
        [(offset, 5, False) for offset in range(4, 8)], dimension_value="beta"
    )
    group = group_on(date(2025, 1, 8))
    items = attribute_groups(
        [group], [alpha, beta], timeline(8), make_cfg("common_dates")
    )
    context = group.attribution_contexts["channel"]
    assert context.status == "incomparable"
    assert context.status_reason == REASON_NO_COMPARABLE
    views = by_value(items)
    assert views["alpha"].status_reason == REASON_NO_COMPARISON
    assert views["beta"].status_reason == REASON_NO_BASELINE
    assert all(item.contribution is None for item in items)


def test_positive_negative_cancel_out_under_common_dates():
    alpha = full_series([10, 10, 10, 10, 20, 20, 20, 20], dimension_value="alpha")
    beta = full_series([20, 20, 20, 20, 10, 10, 10, 10], dimension_value="beta")
    group = group_on(date(2025, 1, 8))
    items = attribute_groups(
        [group], [alpha, beta], timeline(8), make_cfg("common_dates")
    )
    assert {item.contribution for item in items} == {40.0, -40.0}
    assert group.attribution_contexts["channel"].total_change == 0.0
    assert group.attribution_contexts["channel"].status == "ok"
    assert all(item.contribution_rate is None for item in items)
    assert all("undefined" in item.formula for item in items)
    assert "不适用" in group.narrative


def test_other_item_aggregates_mixed_coverage():
    alpha = full_series([5, 5, 10, 10], dimension_value="alpha")
    beta = full_series([5, 5, 8, 8], dimension_value="beta")
    gamma = sparse_series(
        [(0, 3, False), (1, 3, False), (2, 4, False)], dimension_value="gamma"
    )
    delta = sparse_series(
        [(0, 2, False), (2, 1, False), (3, 1, False)], dimension_value="delta"
    )
    days = timeline(4)
    cfg = make_cfg("zero_fill", top_n=2, baseline=2, comparison=2)
    items = attribute_groups([group_on(date(2025, 1, 4))], [alpha, beta, gamma, delta], days, cfg)
    other = next(item for item in items if item.is_other)
    assert (other.current_value, other.baseline_value, other.contribution) == (6.0, 8.0, -2.0)
    assert other.contribution_rate == pytest.approx(-2.0 / 14.0)
    assert other.coverage["baseline"] == {
        "expected": 4, "observed": 3, "imputed": 0, "missing": 1,
        "used": 4, "coverage": 0.75,
    }
    assert other.coverage["comparison"] == {
        "expected": 4, "observed": 3, "imputed": 0, "missing": 1,
        "used": 4, "coverage": 0.75,
    }

    common_group = group_on(date(2025, 1, 4))
    common_cfg = make_cfg("common_dates", top_n=2, baseline=2, comparison=2)
    common_items = attribute_groups(
        [common_group], [alpha, beta, gamma, delta], days, common_cfg
    )
    context = common_group.attribution_contexts["channel"]
    assert context.baseline_dates == (date(2025, 1, 1),)
    assert context.comparison_dates == (date(2025, 1, 3),)
    common_other = next(item for item in common_items if item.is_other)
    # 交集口径下“其他”项只累计共同日期：2 个类别 × 1 天。
    assert common_other.coverage["baseline"]["used"] == 2
    assert common_other.coverage["comparison"]["used"] == 2
    assert common_other.coverage["baseline"]["observed"] == 3
    assert (common_other.current_value, common_other.baseline_value) == (5.0, 5.0)


def test_default_policy_and_full_observation_stay_compatible():
    series = [
        full_series([10, 10, 20, 20], dimension_value="alpha"),
        full_series([0, 5, 3, 1], dimension_value="beta"),
    ]
    days = timeline(4)
    legacy_cfg = AttributionConfig(("channel",), 3, 2, 2, "其他")
    assert legacy_cfg.missing_policy == "zero_fill"
    legacy_items = attribute_groups([group_on(date(2025, 1, 4))], series, days, legacy_cfg)
    common_group = group_on(date(2025, 1, 4))
    common_items = attribute_groups(
        [common_group], series, days, make_cfg("common_dates", baseline=2, comparison=2)
    )
    key = lambda item: (item.dimension_value, item.current_value, item.baseline_value,
                        item.contribution, item.contribution_rate, item.rank, item.is_other)
    assert [key(item) for item in legacy_items] == [key(item) for item in common_items]
    context = common_group.attribution_contexts["channel"]
    assert context.status == "ok"
    assert context.baseline_dates == tuple(days[:2])
    assert context.comparison_dates == tuple(days[2:])
    for item in common_items:
        assert item.status == "ok"
        assert item.coverage["baseline"]["coverage"] == 1.0
        assert item.coverage["comparison"]["coverage"] == 1.0


def test_missing_policy_config_parsing(tmp_path):
    payload = config_dict(
        [{"name": "r", "type": "threshold", "metrics": ["orders"],
          "direction": "below", "lower": 1}],
        attribution={
            "dimensions": ["channel"], "top_n": 3,
            "baseline_window": 2, "comparison_window": 2,
            "missing_policy": "common_dates",
        },
    )
    cfg = load_config(write_config(tmp_path / "rules.yaml", payload))
    assert cfg.attribution.missing_policy == "common_dates"

    payload["attribution"]["missing_policy"] = "bogus"
    with pytest.raises(DriftError):
        load_config(write_config(tmp_path / "bad.yaml", payload))

    del payload["attribution"]["missing_policy"]
    cfg = load_config(write_config(tmp_path / "default.yaml", payload))
    assert cfg.attribution.missing_policy == "zero_fill"


def test_previous_fill_marks_imputed_through_pipeline(tmp_path):
    path = write_csv(
        tmp_path / "data.csv",
        "2025-01-01,orders,channel,alpha,10\n"
        "2025-01-03,orders,channel,alpha,30\n"
        "2025-01-01,orders,channel,beta,5\n"
        "2025-01-02,orders,channel,beta,6\n"
        "2025-01-03,orders,channel,beta,7\n",
    )
    dataset = load_dataset(DatasetConfig(
        path=str(path), format="long", encoding="utf-8", duplicate_policy="warn",
        empty_value="error", date_field="date", metric_field="metric",
        dimension_name_field="dimension_name", dimension_value_field="dimension_value",
        value_field="value",
    ))
    build = build_series(dataset, SeriesConfig("day", "sum", "previous", "monday"))
    alpha = next(series for series in build.series if series.dimension_value == "alpha")
    assert [(point.value, point.imputed) for point in alpha.points] == [
        (10.0, False), (10.0, True), (30.0, False)
    ]
    cfg = make_cfg("common_dates", baseline=1, comparison=1)
    group = group_on(date(2025, 1, 3))
    items = attribute_groups([group], build.series, build.timeline, cfg)
    views = by_value(items)
    # previous 插补值不算真实观测：alpha 在基期只有插补点，被判为不可比。
    assert views["alpha"].status == "incomparable"
    assert views["alpha"].status_reason == REASON_NO_BASELINE
    assert views["alpha"].coverage["baseline"]["imputed"] == 1
    assert views["beta"].status == "ok"
    assert (views["beta"].baseline_value, views["beta"].current_value) == (6.0, 7.0)

    zero_items = attribute_groups(
        [group_on(date(2025, 1, 3))], build.series, build.timeline,
        make_cfg("zero_fill", baseline=1, comparison=1),
    )
    zero_alpha = by_value(zero_items)["alpha"]
    assert zero_alpha.baseline_value == 10.0  # 插补值被使用但被标记
    assert zero_alpha.coverage["baseline"]["imputed"] == 1
    assert zero_alpha.coverage["baseline"]["observed"] == 0


def _write_sparse_project(tmp_path, policy: str | None) -> None:
    rows = []
    days = [date(2025, 3, 1) + timedelta(days=index) for index in range(8)]
    for index, day in enumerate(days):
        rows.append(f"{day},orders,channel,alpha,{10 if index == 7 else 100}")
    for index in (0, 3, 4, 6, 7):
        rows.append(f"{days[index]},orders,channel,beta,{20 if index == 7 else 100}")
    for index in (4, 5, 6, 7):
        rows.append(f"{days[index]},orders,channel,gamma,{30 if index == 7 else 100}")
    write_csv(tmp_path / "data.csv", "\n".join(rows) + "\n")
    attribution = {
        "dimensions": ["channel"], "top_n": 3,
        "baseline_window": 4, "comparison_window": 4,
    }
    if policy:
        attribution["missing_policy"] = policy
    payload = config_dict(
        [{
            "name": "low_orders", "type": "threshold", "metrics": ["orders"],
            "direction": "below", "lower": 45,
        }],
        attribution=attribution,
        series={"fill_missing": "drop"},
    )
    write_config(tmp_path / "rules.yaml", payload)


def test_end_to_end_report_shows_policy_dates_and_coverage(tmp_path):
    _write_sparse_project(tmp_path, "common_dates")
    result = analyze(tmp_path / "rules.yaml", run_id="missing-run", base_dir=tmp_path)
    report = result.report
    assert report["attribution_policy"]["missing_policy"] == "common_dates"
    assert "共同可观测" in report["attribution_policy"]["missing_policy_note"]

    groups = [group for group in report["anomalies"] if group["date"] == "2025-03-08"]
    assert len(groups) == 3  # alpha / beta / gamma 各自命中阈值
    group = groups[0]
    context = group["attribution_contexts"]["channel"]
    assert context["status"] == "ok"
    assert context["baseline_dates"] == ["2025-03-01", "2025-03-04"]
    assert context["comparison_dates"] == ["2025-03-05", "2025-03-07", "2025-03-08"]
    assert context["total_change"] == 30.0

    items = {item["dimension_value"]: item for item in group["attribution"]["channel"]}
    assert items["alpha"]["contribution"] == 10.0
    assert items["beta"]["contribution"] == 20.0
    assert items["alpha"]["coverage"]["comparison"] == {
        "expected": 4, "observed": 4, "imputed": 0, "missing": 0,
        "used": 3, "coverage": 1.0,
    }
    assert items["beta"]["coverage"]["baseline"]["observed"] == 2
    gamma = items["gamma"]
    assert gamma["status"] == "incomparable"
    assert gamma["status_reason"] == REASON_NO_BASELINE
    assert gamma["contribution"] is None
    assert gamma["coverage"]["baseline"] == {
        "expected": 4, "observed": 0, "imputed": 0, "missing": 4,
        "used": 0, "coverage": 0.0,
    }

    markdown = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "缺测口径" in markdown
    assert "实际归因日期" in markdown
    assert "2025-03-04" in markdown  # 双方实际日期集合可见
    assert "不可比" in markdown
    html = (result.run_dir / "report.html").read_text(encoding="utf-8")
    assert "不可比" in html and "覆盖" in html

    stored = {
        item["dimension_value"]: item
        for item in store.get_attribution_items(result.database_path, result.run_id)
        if item["dimension_value"] == "gamma"
    }
    assert stored and all(item["contribution"] is None for item in stored.values())

    app = create_app(result.database_path, tmp_path / "output", tmp_path)
    detail = app.test_client().get(f"/runs/{result.run_id}")
    assert detail.status_code == 200
    assert "不可比" in detail.get_data(as_text=True)


def test_end_to_end_zero_fill_fills_same_gaps_with_zero(tmp_path):
    _write_sparse_project(tmp_path, None)  # 默认口径：缺测即零
    result = analyze(tmp_path / "rules.yaml", run_id="zero-run", base_dir=tmp_path)
    assert result.report["attribution_policy"]["missing_policy"] == "zero_fill"
    group = next(
        group for group in result.report["anomalies"] if group["date"] == "2025-03-08"
    )
    context = group["attribution_contexts"]["channel"]
    assert context["baseline_dates"] == [
        "2025-03-01", "2025-03-02", "2025-03-03", "2025-03-04"
    ]
    items = {item["dimension_value"]: item for item in group["attribution"]["channel"]}
    gamma = items["gamma"]
    # 同一批数据：默认口径把 gamma 缺失的基期当作 0，得出“新增 +330”的结论。
    assert gamma["status"] == "ok"
    assert gamma["baseline_value"] == 0.0
    assert gamma["current_value"] == 330.0
    assert gamma["contribution"] == 330.0
    assert gamma["coverage"]["baseline"]["observed"] == 0
    assert gamma["coverage"]["baseline"]["missing"] == 4
