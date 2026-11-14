"""Robust z-score detector using median and median absolute deviation."""

from __future__ import annotations

import statistics
from typing import Any, Mapping

from drift_attr.detectors.base import BaseDetector
from drift_attr.errors import fail
from drift_attr.schema import Detection, Series


class RobustZDetector(BaseDetector):
    detector_type = "robust_z"
    allowed_params = frozenset(
        {"history", "threshold", "mad_zero", "floor_scale", "direction"}
    )

    @classmethod
    def validate_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        raw = cls.normalize_params(params, field)
        history = cls.integer(
            raw, "history", field=field, default=14, minimum=3
        )
        threshold = cls.number(
            raw, "threshold", field=field, required=True, minimum=0.0
        )
        floor_scale = cls.number(
            raw, "floor_scale", field=field, default=1.0, minimum=0.0
        )
        assert history is not None and threshold is not None and floor_scale is not None
        if threshold <= 0:
            fail("E_CONFIG_SCHEMA", "threshold 必须大于 0", field=f"{field}.threshold")
        if floor_scale <= 0:
            fail("E_CONFIG_SCHEMA", "floor_scale 必须大于 0", field=f"{field}.floor_scale")
        mad_zero = cls.choice(
            raw, "mad_zero", ("skip", "floor"), field=field, default="skip"
        )
        direction = cls.choice(
            raw,
            "direction",
            ("above", "below", "both"),
            field=field,
            default="both",
        )
        return {
            "history": history,
            "threshold": threshold,
            "mad_zero": mad_zero,
            "floor_scale": floor_scale,
            "direction": direction,
        }

    def detect(self, series: Series, params: Mapping[str, Any]) -> list[Detection]:
        history = int(params["history"])
        threshold = float(params["threshold"])
        mad_zero = str(params["mad_zero"])
        floor_scale = float(params["floor_scale"])
        direction = str(params["direction"])
        detections: list[Detection] = []
        for index in range(history, len(series.points)):
            point = series.points[index]
            window = [item.value for item in series.points[index - history : index]]
            median = statistics.median(window)
            deviations = [abs(value - median) for value in window]
            mad = statistics.median(deviations)
            scale = 1.4826 * mad
            used_floor = False
            if scale == 0 or abs(scale) < 1e-15:
                if mad_zero == "skip":
                    continue
                scale = floor_scale
                used_floor = True
            z_score = (point.value - median) / scale
            if direction == "above" and z_score <= 0:
                continue
            if direction == "below" and z_score >= 0:
                continue
            if abs(z_score) < threshold:
                continue
            evidence = {
                "summary": (
                    f"{point.date.isoformat()} 观测值 {point.value:g} 的稳健 z-score "
                    f"为 {z_score:.4f}，超过阈值 {threshold:g}"
                ),
                "formula": f"({point.value:g} - {median:g}) / {scale:g}",
                "history": history,
                "median": median,
                "mad": mad,
                "scale": scale,
                "mad_zero": mad_zero,
                "mad_zero_handled": used_floor,
            }
            detections.append(
                Detection(
                    date=point.date,
                    observed=point.value,
                    baseline=median,
                    score=z_score,
                    evidence=evidence,
                    window_start=point.date,
                    window_end=point.date,
                )
            )
        return detections
