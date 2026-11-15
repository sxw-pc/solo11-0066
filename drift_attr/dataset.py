"""Local CSV ingestion, validation, wide-table adaptation and fingerprints."""
from __future__ import annotations
import csv
import hashlib
import io
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from drift_attr.config import DatasetConfig
from drift_attr.errors import DriftError, fail
from drift_attr.schema import stable_float
@dataclass(frozen=True)
class MetricRow:
    date: date
    metric: str
    dimension_name: str
    dimension_value: str
    value: float
    line: int = 0
@dataclass
class Dataset:
    path: str
    resolved_path: Path
    format: str
    rows: list[MetricRow]
    fingerprint: str
    warnings: list[str] = field(default_factory=list)
    raw_bytes: bytes = b""
    raw_sha256: str = ""
    @property
    def row_count(self) -> int:
        return len(self.rows)
    @property
    def dimension_names(self) -> list[str]:
        return sorted({row.dimension_name for row in self.rows})
def resolve_path(value: str | Path, base_dir: str | Path | None = None) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(base_dir or Path.cwd()) / path
    return path.resolve()
def _read_bytes(path: Path, display: str) -> bytes:
    if not path.exists() or not path.is_file():
        fail("E_DATA_FILE_NOT_FOUND", "数据文件不存在", path=display)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DriftError("E_DATA_FILE_NOT_FOUND", f"数据文件读取失败: {exc}", path=display) from exc


