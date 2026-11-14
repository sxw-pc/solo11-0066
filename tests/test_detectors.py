import pytest

from drift_attr.detectors.changepoint import ChangepointDetector
from drift_attr.detectors.delta import DeltaDetector
from drift_attr.detectors.robust_z import RobustZDetector
from drift_attr.detectors.threshold import ThresholdDetector
from drift_attr.errors import DriftError
from helpers import make_series


def test_threshold_below_above_and_boundary():
    series = make_series([4, 5, 6, 11])
    below = ThresholdDetector()
    hits = below.detect(series, below.validate_params(
        {"direction": "below", "lower": 5}, "rule"
    ))
    assert [(hit.date.day, hit.observed) for hit in hits] == [(1, 4)]
    above = below.validate_params({"direction": "above", "upper": 10}, "rule")
    assert [hit.observed for hit in below.detect(series, above)] == [11]
    both = below.validate_params({"direction": "both", "lower": 5, "upper": 10}, "rule")
    assert [hit.observed for hit in below.detect(series, both)] == [4, 11]


def test_threshold_validation_errors():
    detector = ThresholdDetector()
    with pytest.raises(DriftError) as exc:
        detector.validate_params({"direction": "below"}, "rule")
    assert exc.value.code == "E_CONFIG_SCHEMA"
    with pytest.raises(DriftError):
        detector.validate_params({"lower": 8, "upper": 3, "direction": "both"}, "rule")


def test_delta_relative_absolute_and_zero_baseline():
    detector = DeltaDetector()
    relative = detector.validate_params({"threshold": 0.25, "mode": "relative"}, "rule")
    hits = detector.detect(make_series([100, 150, 160, 170]), relative)
    assert [hit.date.day for hit in hits] == [2]
    assert hits[0].score == pytest.approx(0.5)
    assert detector.detect(make_series([0, 50]), relative) == []
    absolute = detector.validate_params(
        {"threshold": 20, "mode": "absolute", "direction": "decrease"}, "rule"
    )
    assert [hit.observed for hit in detector.detect(make_series([100, 150, 120]), absolute)] == [120]
    seasonal = detector.validate_params(
        {"threshold": 0.1, "comparison": "seasonal", "lag": 2}, "rule"
    )
    assert seasonal["lag"] == 2


def test_delta_previous_lag_must_be_one():
    with pytest.raises(DriftError):
        DeltaDetector.validate_params(
            {"threshold": 0.1, "comparison": "previous", "lag": 2}, "rule"
        )


def test_robust_z_hit_and_insufficient_history():
    detector = RobustZDetector()
    params = detector.validate_params({"threshold": 3.5, "history": 5}, "rule")
    series = make_series([8, 10, 12, 10, 8, 100])
    hits = detector.detect(series, params)
    assert len(hits) == 1
    assert hits[0].score > 20
    assert hits[0].baseline == 10
    assert detector.detect(make_series([1, 2, 3]), params) == []


def test_robust_z_mad_zero_skip_and_floor():
    detector = RobustZDetector()
    series = make_series([10, 10, 10, 10, 10, 100])
    skipped = detector.validate_params(
        {"threshold": 3.5, "history": 5, "mad_zero": "skip"}, "rule"
    )
    assert detector.detect(series, skipped) == []
    floored = detector.validate_params(
        {"threshold": 3.5, "history": 5, "mad_zero": "floor", "floor_scale": 2}, "rule"
    )
    hits = detector.detect(series, floored)
    assert len(hits) == 1
    assert hits[0].score == pytest.approx(45)
    assert hits[0].evidence["mad_zero_handled"] is True


def test_changepoint_detects_single_level_shift():
    detector = ChangepointDetector()
    params = detector.validate_params(
        {"threshold": 2, "pre_window": 3, "post_window": 3, "direction": "increase"}, "rule"
    )
    hits = detector.detect(make_series([10] * 8 + [30] * 8), params)
    assert len(hits) == 1
    assert hits[0].date.day == 9
    assert hits[0].observed == 30
    assert hits[0].baseline == 10
    down = detector.validate_params(
        {"threshold": 2, "pre_window": 3, "post_window": 3, "direction": "decrease"},
        "rule",
    )
    assert detector.detect(make_series([10] * 8 + [30] * 8), down) == []


def test_changepoint_constant_series_has_no_event():
    detector = ChangepointDetector()
    params = detector.validate_params(
        {"threshold": 2, "pre_window": 3, "post_window": 3}, "rule"
    )
    assert detector.detect(make_series([5] * 12), params) == []
