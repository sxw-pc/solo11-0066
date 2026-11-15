"""Stable error codes and the single exception type used across drift-attr."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ERROR_CATALOG: dict[str, str] = {
    "E_ARGUMENT": "命令参数缺失或非法",
    "E_CONFIG_FILE_NOT_FOUND": "配置文件不存在",
    "E_CONFIG_PARSE": "YAML 语法错误",
    "E_CONFIG_SCHEMA": "配置字段缺失、类型错误或未知字段",
    "E_CONFIG_UNKNOWN_DETECTOR": "检测器类型未注册",
    "E_CONFIG_DUPLICATE": "检测器名称或归因维度重复",
    "E_CONFIG_WINDOW": "窗口长度非法",
    "E_CONFIG_TOP_N": "Top-N 取值非法",
    "E_DATA_FILE_NOT_FOUND": "数据文件不存在",
    "E_DATA_ENCODING": "数据文件不是合法 UTF-8",
    "E_DATA_HEADER": "CSV 表头缺失或字段不匹配",
    "E_DATA_EMPTY_VALUE": "必填字段为空且策略为 error",
    "E_DATA_DATE": "日期无法解析",
    "E_DATA_NUMBER": "数值无法解析",
    "E_DATA_NON_FINITE": "数值不是有限数",
    "E_DATA_DUPLICATE_KEY": "重复业务键且策略为 error",
    "E_DATA_NO_ROWS": "数据文件没有有效记录",
    "E_DATA_SCHEMA": "数据处理逻辑收到的结构不合法",
    "E_ANALYSIS": "分析过程失败",
    "E_RUN_NOT_FOUND": "台账中不存在指定运行",
    "E_ARTIFACT_NOT_FOUND": "运行产物不存在",
    "E_STORE": "SQLite 台账读写失败",
    "E_SAFE_PATH": "路径越界或不安全",
    "E_PURGE_NOT_FOUND": "台账中不存在可清理的运行",
    "E_PURGE_STATE": "运行当前状态不允许该清理操作",
    "E_PURGE_CONFLICT": "清理发现路径、内容或共享引用冲突",
    "E_PURGE_RESUME": "清理过程存在不可恢复的中断状态",
    "E_WEB_HOST": "Web 服务只允许监听本机地址",
    "E_WEB_NOT_FOUND": "页面或运行不存在",
}


@dataclass
class DriftError(Exception):
    """An expected, user-facing error with a stable code and location.

    The string form is intentionally stable so CLI output and tests can assert
    against it without parsing tracebacks.
    """

    code: str
    message: str
    path: str | None = None
    line: int | None = None
    field: str | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CATALOG:
            raise ValueError(f"unknown drift-attr error code: {self.code}")
        super().__init__(self.message)

    def __str__(self) -> str:
        location = ""
        if self.path:
            location += self.path
            if self.line is not None:
                location += f":{self.line}"
            location += " "
        field = f" field={self.field}" if self.field else ""
        hint = f" hint={self.hint}" if self.hint else ""
        return f"[{self.code}] {location}{field}: {self.message}{hint}".strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "field": self.field,
            "hint": self.hint,
        }


def fail(
    code: str,
    message: str,
    *,
    path: str | None = None,
    line: int | None = None,
    field: str | None = None,
    hint: str | None = None,
) -> "None":
    """Raise a DriftError; helper that keeps call sites compact."""
    raise DriftError(code, message, path=path, line=line, field=field, hint=hint)