def _decode_text(raw: bytes, display: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DriftError(
            "E_DATA_ENCODING", "数据文件必须使用 UTF-8 编码", path=display
        ) from exc


def _read_text(path: Path, display: str) -> str:
    return _decode_text(_read_bytes(path), display)
def _normalized_headers(reader: csv.DictReader, display: str, required: set[str]) -> list[str]:
    headers = [str(item).strip() for item in (reader.fieldnames or [])]
    if not headers or any(not item for item in headers):
        fail("E_DATA_HEADER", "CSV 表头为空或包含空列名", path=display, line=1)
    if len(set(headers)) != len(headers):
        fail("E_DATA_HEADER", "CSV 表头包含重复列名", path=display, line=1)
    missing = sorted(required - set(headers))
    if missing:
        fail(
            "E_DATA_HEADER",
            f"CSV 缺少必需字段: {', '.join(missing)}",
            path=display,
            line=1,
        )
    return headers
def _clean_row(row: dict[str | None, str | list[str] | None], headers: list[str]) -> dict[str, str]:
    return {header: str(row.get(header, "") or "").strip() for header in headers}
def _parse_date(raw: str, display: str, line: int, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        fail(
            "E_DATA_DATE", f"无法解析日期: {raw!r}，期望 YYYY-MM-DD",
            path=display, line=line, field=field_name,
        )
    if parsed.year < 1900 or parsed.year > 2200:
        fail("E_DATA_DATE", "日期超出 1900-2200 范围", path=display, line=line, field=field_name)
    return parsed
def _parse_number(raw: str, display: str, line: int, field_name: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        fail("E_DATA_NUMBER", f"无法解析数值: {raw!r}", path=display, line=line, field=field_name)
    if not math.isfinite(value):
        fail("E_DATA_NON_FINITE", "数值必须是有限数", path=display, line=line, field=field_name)
    return value
def _duplicates(rows: list[MetricRow], display: str, policy: str) -> list[str]:
    positions: dict[tuple[date, str, str, str], list[int]] = {}
    for row in rows:
        key = (row.date, row.metric, row.dimension_name, row.dimension_value)
        positions.setdefault(key, []).append(row.line)
    warnings: list[str] = []
    for key in sorted(positions, key=lambda item: (item[0], item[1], item[2], item[3])):
        lines = positions[key]
        if len(lines) < 2:
            continue
        message = (
            f"重复业务键 date={key[0].isoformat()} metric={key[1]} "
            f"{key[2]}={key[3]}，行号 {', '.join(str(line) for line in lines)}"
        )
        if policy == "error":
            fail("E_DATA_DUPLICATE_KEY", message, path=display, line=lines[1])
        warnings.append(message)
    return warnings
def _fingerprint(rows: list[MetricRow]) -> str:
    ordered = sorted(
        rows,
        key=lambda row: (
            row.date, row.metric, row.dimension_name, row.dimension_value, row.value
        ),
    )
    digest = hashlib.sha256()
    for row in ordered:
        value = 0.0 if row.value == 0 else row.value
        line = (
            f"{row.date.isoformat()}\t{row.metric}\t{row.dimension_name}\t"
            f"{row.dimension_value}\t{value:.12g}\n"
        )
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()
def _read_long(
    text: str, cfg: DatasetConfig, display: str
) -> tuple[list[MetricRow], list[str]]:
    assert cfg.metric_field and cfg.dimension_name_field
    assert cfg.dimension_value_field and cfg.value_field
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {
        cfg.date_field, cfg.metric_field, cfg.dimension_name_field,
        cfg.dimension_value_field, cfg.value_field,
    }
    headers = _normalized_headers(reader, display, required)
    rows: list[MetricRow] = []
    warnings: list[str] = []
    for line, raw_row in enumerate(reader, start=2):
        raw = _clean_row(raw_row, headers)
        if not any(raw.values()):
            continue
        required_values = [
            cfg.date_field, cfg.metric_field, cfg.dimension_name_field,
            cfg.dimension_value_field, cfg.value_field,
        ]
        empty = [name for name in required_values if not raw[name]]
        if empty:
            if cfg.empty_value == "drop":
                warnings.append(f"第 {line} 行空值已丢弃: {', '.join(empty)}")
                continue
            fail(
                "E_DATA_EMPTY_VALUE", f"必填字段为空: {', '.join(empty)}",
                path=display, line=line, field=empty[0],
            )
        rows.append(
            MetricRow(
                date=_parse_date(raw[cfg.date_field], display, line, cfg.date_field),
                metric=raw[cfg.metric_field],
                dimension_name=raw[cfg.dimension_name_field],
                dimension_value=raw[cfg.dimension_value_field],
                value=_parse_number(raw[cfg.value_field], display, line, cfg.value_field),
                line=line,
            )
        )
    return rows, warnings
def _read_wide(
    text: str, cfg: DatasetConfig, display: str
) -> tuple[list[MetricRow], list[str]]:
    assert cfg.dimension_field and cfg.dimension_name
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {cfg.date_field, cfg.dimension_field, *cfg.metric_columns}
    headers = _normalized_headers(reader, display, required)
    rows: list[MetricRow] = []
    warnings: list[str] = []
    for line, raw_row in enumerate(reader, start=2):
        raw = _clean_row(raw_row, headers)
        if not any(raw.values()):
            continue
        if not raw[cfg.date_field] or not raw[cfg.dimension_field]:
            if cfg.empty_value == "drop":
                warnings.append(f"第 {line} 行日期或维度为空，已丢弃")
                continue
            fail(
                "E_DATA_EMPTY_VALUE", "日期或维度字段为空",
                path=display, line=line,
                field=cfg.date_field if not raw[cfg.date_field] else cfg.dimension_field,
            )
        parsed_date = _parse_date(raw[cfg.date_field], display, line, cfg.date_field)
        for metric in cfg.metric_columns:
            cell = raw[metric]
            if not cell:
                if cfg.empty_value == "drop":
                    warnings.append(f"第 {line} 行 {metric} 为空，已丢弃")
                    continue
                fail(
                    "E_DATA_EMPTY_VALUE", f"{metric} 为空",
                    path=display, line=line, field=metric,
                )
            rows.append(
                MetricRow(
                    date=parsed_date,
                    metric=metric,
                    dimension_name=cfg.dimension_name,
                    dimension_value=raw[cfg.dimension_field],
                    value=_parse_number(cell, display, line, metric),
                    line=line,
                )
            )
    return rows, warnings
def parse_dataset_bytes(
    raw: bytes,
    cfg: DatasetConfig,
    display: str,
    *,
    resolved_path: Path,
) -> Dataset:
    """Parse the exact bytes the loader read; callers never re-read the source."""
    text = _decode_text(raw, display)
    if cfg.format == "long":
        rows, warnings = _read_long(text, cfg, display)
    else:
        rows, warnings = _read_wide(text, cfg, display)
    if not rows:
        fail("E_DATA_NO_ROWS", "数据文件没有有效记录", path=display)
    escaped = _duplicates(rows, display, cfg.duplicate_policy)
    warnings.extend(escaped)
    return Dataset(
        path=cfg.path,
        resolved_path=resolved_path,
        format=cfg.format,
        rows=rows,
        fingerprint=_fingerprint(rows),
        warnings=warnings,
        raw_bytes=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_dataset(
    cfg: DatasetConfig, base_dir: str | Path | None = None
) -> Dataset:
    """Load a local CSV into canonical rows and compute a stable fingerprint."""
    resolved = resolve_path(cfg.path, base_dir)
    display = cfg.path
    return parse_dataset_bytes(_read_bytes(resolved, display), cfg, display,
                               resolved_path=resolved)
def dataset_summary(dataset: Dataset) -> dict[str, object]:
    values = [stable_float(row.value) for row in dataset.rows]
    return {
        "path": dataset.path,
        "format": dataset.format,
        "fingerprint": dataset.fingerprint,
        "row_count": dataset.row_count,
        "dimension_names": dataset.dimension_names,
        "warnings": list(dataset.warnings),
        "min_value": min(values),
        "max_value": max(values),
    }
