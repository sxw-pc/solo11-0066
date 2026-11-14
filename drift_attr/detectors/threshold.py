"""Fixed lower/upper threshold detector."""

from __future__ import annotations

from typing import Any, Mapping

from drift_attr.detectors.base import BaseDetector
from drift_attr.errors import fail
from drift_attr.schema import Detection, Series


class ThresholdDetector(BaseDetector):
    detector_type = "threshold"
    allowed_params = frozenset({"direction", "lower", "upper"})

    @classmethod
    def validate_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        raw = cls.normalize_params(params, field)
        direction = cls.choice(
            raw, "direction", ("above", "below", "both"), field=field, default="both"
        )
        lower = cls.number(raw, "lower", field=field)
        upper = cls.number(raw, "upper", field=field)
        if direction in ("below", "both") and lower is None:
            fail("E_CONFIG_SCHEMA", "below/both 方向必须提供 lower", field=f"{field}.lower")
        if direction in ("above", "both") and upper is None:
            fail("E_CONFIG_SCHEMA", "above/both 方向必须提供 upper", field=f"{field}.upper")
        if lower is not None and upper is not None and lower > upper:
            fail("E_CONFIG_SCHEMA", "lower 不能大于 upper", field=f"{field}.lower")
        return {"direction": direction, "lower": lower, "upper": upper}

    def detect(self, series: Series, params: Mapping[str, Any]) -> list[Detection]:
        direction = str(params["direction"])
        lower = params.get("lower")
        upper = params.get("upper")
        detections: list[Detection] = []
        for point in series.points:
            hit: str | None = None
            boundary: float | None = None
            if lower is not None and point.value < float(lower):
                hit, boundary = "below", float(lower)
            elif upper is not None and point.value > float(upper):
                hit, boundary = "above", float(upper)
            if hit is None or (direction == "above" and hit != "above"):
                continue
            if direction == "below" and hit != "below":
                continue
            gap = point.value - boundary if boundary is not None else 0.0
            evidence = {
                "summary": (
                    f"{point.date.isoformat()} 观测值 {point.value:g} "
                    f"{'低于' if hit == 'below' else '高于'}固定阈值 {boundary:g}"
                ),
                "formula": f"observed={point.value:g}, boundary={boundary:g}, gap={gap:g}",
                "boundary": boundary,
                "direction": hit,
                "gap": gap,
            }
            detections.append(
                Detection(
                    date=point.date,
                    observed=point.value,
                    baseline=boundary,
                    score=gap,
                    evidence=evidence,
                    window_start=point.date,
                    window_end=point.date,
                )
            )
        return detections
