"""Sliding-window level-shift detector with local-peak suppression."""

from __future__ import annotations

import statistics
from typing import Any, Mapping

from drift_attr.detectors.base import BaseDetector
from drift_attr.errors import fail
from drift_attr.schema import Detection, Series


class ChangepointDetector(BaseDetector):
    detector_type = "changepoint"
    allowed_params = frozenset(
        {"pre_window", "post_window", "threshold", "min_scale", "direction"}
    )

    @classmethod
    def validate_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        raw = cls.normalize_params(params, field)
        pre_window = cls.integer(
            raw, "pre_window", field=field, default=7, minimum=2
        )
        post_window = cls.integer(
            raw, "post_window", field=field, default=7, minimum=2
        )
        threshold = cls.number(
            raw, "threshold", field=field, required=True, minimum=0.0
        )
        min_scale = cls.number(
            raw, "min_scale", field=field, default=1.0, minimum=0.0
        )
        assert pre_window is not None and post_window is not None
        assert threshold is not None and min_scale is not None
        if threshold <= 0:
            fail("E_CONFIG_SCHEMA", "threshold 必须大于 0", field=f"{field}.threshold")
        if min_scale <= 0:
            fail("E_CONFIG_SCHEMA", "min_scale 必须大于 0", field=f"{field}.min_scale")
        direction = cls.choice(
            raw,
            "direction",
            ("increase", "decrease", "both"),
            field=field,
            default="both",
        )
        return {
            "pre_window": pre_window,
            "post_window": post_window,
            "threshold": threshold,
            "min_scale": min_scale,
            "direction": direction,
        }

    def detect(self, series: Series, params: Mapping[str, Any]) -> list[Detection]:
        pre_window = int(params["pre_window"])
        post_window = int(params["post_window"])
        threshold = float(params["threshold"])
        min_scale = float(params["min_scale"])
        direction = str(params["direction"])
        points = series.points
        candidates: list[tuple[int, float, float, float, float]] = []
        # (start index, pre mean, post mean, scale, score)
        for start in range(pre_window, len(points) - post_window + 1):
            pre = [item.value for item in points[start - pre_window : start]]
            post = [item.value for item in points[start : start + post_window]]
            pre_mean = statistics.fmean(pre)
            post_mean = statistics.fmean(post)
            diff = post_mean - pre_mean
            scale = max(statistics.stdev(pre), min_scale)
            score = diff / scale
            candidates.append((start, pre_mean, post_mean, scale, score))

        detections: list[Detection] = []
        for index, (start, pre_mean, post_mean, scale, score) in enumerate(candidates):
            if abs(score) < threshold:
                continue
            if direction == "increase" and score <= 0:
                continue
            if direction == "decrease" and score >= 0:
                continue
            previous = candidates[index - 1][4] if index > 0 else None
            following = candidates[index + 1][4] if index + 1 < len(candidates) else None
            # Strictly greater than the left neighbour and no smaller than the
            # right neighbour selects the earliest point of a flat peak.
            if previous is not None and score <= previous:
                continue
            if following is not None and score < following:
                continue
            post_points = points[start : start + post_window]
            evidence = {
                "summary": (
                    f"{post_points[0].date.isoformat()} 起后窗均值 {post_mean:g} "
                    f"较前窗均值 {pre_mean:g} 变化，突变分数 {score:.4f}"
                ),
                "formula": (
                    f"(post_mean {post_mean:g} - pre_mean {pre_mean:g}) / "
                    f"max(pre_stdev, {min_scale:g}) = {score:.6f}"
                ),
                "pre_window": pre_window,
                "post_window": post_window,
                "pre_mean": pre_mean,
                "post_mean": post_mean,
                "scale": scale,
            }
            detections.append(
                Detection(
                    date=post_points[0].date,
                    observed=post_mean,
                    baseline=pre_mean,
                    score=score,
                    evidence=evidence,
                    window_start=post_points[0].date,
                    window_end=post_points[-1].date,
                )
            )
        return detections
