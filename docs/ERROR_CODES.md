# 稳定错误码（ERROR_CODES）

漂移归因台把可预期的用户错误统一表示为 `DriftError`。CLI 只打印一行稳定错误信息，不打印未处理 traceback：

```text
错误: [E_DATA_DATE] fixtures/data.csv:12 field=date: 无法解析日期: 'bad'，期望 YYYY-MM-DD
```

格式：

```text
[CODE] path:line field=name: message hint=...
```

`path`、`line`、`field`、`hint` 是可选项；`code` 和 `message` 始终存在。错误码是测试和自动化脚本依赖的稳定接口，新增错误码时应同步更新本文件。

## 配置错误

| 错误码 | 触发条件 | 常见字段 |
| --- | --- | --- |
| `E_CONFIG_FILE_NOT_FOUND` | YAML 配置文件不存在或不是文件 | `path` |
| `E_CONFIG_PARSE` | YAML 语法错误 | `path`、`line` |
| `E_CONFIG_SCHEMA` | 顶层或分区存在未知字段、字段类型错误、缺少必填字段、参数非法 | `field` |
| `E_CONFIG_UNKNOWN_DETECTOR` | `type` 不在四类已注册检测器中 | `detection.detectors[i].type` |
| `E_CONFIG_DUPLICATE` | 检测器名称重复，或归因维度重复 | 规则名或 `attribution.dimensions` |
| `E_CONFIG_WINDOW` | 窗口长度专用错误码（保留；当前范围校验通常由 `E_CONFIG_SCHEMA` 报告） | `attribution.*_window` |
| `E_CONFIG_TOP_N` | `top_n` 不是 1-100 的整数 | `attribution.top_n` |

配置还覆盖以下具体校验：

- `dataset.format` 不是 `long` / `wide`；
- `dataset.encoding` 不是 UTF-8；
- `duplicate_policy`、`empty_value`、`granularity`、`aggregate`、`fill_missing`、`week_start`、`severity` 等枚举非法；
- `threshold` 方向缺少对应上下界，或 `lower > upper`；
- `delta` 的 `previous` 模式 `lag != 1`；
- `robust_z` 的 `history < 3`、`mad_zero: floor` 的 `floor_scale <= 0`；
- `changepoint` 的窗口小于 2、`min_scale <= 0`；
- `output.database` 含路径分隔符或为 `.` / `..`。

## 数据错误

| 错误码 | 触发条件 | 定位信息 |
| --- | --- | --- |
| `E_DATA_FILE_NOT_FOUND` | CSV 文件不存在或不是文件 | `path` |
| `E_DATA_ENCODING` | 文件不是合法 UTF-8 | `path` |
| `E_DATA_HEADER` | 表头为空、含空列名、重复列名或缺少必需字段 | `path`、`line=1` |
| `E_DATA_EMPTY_VALUE` | 必填字段为空且 `empty_value: error` | `path`、`line`、`field` |
| `E_DATA_DATE` | 日期不是 `YYYY-MM-DD` 或超出 1900-2200 | `path`、`line`、`field` |
| `E_DATA_NUMBER` | 数值字段无法解析为浮点数 | `path`、`line`、`field` |
| `E_DATA_NON_FINITE` | 数值为 `NaN`、`Infinity` 等非有限数 | `path`、`line`、`field` |
| `E_DATA_DUPLICATE_KEY` | 重复业务键且 `duplicate_policy: error` | `path`、第二次出现的 `line` |
| `E_DATA_NO_ROWS` | CSV 没有有效记录 | `path` |
| `E_DATA_SCHEMA` | 内部数据结构不合法（保留给防御性检查） | `path`、`field` |

重复业务键为 `(date, metric, dimension_name, dimension_value)`。策略为 `warn` 时，警告写入数据集摘要、报告和 Web 运行详情，重复记录在聚合阶段按配置合并。

## 运行、产物与台账

| 错误码 | 触发条件 |
| --- | --- |
| `E_ARGUMENT` | CLI 参数非法，例如 `run_id` 含路径字符、端口越界 |
| `E_ANALYSIS` | 报告或运行目录写入失败 |
| `E_RUN_NOT_FOUND` | `report` 命令指定的 `run_id` 不在台账中 |
| `E_ARTIFACT_NOT_FOUND` | 台账缺少对应报告产物，或产物文件已不存在 |
| `E_STORE` | SQLite 初始化或事务写入失败 |
| `E_SAFE_PATH` | 路径安全校验失败（保留；Web 实际以 404 隐藏越界访问） |

## Web 错误

| 错误码 | 触发条件 |
| --- | --- |
| `E_WEB_HOST` | `serve --host` 不是 `127.0.0.1` 或 `localhost` |
| `E_WEB_NOT_FOUND` | Web 页面/运行/产物不存在（保留；HTTP 层返回 404 页面） |

Web 不返回内部 traceback。运行 ID 不存在、未知产物类型、产物路径越界、文件缺失均返回 404 页面；非本机 Host 返回 403。

## 测试中的断言建议

- 对配置错误断言 `DriftError.code`，不要依赖完整中文文案；
- 对数据错误同时断言 `line` 与 `field`；
- 对 CLI 错误断言 stderr 包含错误码；
- 对 Web 错误断言 HTTP 状态码和页面文案，不依赖 Flask 默认 404 HTML。
