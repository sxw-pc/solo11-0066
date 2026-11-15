# 漂移归因台分析报告

- 运行 ID：`golden-run`
- 状态：success
- 开始时间：2026-01-02T03:04:05Z
- 结束时间：2026-01-02T03:04:05Z
- 配置指纹：`466af8d905d7afd9f0c7d72c8e1f05c31e86d85ea70e56cfe0f04075bc47b55e`
- 数据指纹：`9df9f320873b52ae7287f4b86c59095a8c5c8c663ffa6263c2df73fa2d7951ea`

## 概要

| 指标 | 值 |
| --- | --- |
| 原始检测事件 | 4 |
| 合并异常位置 | 4 |
| 归因明细 | 12 |
| 严重度分布 | {'low': 0, 'medium': 2, 'high': 2, 'critical': 0} |

## 异常与归因

### 2025-03-27 · sessions · channel=channel_web

- 严重度：high
- 命中检测器：changepoint
- 规则：sessions_level_shift_web
- 归因窗口：对照 2025-03-14 ~ 2025-03-20 (7 期) / 异常 2025-03-21 ~ 2025-03-27 (7 期)
- 解释：2025-03-27 的 sessions （channel=channel_web）出现异常，命中检测器：changepoint。 2025-03-27 起后窗均值 898.857 较前窗均值 701.143 变化，突变分数 35.0558。 按 channel 归因，channel_web 的贡献额为 +202.00，占总变化 83.47%。

| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 其他项 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | channel=channel_web | 5098.0 | 4896.0 | +202.000000 | 83.47% | 否 |
| 2 | channel=channel_app | 3652.0 | 3632.0 | +20.000000 | 8.26% | 否 |
| 3 | channel=其他 | 2108.0 | 2088.0 | +20.000000 | 8.26% | 是 |

证据：

- `sessions_level_shift_web` / changepoint：2025-03-27 起后窗均值 898.857 较前窗均值 701.143 变化，突变分数 35.0558（score=35.055766）

### 2025-03-31 · orders · channel=channel_store

- 严重度：high
- 命中检测器：threshold
- 规则：low_orders_store
- 归因窗口：对照 2025-03-18 ~ 2025-03-24 (7 期) / 异常 2025-03-25 ~ 2025-03-31 (7 期)
- 解释：2025-03-31 的 orders （channel=channel_store）出现异常，命中检测器：threshold。 2025-03-31 观测值 20 低于固定阈值 45。 按 channel 归因，channel_store 的贡献额为 -46.00，占总变化 86.79%。

| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 其他项 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | channel=channel_store | 440.0 | 486.0 | -46.000000 | 86.79% | 否 |
| 2 | channel=channel_web | 834.0 | 842.0 | -8.000000 | 15.09% | 否 |
| 3 | channel=其他 | 631.0 | 630.0 | +1.000000 | -1.89% | 是 |

证据：

- `low_orders_store` / threshold：2025-03-31 观测值 20 低于固定阈值 45（score=-25.0）

### 2025-04-02 · visits · channel=channel_app

- 严重度：medium
- 命中检测器：robust_z
- 规则：visits_robust_z_app
- 归因窗口：对照 2025-03-20 ~ 2025-03-26 (7 期) / 异常 2025-03-27 ~ 2025-04-02 (7 期)
- 解释：2025-04-02 的 visits （channel=channel_app）出现异常，命中检测器：robust_z。 2025-04-02 观测值 1700 的稳健 z-score 为 151.7604，超过阈值 3.5。 按 channel 归因，channel_app 的贡献额为 +904.00，占总变化 95.26%。

| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 其他项 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | channel=channel_app | 6500.0 | 5596.0 | +904.000000 | 95.26% | 否 |
| 2 | channel=channel_store | 2824.0 | 2784.0 | +40.000000 | 4.21% | 否 |
| 3 | channel=其他 | 4205.0 | 4200.0 | +5.000000 | 0.53% | 是 |

证据：

- `visits_robust_z_app` / robust_z：2025-04-02 观测值 1700 的稳健 z-score 为 151.7604，超过阈值 3.5（score=151.760421）

### 2025-04-04 · gmv · channel=channel_web

- 严重度：medium
- 命中检测器：delta
- 规则：gmv_day_over_day_web
- 归因窗口：对照 2025-03-22 ~ 2025-03-28 (7 期) / 异常 2025-03-29 ~ 2025-04-04 (7 期)
- 解释：2025-04-04 的 gmv （channel=channel_web）出现异常，命中检测器：delta。 2025-04-04 较滞后 1 期上升 0.5385。 按 channel 归因，channel_web 的贡献额为 +900.00，占总变化 119.21%。

| 排名 | 维度值 | 异常窗口 | 对照窗口 | 贡献额 | 贡献率 | 其他项 |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | channel=channel_web | 15000.0 | 14100.0 | +900.000000 | 119.21% | 否 |
| 2 | channel=channel_app | 10380.0 | 10540.0 | -160.000000 | -21.19% | 否 |
| 3 | channel=其他 | 7015.0 | 7000.0 | +15.000000 | 1.99% | 是 |

证据：

- `gmv_day_over_day_web` / delta：2025-04-04 较滞后 1 期上升 0.5385（score=0.538462）

## 产物

- report_json: `output/golden-run/report.json`
- report_markdown: `output/golden-run/report.md`
- report_html: `output/golden-run/report.html`
- attribution_json: `output/golden-run/attribution.json`
- rule_snapshot: `output/golden-run/rules.snapshot.yaml`

本报告由本地模板生成，不包含外部模型调用或远程资源引用。
