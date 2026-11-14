# 配置参考（CONFIG）

漂移归因台使用 UTF-8 YAML 配置文件。解析使用 `yaml.safe_load`，随后做严格结构校验；未知字段、缺失字段、类型错误、非法枚举值和重复声明都会产生稳定错误码。

运行命令：

```bash
python -m drift_attr validate --config fixtures/rules.yaml
python -m drift_attr analyze --config fixtures/rules.yaml --run-id demo-run
```

## 路径约定与指纹

- `dataset.path`、`output.directory` 中的相对路径按**当前工作目录**解析。
- 配置路径本身可以是相对或绝对路径；台账和报告中的路径尽量写为仓库内相对路径。
- 规则指纹是对配置文件 UTF-8 原文计算的 SHA-256，运行时写入 `runs.rule_fingerprint`、`rule_snapshots` 和报告。
- 数据指纹是对规范长表记录排序后计算的 SHA-256，不受 CSV 行序影响；重复记录会保留在指纹中。
- 报告中的路径只指向本地输出目录，不写外部目标地址。

## 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `version` | int | 否 | 仅支持 `1`，默认 1 |
| `dataset` | mapping | 是 | 本地 CSV 接入配置 |
| `series` | mapping | 否 | 聚合、粒度和补值配置 |
| `detection` | mapping | 是 | 必须包含非空 `detectors` 列表 |
| `attribution` | mapping | 是 | 归因维度、窗口、Top-N |
| `output` | mapping | 否 | 输出目录与 SQLite 文件名 |

顶层不允许其他字段。

## dataset

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 必填 | 本地 CSV 路径 |
| `format` | `long` / `wide` | `long` | 长表或宽表适配 |
| `encoding` | string | `utf-8` | 只接受 UTF-8、UTF8 或 UTF-8-SIG 写法 |
| `duplicate_policy` | `warn` / `error` | `warn` | 重复业务键行为 |
| `empty_value` | `error` / `drop` | `error` | 必填字段为空的行为 |
| `date_field` | string | 必填 | 日期列名 |
| `metric_field` | string | long 必填 | 指标名列 |
| `dimension_name_field` | string | long 必填 | 维度名列 |
| `dimension_value_field` | string | long 必填 | 维度值列 |
| `value_field` | string | long 必填 | 数值列 |
| `dimension_field` | string | wide 必填 | 宽表中表示维度值的列 |
| `dimension_name` | string | wide 必填 | 宽表展开后的固定维度名 |
| `metric_columns` | string list | wide 必填 | 宽表中要展开的指标列，不能为空或重复 |

### 长表示例

```csv
date,metric,dimension_name,dimension_value,value
2025-03-01,orders,channel,channel_web,120
2025-03-01,orders,channel,channel_store,70
```

```yaml
dataset:
  path: fixtures/demo_metrics.csv
  format: long
  date_field: date
  metric_field: metric
  dimension_name_field: dimension_name
  dimension_value_field: dimension_value
  value_field: value
```

长表业务键为：`(date, metric, dimension_name, dimension_value)`。重复键在 `warn` 下保留记录并记录警告，在聚合阶段按 `series.aggregate` 合并；在 `error` 下抛出 `E_DATA_DUPLICATE_KEY`。

### 宽表示例

```csv
date,region,visits,orders
2025-01-06,region_alpha,120,18
2025-01-06,region_beta,90,14
```

```yaml
dataset:
  path: fixtures/demo_wide_metrics.csv
  format: wide
  date_field: date
  dimension_field: region
  dimension_name: region
  metric_columns: [visits, orders]
```

每一行每个指标列展开为一条规范记录，例如 `(2025-01-06, visits, region, region_alpha, 120)`。空单元格按 `empty_value` 处理。

## series

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `granularity` | `day` / `week` | `day` | 聚合周期 |
| `aggregate` | `sum` / `mean` | `sum` | 同一周期多条记录的聚合方式 |
| `fill_missing` | `zero` / `previous` / `drop` | `zero` | 缺失周期补值策略 |
| `week_start` | `monday` / `sunday` | `monday` | 周粒度标签的周起点 |

周粒度的时间标签是周期起点日期（周一或周日）。构建时以全数据集最早/最晚周期为时间轴，为每个“指标 × 维度值”序列对齐缺失周期。

补值行为：

- `zero`：缺失周期填 `0.0`，标记 `imputed=true`；
- `previous`：使用最近一个已有观测值；序列开头没有可回填值的周期被丢弃并记录警告；
- `drop`：不补点，保留稀疏序列。

报告 `series` 段记录粒度、聚合方式、补值方式、时间轴和每个序列的补值点数。

## detection

```yaml
detection:
  detectors:
    - name: rule_name
      type: threshold
      metrics: [orders]
      dimension_names: [channel]       # 可选
      dimension_values: [channel_store] # 可选
      severity: high                   # low|medium|high|critical
      direction: below
      lower: 45
```

每条规则通用字段：

| 字段 | 说明 |
| --- | --- |
| `name` | 运行内唯一规则名，必填 |
| `type` | `threshold` / `delta` / `robust_z` / `changepoint` |
| `metrics` | 匹配的指标名列表，必填且非空 |
| `dimension_names` | 可选，只检测指定维度名 |
| `dimension_values` | 可选，只检测指定维度值 |
| `severity` | 默认 `medium` |

检测器专属参数见 [DETECTORS.md](DETECTORS.md)。未知检测器抛 `E_CONFIG_UNKNOWN_DETECTOR`，未知参数抛 `E_CONFIG_SCHEMA`。

## attribution

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `dimensions` | string list | 必填 | 归因维度名，非空且不得重复 |
| `top_n` | int | `3` | 每个异常位置、每个归因维度保留的维度值数，1-100 |
| `baseline_window` | int | `7` | 异常窗口之前的基期周期数，1-365 |
| `comparison_window` | int | `7` | 截至异常日期的异常/对比周期数，1-365 |
| `other_label` | string | `其他` | 超出 Top-N 的剩余项标签 |

窗口按 `series.granularity` 的周期数计算。窗口顺序与公式见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## output

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `directory` | string | `output` | 运行输出根目录 |
| `database` | string | `drift_attr.sqlite3` | SQLite 文件名，只能是输出目录内文件名，不能含路径分隔符 |

每次运行生成的文件固定为 JSON、Markdown、自包含 HTML、归因 JSON 和规则快照，不提供“关闭某种格式”的配置，以保证验收产物一致。

## 完整示例

见仓库根目录 `fixtures/rules.yaml`，它对应 4 个 demo 检测器、1 个归因维度、Top-N=2、7 期基期与 7 期对比窗口。

## 校验边界

- `dataset.format: long` 时缺少任何长表字段会报错；`wide` 时缺少宽表字段或 `metric_columns` 为空会报错。
- `detection.detectors` 不能为空；规则名、`metrics` 列表项和归因维度不得重复。
- `direction`、`mode`、`comparison`、`mad_zero`、`severity` 等枚举值均为严格匹配。
- `top_n` 非法统一返回 `E_CONFIG_TOP_N`；窗口长度非法返回配置错误（通常为 `E_CONFIG_SCHEMA`，也可由窗口专用错误码表达）。
- 所有路径与 CSV 检查错误都带文件路径、行号（适用时）和字段名。
