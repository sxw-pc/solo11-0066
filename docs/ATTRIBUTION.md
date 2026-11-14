# 维度贡献归因（ATTRIBUTION）

归因阶段对每个合并后的异常位置，按 `attribution.dimensions` 中声明的维度，将异常窗口与基期窗口进行对比，计算各维度值的贡献额、贡献率和排名。

## 窗口定义

对异常周期 `D`（日粒度时是具体日期，周粒度时是周期起点）：

- **异常/对比窗口**（comparison window）：截至 `D` 的 `comparison_window` 个周期，包含 `D`；
- **基期窗口**（baseline window）：紧邻异常窗口之前、长度为 `baseline_window` 的周期集合。

示例：日粒度，`comparison_window: 7`、`baseline_window: 7`，异常日期为 `2025-03-31`：

```text
异常窗口：2025-03-25 ~ 2025-03-31
基期窗口：2025-03-18 ~ 2025-03-24
```

事件本身的检测器窗口（如 changepoint 的后窗）只回答“在哪里发生突变”。归因窗口由 `attribution` 配置独立定义，报告会在每个异常位置记录实际使用的窗口起止日期与周期数。

窗口长度不同时，贡献额仍按各自窗口的聚合值相减，不在内部做日均换算。需要可比的速率时，应将 `baseline_window` 和 `comparison_window` 设为相同长度。

## 聚合与贡献公式

对每个归因维度值 `v`：

```text
current_value(v)  = 异常窗口内 metric 在维度值 v 上的聚合值
baseline_value(v) = 基期窗口内 metric 在维度值 v 上的聚合值
contribution(v)   = current_value(v) - baseline_value(v)

total_change = Σ contribution(v)
contribution_rate(v) = contribution(v) / total_change
```

聚合值来自 `timeseries` 阶段生成的序列：
- 同一周期多行按 `series.aggregate`（`sum` / `mean`）合并；
- 缺失周期按 `series.fill_missing` 补值；
- `zero` / `previous` 产生的补值参与归因，报告 `series.imputed_points` 记录补值规模；
- `drop` 留下的缺失周期在该归因窗口中按 0 计入。

某维度值只在一个窗口出现时，另一窗口值按 0 处理。例如只在新窗口出现的维度值贡献额等于其当前值；只在旧窗口出现的维度值贡献额为负。

## 总变化为 0

当 `|total_change| < 1e-12` 时：

- 每个归因项的 `contribution_rate` 为 `null`；
- 公式摘要写 `rate=undefined (total_change=0)`；
- 解释文本写“贡献率 不适用（总变化为 0）”；
- 贡献额、当前值、基期值、排名仍正常输出。

这适用于“所有维度贡献都为 0”和“正负贡献恰好完全抵消”两种情况。

## 正负贡献与贡献率范围

- 贡献额可正可负，表示维度值推动指标上升或下降。
- 贡献率不截断到 0%-100%。当总变化很小或正负贡献相互抵消时，单个维度的贡献率可能大于 100% 或小于 -100%。
- 同一异常位置正负贡献混合是正常情况；报告不强行归零，也不丢弃负贡献。

## 排序规则

对每个异常位置、每个归因维度：

1. 按 `abs(contribution)` 降序；
2. 若绝对值相同，按维度值字典序升序；
3. 取前 `top_n` 项；
4. 剩余项（若存在）合并为“其他”项，排在 Top-N 之后。

排序不依赖输入行序，因此相同数据与配置得到稳定排名。

## Top-N 与“其他”项

设维度值共有 `k` 个，`top_n = n`：

- `k <= n`：输出全部维度值，不产生“其他”项；
- `k > n`：输出前 `n` 个维度值，并额外输出一个 `is_other: true` 的“其他”项；
- “其他”项的：
  - `dimension_value` = `attribution.other_label`（默认“其他”）；
  - 当前值/基期值/贡献额 = 被截断维度值对应值之和；
  - 贡献率 = 其余贡献之和 / `total_change`（总变化为 0 时为 `null`）；
  - **排名为 `n + 1`**，但其贡献额绝对值未必小于第 `n` 项，因为“其他”是合计数。

“其他”项保留汇总公式与解释文本，并在 JSON、SQLite、Web 页面和 HTML 报告中标注 `is_other`，便于区分真实维度值与剩余项。

## 输出字段

每个归因项输出：

| 字段 | 说明 |
| --- | --- |
| `item_id` | 由运行、异常位置、归因维度、维度值和排名确定性生成 |
| `group_id` | 合并后的异常位置 ID |
| `event_id` | 该异常位置的代表原始事件 ID |
| `attribution_dimension` | 归因维度名 |
| `dimension_value` | 维度值或“其他”标签 |
| `current_value` | 异常窗口值 |
| `baseline_value` | 基期窗口值 |
| `contribution` | 贡献额 |
| `contribution_rate` | 贡献率，总变化为 0 时 null |
| `rank` | 排名 |
| `is_other` | 是否为剩余项 |
| `formula` | 当前值、基期值、贡献额与贡献率的公式摘要 |
| `explanation` | 中文模板化解释，不含外部模型调用 |

## 解释文本

示例（虚构业务值）：

```text
channel=channel_web 在异常窗口为 5098.00，对照窗口为 4896.00，
贡献额 +202.00，贡献率 83.47%。
```

每个异常位置还有一段整体说明，包含异常日期、指标、维度、命中检测器、主要证据和首个归因维度的 Top-1 贡献。文本完全由本地模板拼装，不包含真实公司名、真实人名或真实域名。

## 多个归因维度

`attribution.dimensions` 可以声明多个维度。每个维度独立计算 Top-N、“其他”项和排序，在 JSON 报告中按维度名字典序组织，在 SQLite 中逐项写入 `attribution_items`。

同一异常位置的所有归因项使用同一个 `group_id`；归因维度和维度值共同决定项 ID，因此不同维度的同名维度值不会冲突。
