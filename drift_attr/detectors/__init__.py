"""Detector registry.

Keeping the registry explicit makes unknown detector configuration errors
deterministic and avoids plugin discovery or network access.
"""

from __future__ import annotations

from drift_attr.detectors.base import BaseDetector
from drift_attr.detectors.changepoint import ChangepointDetector
from drift_attr.detectors.delta import DeltaDetector
from drift_attr.detectors.robust_z import RobustZDetector
from drift_attr.detectors.threshold import ThresholdDetector
from drift_attr.errors import fail

DETECTOR_CLASSES: dict[str, type[BaseDetector]] = {
    cls.detector_type: cls
    for cls in (
        ThresholdDetector,
        DeltaDetector,
        RobustZDetector,
        ChangepointDetector,
    )
}

__all__ = [
    "BaseDetector",
    "ThresholdDetector",
    "DeltaDetector",
    "RobustZDetector",
    "ChangepointDetector",
    "DETECTOR_CLASSES",
    "get_detector",
]


def get_detector(detector_type: str, *, field: str = "detection.detectors") -> BaseDetector:
    cls = DETECTOR_CLASSES.get(detector_type)
    if cls is None:
        fail(
            "E_CONFIG_UNKNOWN_DETECTOR",
            f"未知检测器类型: {detector_type}",
            field=field,
        )
    return cls()
