"""Detector protocol and shared configuration helpers."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Mapping

from drift_attr.errors import fail
from drift_attr.schema import Detection, Series


class BaseDetector(ABC):
    """All detectors are deterministic and evaluate one Series at a time."""

    detector_type = "base"
    allowed_params: frozenset[str] = frozenset()

    @classmethod
    def normalize_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        """Reject unknown parameters and return a plain mutable dict."""
        unknown = sorted(set(params) - cls.allowed_params)
        if unknown:
            fail(
                "E_CONFIG_SCHEMA",
                f"检测器 {cls.detector_type} 存在未知参数: {', '.join(unknown)}",
                field=field,
            )
        return dict(params)

    @classmethod
    def number(
        cls,
        params: Mapping[str, Any],
        key: str,
        *,
        field: str,
        required: bool = False,
        default: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        if key not in params:
            if required:
                fail("E_CONFIG_SCHEMA", f"缺少参数 {key}", field=f"{field}.{key}")
            return default
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须是数值", field=f"{field}.{key}")
        number = float(value)
        if not math.isfinite(number):
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须是有限数", field=f"{field}.{key}")
        if minimum is not None and number < minimum:
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须 >= {minimum}", field=f"{field}.{key}")
        if maximum is not None and number > maximum:
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须 <= {maximum}", field=f"{field}.{key}")
        return number

    @classmethod
    def integer(
        cls,
        params: Mapping[str, Any],
        key: str,
        *,
        field: str,
        required: bool = False,
        default: int | None = None,
        minimum: int | None = None,
    ) -> int | None:
        if key not in params:
            if required:
                fail("E_CONFIG_SCHEMA", f"缺少参数 {key}", field=f"{field}.{key}")
            return default
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int):
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须是整数", field=f"{field}.{key}")
        if minimum is not None and value < minimum:
            fail("E_CONFIG_SCHEMA", f"参数 {key} 必须 >= {minimum}", field=f"{field}.{key}")
        return value

    @classmethod
    def choice(
        cls,
        params: Mapping[str, Any],
        key: str,
        choices: tuple[str, ...],
        *,
        field: str,
        default: str,
    ) -> str:
        value = params.get(key, default)
        if value not in choices:
            fail(
                "E_CONFIG_SCHEMA",
                f"参数 {key} 必须是 {', '.join(choices)} 之一",
                field=f"{field}.{key}",
            )
        return str(value)

    @classmethod
    @abstractmethod
    def validate_params(cls, params: Mapping[str, Any], field: str) -> dict[str, Any]:
        """Return normalized parameters or raise a configuration error."""

    @abstractmethod
    def detect(self, series: Series, params: Mapping[str, Any]) -> list[Detection]:
        """Return zero or more deterministic detections on a sorted series."""
