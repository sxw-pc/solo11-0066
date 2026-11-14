from drift_attr.config import DatasetConfig, SeriesConfig
from drift_attr.dataset import load_dataset
from drift_attr.timeseries import bucket_date, build_series, build_timeline
from helpers import write_csv


def make_data(tmp_path, body):
    path = write_csv(tmp_path / "data.csv", body)
    cfg = DatasetConfig(
        path=str(path), format="long", encoding="utf-8", duplicate_policy="warn",
        empty_value="error", date_field="date", metric_field="metric",
        dimension_name_field="dimension_name", dimension_value_field="dimension_value",
        value_field="value",
    )
    return load_dataset(cfg)


def test_fill_zero_and_previous_and_drop(tmp_path):
    data = make_data(
        tmp_path,
        "2025-01-01,orders,channel,region_alpha,10\n"
        "2025-01-03,orders,channel,region_alpha,30\n",
    )
    zero = SeriesConfig("day", "sum", "zero", "monday")
    result = build_series(data, zero)
    assert [(p.value, p.imputed) for p in result.series[0].points] == [
        (10, False), (0, True), (30, False)
    ]
    assert result.imputed_points == 1
    previous = build_series(data, SeriesConfig("day", "sum", "previous", "monday"))
    assert [p.value for p in previous.series[0].points] == [10, 10, 30]
    dropped = build_series(data, SeriesConfig("day", "sum", "drop", "monday"))
    assert [p.value for p in dropped.series[0].points] == [10, 30]
    assert dropped.timeline[0].isoformat() == "2025-01-01"


def test_sum_mean_and_series_sorting(tmp_path):
    data = make_data(
        tmp_path,
        "2025-01-01,orders,channel,region_beta,4\n"
        "2025-01-01,orders,channel,region_beta,6\n"
        "2025-01-01,visits,channel,region_alpha,2\n"
        "2025-01-01,visits,channel,region_alpha,4\n",
    )
    summed = build_series(data, SeriesConfig("day", "sum", "zero", "monday"))
    assert [(s.metric, s.dimension_value, s.points[0].value) for s in summed.series] == [
        ("orders", "region_beta", 10.0), ("visits", "region_alpha", 6.0)
    ]
    averaged = build_series(data, SeriesConfig("day", "mean", "zero", "monday"))
    assert [s.points[0].value for s in averaged.series] == [5.0, 3.0]


def test_weekly_buckets_and_date_fill(tmp_path):
    data = make_data(
        tmp_path,
        "2025-01-06,orders,channel,region_alpha,1\n"
        "2025-01-08,orders,channel,region_alpha,2\n"
        "2025-01-20,orders,channel,region_alpha,4\n",
    )
    cfg = SeriesConfig("week", "sum", "zero", "monday")
    result = build_series(data, cfg)
    assert [period.isoformat() for period in result.timeline] == [
        "2025-01-06", "2025-01-13", "2025-01-20"
    ]
    assert [(p.value, p.imputed) for p in result.series[0].points] == [
        (3.0, False), (0.0, True), (4.0, False)
    ]
    assert bucket_date(bucket_date.__defaults__[0] if False else __import__("datetime").date(2025, 1, 8), cfg).isoformat() == "2025-01-06"
    sunday = SeriesConfig("week", "sum", "zero", "sunday")
    assert bucket_date(__import__("datetime").date(2025, 1, 8), sunday).isoformat() == "2025-01-05"


def test_leading_previous_is_dropped_with_warning(tmp_path):
    data = make_data(
        tmp_path,
        "2025-01-02,orders,channel,region_alpha,10\n"
        "2025-01-03,orders,channel,region_alpha,20\n",
    )
    result = build_series(data, SeriesConfig("day", "sum", "previous", "monday"))
    assert len(result.timeline) == 2
    assert result.series[0].points[0].date.isoformat() == "2025-01-02"
    assert build_timeline(data, SeriesConfig("day", "sum", "zero", "monday"))[0].isoformat() == "2025-01-02"
