"""Strict YAML configuration loading and validation."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import yaml
from drift_attr.detectors import DETECTOR_CLASSES, get_detector
from drift_attr.errors import DriftError, fail
from drift_attr.schema import SEVERITIES
@dataclass(frozen=True)
class DatasetConfig:
    path: str
    format: str
    encoding: str
    duplicate_policy: str
    empty_value: str
    date_field: str
    metric_field: str | None = None
    dimension_name_field: str | None = None
    dimension_value_field: str | None = None
    value_field: str | None = None
    dimension_field: str | None = None
    dimension_name: str | None = None
    metric_columns: tuple[str, ...] = ()
@dataclass(frozen=True)
class SeriesConfig:
    granularity: str
    aggregate: str
    fill_missing: str
    week_start: str
@dataclass(frozen=True)
class DetectorRule:
    name: str
    detector_type: str
    metrics: tuple[str, ...]
    dimension_names: tuple[str, ...]
    dimension_values: tuple[str, ...]
    severity: str
    params: dict[str, Any]
@dataclass(frozen=True)
class AttributionConfig:
    dimensions: tuple[str, ...]
    top_n: int
    baseline_window: int
    comparison_window: int
    other_label: str
@dataclass(frozen=True)
class OutputConfig:
    directory: str
    database: str
@dataclass
class AppConfig:
    source_path: Path
    fingerprint: str
    text: str
    version: int
    dataset: DatasetConfig
    series: SeriesConfig
    detectors: list[DetectorRule] = field(default_factory=list)
    attribution: AttributionConfig | None = None
    output: OutputConfig | None = None
TOP_KEYS = {"version", "dataset", "series", "detection", "attribution", "output"}
DATASET_KEYS = {
    "path", "format", "encoding", "duplicate_policy", "empty_value", "date_field",
    "metric_field", "dimension_name_field", "dimension_value_field", "value_field",
    "dimension_field", "dimension_name", "metric_columns",
}
SERIES_KEYS = {"granularity", "aggregate", "fill_missing", "week_start"}
ATTRIBUTION_KEYS = {"dimensions", "top_n", "baseline_window", "comparison_window", "other_label"}
OUTPUT_KEYS = {"directory", "database"}
def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        fail("E_CONFIG_SCHEMA", f"{field_name} 必须是映射", field=field_name)
    return value
def _only(mapping: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        fail(
            "E_CONFIG_SCHEMA",
            f"{field_name} 存在未知字段: {', '.join(unknown)}",
            field=field_name,
        )
def _text(mapping: Mapping[str, Any], key: str, field_name: str, default: str | None = None) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value.strip():
        fail("E_CONFIG_SCHEMA", f"{key} 必须是非空字符串", field=f"{field_name}.{key}")
    return value.strip()
def _integer(
    mapping: Mapping[str, Any], key: str, field_name: str, default: int,
    minimum: int, maximum: int | None = None,
) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        fail("E_CONFIG_SCHEMA", f"{key} 必须是整数", field=f"{field_name}.{key}")
    if value < minimum or (maximum is not None and value > maximum):
        fail("E_CONFIG_SCHEMA", f"{key} 超出允许范围", field=f"{field_name}.{key}")
    return value
def _strings(
    mapping: Mapping[str, Any], key: str, field_name: str, *, required: bool,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = mapping.get(key, list(default))
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        fail("E_CONFIG_SCHEMA", f"{key} 必须是字符串列表", field=f"{field_name}.{key}")
    if required and not value:
        fail("E_CONFIG_SCHEMA", f"{key} 不能为空", field=f"{field_name}.{key}")
    if len(set(value)) != len(value):
        fail("E_CONFIG_DUPLICATE", f"{key} 存在重复项", field=f"{field_name}.{key}")
    return tuple(item.strip() for item in value)
def _dataset(raw: Mapping[str, Any]) -> DatasetConfig:
    _only(raw, DATASET_KEYS, "dataset")
    path = _text(raw, "path", "dataset")
    fmt = _text(raw, "format", "dataset", "long").lower()
    if fmt not in {"long", "wide"}:
        fail("E_CONFIG_SCHEMA", "format 必须是 long 或 wide", field="dataset.format")
    encoding = _text(raw, "encoding", "dataset", "utf-8").lower().replace("_", "-")
    if encoding not in {"utf-8", "utf8", "utf-8-sig"}:
        fail("E_CONFIG_SCHEMA", "仅支持 UTF-8 编码", field="dataset.encoding")
    duplicate = _text(raw, "duplicate_policy", "dataset", "warn").lower()
    empty = _text(raw, "empty_value", "dataset", "error").lower()
    if duplicate not in {"warn", "error"}:
        fail("E_CONFIG_SCHEMA", "duplicate_policy 必须是 warn 或 error", field="dataset.duplicate_policy")
    if empty not in {"error", "drop"}:
        fail("E_CONFIG_SCHEMA", "empty_value 必须是 error 或 drop", field="dataset.empty_value")
    date_field = _text(raw, "date_field", "dataset")
    if fmt == "long":
        return DatasetConfig(
            path=path, format=fmt, encoding="utf-8", duplicate_policy=duplicate,
            empty_value=empty, date_field=date_field,
            metric_field=_text(raw, "metric_field", "dataset"),
            dimension_name_field=_text(raw, "dimension_name_field", "dataset"),
            dimension_value_field=_text(raw, "dimension_value_field", "dataset"),
            value_field=_text(raw, "value_field", "dataset"),
        )
    metric_columns = _strings(raw, "metric_columns", "dataset", required=True)
    return DatasetConfig(
        path=path, format=fmt, encoding="utf-8", duplicate_policy=duplicate,
        empty_value=empty, date_field=date_field,
        dimension_field=_text(raw, "dimension_field", "dataset"),
        dimension_name=_text(raw, "dimension_name", "dataset"),
        metric_columns=metric_columns,
    )
def _series(raw: Mapping[str, Any]) -> SeriesConfig:
    _only(raw, SERIES_KEYS, "series")
    granularity = _text(raw, "granularity", "series", "day").lower()
    aggregate = _text(raw, "aggregate", "series", "sum").lower()
    fill = _text(raw, "fill_missing", "series", "zero").lower()
    week_start = _text(raw, "week_start", "series", "monday").lower()
    for key, value, choices in (
        ("granularity", granularity, {"day", "week"}),
        ("aggregate", aggregate, {"sum", "mean"}),
        ("fill_missing", fill, {"zero", "previous", "drop"}),
        ("week_start", week_start, {"monday", "sunday"}),
    ):
        if value not in choices:
            fail("E_CONFIG_SCHEMA", f"{key} 必须是 {'/'.join(sorted(choices))}", field=f"series.{key}")
    return SeriesConfig(granularity, aggregate, fill, week_start)
def _detectors(raw: Any) -> list[DetectorRule]:
    if not isinstance(raw, list) or not raw:
        fail("E_CONFIG_SCHEMA", "detection.detectors 必须是非空列表", field="detection.detectors")
    rules: list[DetectorRule] = []
    names: set[str] = set()
    base_keys = {"name", "type", "metrics", "dimension_names", "dimension_values", "severity"}
    for index, item in enumerate(raw):
        field_name = f"detection.detectors[{index}]"
        rule = _mapping(item, field_name)
        name = _text(rule, "name", field_name)
        if name in names:
            fail("E_CONFIG_DUPLICATE", f"检测器名称重复: {name}", field=f"{field_name}.name")
        names.add(name)
        detector_type = _text(rule, "type", field_name)
        get_detector(detector_type, field=f"{field_name}.type")
        cls = DETECTOR_CLASSES[detector_type]
        params_raw = {key: value for key, value in rule.items() if key not in base_keys}
        params = cls.validate_params(params_raw, field_name)
        metrics = _strings(rule, "metrics", field_name, required=True)
        dimension_names = _strings(rule, "dimension_names", field_name, required=False)
        dimension_values = _strings(rule, "dimension_values", field_name, required=False)
        severity = _text(rule, "severity", field_name, "medium").lower()
        if severity not in SEVERITIES:
            fail("E_CONFIG_SCHEMA", f"severity 必须是 {'/'.join(SEVERITIES)}", field=f"{field_name}.severity")
        rules.append(
            DetectorRule(
                name=name, detector_type=detector_type, metrics=metrics,
                dimension_names=dimension_names, dimension_values=dimension_values,
                severity=severity, params=params,
            )
        )
    return rules
def _attribution(raw: Mapping[str, Any]) -> AttributionConfig:
    _only(raw, ATTRIBUTION_KEYS, "attribution")
    dimensions = _strings(raw, "dimensions", "attribution", required=True)
    top_n = raw.get("top_n", 3)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 100:
        fail("E_CONFIG_TOP_N", "top_n 必须是 1-100 的整数", field="attribution.top_n")
    baseline = _integer(raw, "baseline_window", "attribution", 7, 1, 365)
    comparison = _integer(raw, "comparison_window", "attribution", 7, 1, 365)
    if baseline < 1 or comparison < 1:
        fail("E_CONFIG_WINDOW", "窗口长度必须 >= 1", field="attribution.baseline_window")
    return AttributionConfig(
        dimensions=dimensions, top_n=top_n, baseline_window=baseline,
        comparison_window=comparison,
        other_label=_text(raw, "other_label", "attribution", "其他"),
    )
def load_config(path: str | Path) -> AppConfig:
    source = Path(path)
    if not source.exists() or not source.is_file():
        fail("E_CONFIG_FILE_NOT_FOUND", "配置文件不存在", path=str(path))
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DriftError("E_CONFIG_SCHEMA", "配置文件必须使用 UTF-8 编码", path=str(path)) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise DriftError(
            "E_CONFIG_PARSE", "YAML 解析失败", path=str(path),
            line=(mark.line + 1) if mark else None,
        ) from exc
    root = _mapping(raw, "root")
    _only(root, TOP_KEYS, "root")
    version = root.get("version", 1)
    if version != 1:
        fail("E_CONFIG_SCHEMA", "仅支持 version: 1", field="version")
    dataset_raw = _mapping(root.get("dataset"), "dataset")
    series_raw = _mapping(root.get("series", {}), "series")
    detection_raw = _mapping(root.get("detection"), "detection")
    _only(detection_raw, {"detectors"}, "detection")
    attribution_raw = _mapping(root.get("attribution"), "attribution")
    output_raw = _mapping(root.get("output", {}), "output")
    _only(output_raw, OUTPUT_KEYS, "output")
    database = _text(output_raw, "database", "output", "drift_attr.sqlite3")
    if "/" in database or "\\" in database or database in {".", ".."}:
        fail("E_CONFIG_SCHEMA", "database 只能是输出目录内的文件名", field="output.database")
    output = OutputConfig(
        directory=_text(output_raw, "directory", "output", "output"),
        database=database,
    )
    return AppConfig(
        source_path=source.resolve(),
        fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        version=1,
        dataset=_dataset(dataset_raw),
        series=_series(series_raw),
        detectors=_detectors(detection_raw.get("detectors")),
        attribution=_attribution(attribution_raw),
        output=output,
    )
