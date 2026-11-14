# 检测器参考（DETECTORS）

所有检测器实现同一接口：输入一条按日期升序排列的 `Series`，输出零个或多个 `Detection`。检测器不访问网络、不使用随机数、不依赖当前时间，因此相同输入与参数产生相同结果。

每个 `Detection` 最终附带运行 ID 转成异常事件，包含：

- `run_id`、`metric`、`dimension_name`、`dimension_value`、`date`；
- `detector`、`rule_name`、`observed`、`baseline`、`score`；
- `severity`、`evidence`（摘要、公式与参数）、`window_start`、`window_end`。

多个检测器命中同一“指标 × 维度 × 日期”位置时，报告层合并成一个异常位置；原始事件仍逐条保留在 `raw_events` 和 `anomaly_events` 台账中。

## threshold

固定阈值检测器。

```yaml
- name: low_orders_store
  type: threshold
  metrics: [orders]
  dimension_values: [channel_store]
  severity: high
  direction: below
  lower: 45
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `direction` | `both` | `above` / `below` / `both` |
| `lower` | 无 | 下阈值；`below` 或 `both` 必填 |
| `upper` | 无 | 上阈值；`above` 或 `both` 必填 |

触发条件：

```text
below: observed < lower
above: observed > upper
both:  observed < lower OR observed > upper
```

边界：观测值**严格**越过阈值才触发，恰好等于阈值不触发。`lower > upper` 是配置错误。分数为 `observed - 触发边界`，可正可负；事件基线为触发边界，窗口为当天。

## delta

环比或同周期变化检测器。

```yaml
- name: gmv_day_over_day_web
  type: delta
  metrics: [gmv]
  dimension_values: [channel_web]
  severity: medium
  comparison: previous
  mode: relative
  threshold: 0.25
  direction: both
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `comparison` | `previous` | `previous` 或 `seasonal` |
| `lag` | previous=1，seasonal=7 | 回看周期数，至少 1 |
| `mode` | `relative` | `relative` 或 `absolute` |
| `threshold` | 必填 | 大于 0 的触发阈值 |
| `direction` | `both` | `increase` / `decrease` / `both` |

公式：

```text
baseline = value[i - lag]
change   = value[i] - baseline

relative score = change / |baseline|
absolute score = change
```

触发要求变化方向符合 `direction`，且 `|score| >= threshold`。

边界：

- `comparison: previous` 的 `lag` 只能为 1。
- 相对模式的基线为 0 时变化率未定义，该点被跳过，不产生事件，也不抛异常。
- `seasonal` 只是按固定 `lag` 回看，不做日历节假日推断。
- 基线值使用绝对值为分母，避免负业务值改变变化率符号。

## robust_z

使用中位数和 MAD 的稳健异常检测器。

```yaml
- name: visits_robust_z_app
  type: robust_z
  metrics: [visits]
  dimension_values: [channel_app]
  severity: medium
  history: 21
  threshold: 3.5
  mad_zero: skip
  direction: both
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `history` | 14 | 严格在当前点之前的历史点数，至少 3 |
| `threshold` | 必填 | 大于 0 的 |z| 阈值 |
| `mad_zero` | `skip` | `skip` 或 `floor` |
| `floor_scale` | 1.0 | `mad_zero: floor` 时替换零尺度，必须大于 0 |
| `direction` | `both` | `above` / `below` / `both` |

公式：

```text
median = median(values[i-history:i])
MAD    = median(|values[i-history:i] - median|)
scale  = 1.4826 * MAD
z      = (value[i] - median) / scale
```

MAD=0 的明确行为：

- `skip`：跳过这个点，不产生事件。常量历史序列之后突然出现一个尖峰时，这是默认行为。
- `floor`：令 `scale = floor_scale`，继续计算 `z`。例如历史全为 10、`floor_scale=2`、新值 100 时 `z=45`。

历史点数不足 `history` 时不检测。事件基线为中位数，证据中记录 median、MAD、scale 与是否启用零尺度替代。

## changepoint

水平突变检测器，使用滑窗前后的均值差与基线波动。

```yaml
- name: sessions_level_shift_web
  type: changepoint
  metrics: [sessions]
  dimension_values: [channel_web]
  severity: high
  pre_window: 7
  post_window: 7
  threshold: 4.0
  min_scale: 1.0
  direction: increase
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `pre_window` | 7 | 候选点之前的窗口，至少 2 |
| `post_window` | 7 | 候选点起始的后窗，至少 2 |
| `threshold` | 必填 | 大于 0 的分数阈值 |
| `min_scale` | 1.0 | 前窗标准差的下限，必须大于 0 |
| `direction` | `both` | `increase` / `decrease` / `both` |

对每个有完整前后窗口的候选起点 `i`：

```text
pre_mean  = mean(values[i-pre_window:i])
post_mean = mean(values[i:i+post_window])
scale     = max(stdev(pre_window), min_scale)
score     = (post_mean - pre_mean) / scale
```

事件日期为后窗第一个周期；事件 `observed` 为后窗均值，`baseline` 为前窗均值，窗口覆盖整个后窗。

边界与稳定性：

- 前后窗口不完整时不评估。
- 前窗为常量时 `stdev=0`，使用 `min_scale` 避免除零；前后均值相等则分数为 0。
- 只输出“局部峰值”候选：分数必须不小于阈值、严格大于左邻候选且不小于右邻候选。平台峰取最早点，避免同一水平突变在相邻日期重复产生事件。
- 没有候选点时返回空列表，不抛异常。

## 检测器参数校验

检测器通过 `validate_params` 做独立校验：

- 未知参数：`E_CONFIG_SCHEMA`；
- 缺少必填阈值、方向对应的上下界：`E_CONFIG_SCHEMA`；
- 非数值、非有限数、非整数、越界：`E_CONFIG_SCHEMA`；
- 未注册检测器类型：`E_CONFIG_UNKNOWN_DETECTOR`。

`python -m drift_attr validate` 会加载数据与时间序列，并实例化所有检测器规则，但不执行完整分析或写台账。
