"""Previous-period or seasonal-cycle change detector."""

from __future__ import annotations

from typing import Any, Mapping

from drift_attr.detectors.base import BaseDetector
from drift_attr.errors import fail
from drift_attr.schema import Detection, Series


class DeltaDetector(BaseDetector):
    detector_type = "delta"
    allowed_params = frozenset(
        {"comparison", "lag", "mode", "threshold", "direction"}
    )

    @classmethod
    def validate_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        raw = cls.normalize_params(params, field)
        comparison = cls.choice(
            raw,
            "comparison",
            ("previous", "seasonal"),
            field=field,
            default="previous",
        )
        mode = cls.choice(raw, "mode", ("relative", "absolute"), field=field, default="relative")
        direction = cls.choice(
            raw,
            "direction",
            ("increase", "decrease", "both"),
            field=field,
            default="both",
        )
        threshold = cls.number(
            raw, "threshold", field=field, required=True, minimum=0.0
        )
        assert threshold is not None
        if threshold <= 0:
            fail("E_CONFIG_SCHEMA", "threshold 必须大于 0", field=f"{field}.threshold")
        lag = cls.integer(
            raw,
            "lag",
            field=field,
            default=1 if comparison == "previous" else 7,
            minimum=1,
        )
        if comparison == "previous" and "lag" in raw and lag != 1:
            fail(
                "E_CONFIG_SCHEMA",
                "comparison=previous 时 lag 只能为 1",
                field=f"{field}.lag",
            )
        return {
            "comparison": comparison,
            "lag": lag,
            "mode": mode,
            "threshold": threshold,
            "direction": direction,
        }

    def detect(self, series: Series, params: Mapping[str, Any]) -> list[Detection]:
        lag = int(params["lag"])
        mode = str(params["mode"])
        threshold = float(params["threshold"])
        direction = str(params["direction"])
        detections: list[Detection] = []
        for index in range(lag, len(series.points)):
            point = series.points[index]
            base_point = series.points[index - lag]
            change = point.value - base_point.value
            if mode == "relative":
                if base_point.value == 0:
                    # A relative change from zero is undefined. The rule is
                    # documented and does not emit an event.
                    continue
                score = change / abs(base_point.value)
            else:
                score = change
            if direction == "increase" and score <= 0:
                continue
            if direction == "decrease" and score >= 0:
                continue
            if abs(score) < threshold:
                continue
            unit = "change_rate" if mode == "relative" else "change_amount"
            evidence = {
                "summary": (
                    f"{point.date.isoformat()} 较滞后 {lag} 期"
                    f"{'上升' if score > 0 else '下降'} "
                    f"{abs(score):.4f}{'' if mode == 'relative' else ''}"
                ),
                "formula": (
                    f"({point.value:g} - {base_point.value:g}) / "
                    f"|{base_point.value:g}| = {score:.6f}"
                    if mode == "relative"
                    else f"{point.value:g} - {base_point.value:g} = {score:.6f}"
                ),
                "comparison": params["comparison"],
                "lag": lag,
                "mode": mode,
                "metric": unit,
            }
            detections.append(
                Detection(
                    date=point.date,
                    observed=point.value,
                    baseline=base_point.value,
                    score=score,
                    evidence=evidence,
                    window_start=point.date,
                    window_end=point.date,
                )
            )
        return detections
