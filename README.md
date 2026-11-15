# 漂移归因台（drift-attr）

**漂移归因台**是一个纯本地、离线、确定性的业务指标异常检测与多维贡献归因工作台。它读取本地 CSV 指标数据和 YAML 声明式规则，识别指标异常事件，按配置维度拆解异常窗口相对基期窗口的变化贡献，生成 JSON / Markdown / 自包含 HTML 报告，并把运行参数、规则快照、数据指纹、异常证据和归因明细写入 SQLite 台账。

一句话定位：**在本机快速回答“哪个指标在什么时候异常、哪个维度贡献最大、证据是什么”。**

## 核心价值

- **声明式**：检测器、指标范围、窗口、阈值、归因维度和 Top-N 全部写在 YAML 中，不写代码即可调整分析口径。
- **可复核**：每个异常事件保留检测器来源、观测值、基线值、分数和公式化证据；运行记录同时写入规则指纹和数据指纹。
- **可归因**：将异常日期前的对比窗口与紧邻的基期窗口按维度值聚合，输出贡献额、贡献率、排名和“其他”剩余项。
- **完全离线、确定性**：运行期不请求网络，不使用 CDN、远程字体、外部 API、数据库或消息队列；相同数据与规则产生相同的异常、归因与排序。仓库自带虚构 fixture，一条命令即可演示。

## 适用与不适用场景

适合：小型数据团队在本地排查业务指标异常；运营人员按渠道、地区、品类查看贡献；数据工程开发者为检测器、补值策略和归因公式编写回归测试；需要保留本地可复核台账与报告产物的离线环境。

不适合：指标口径定义校准、SQL 口径静态核对或数据库连接；采样时钟抖动诊断、湖仓快照、表血缘或数据包准入；爬虫、采集调度、RPA、文件同步、真实告警推送；机器学习训练、自动调参、LLM 解释；多用户、RBAC、审批流、多租户或公网部署。

## 完全离线与本地监听

- 运行期不发起任何外部网络请求；报告 HTML 的 CSS 全部内联，Web 页面只使用仓库内 `style.css`。
- Web 服务默认且只允许监听 `127.0.0.1` 或 `localhost`，CLI 会拒绝其他 `--host`。
- Web 端是只读查看器：没有上传、远程 URL 导入、任意文件浏览或路径穿越入口。
- `/healthz` 返回本地服务状态，不访问任何外部依赖；仓库内没有真实密钥、令牌、公司名、真实域名、真实个人姓名或外部目标地址。

## 快速开始

要求 Python 3.11+、Flask、PyYAML、pytest。先安装依赖（已安装可跳过）：

```bash
python -m pip install -r requirements.txt
```

### 一条命令跑演示

```bash
make demo
# 无需 make 时使用等价命令
python -m drift_attr demo --config fixtures/rules.yaml --run-id demo-run
```

该命令完成：初始化/恢复 demo CSV 与规则 → 校验配置和数据 → 执行四类检测器 → 做维度归因 → 导出 JSON、Markdown、HTML 与归因 JSON → 写入 SQLite 台账。也可以分步执行 `init-demo`、`validate`、`analyze`。

### 一条命令启动本地查看器

```bash
make run
# 等价命令
python -m drift_attr serve --demo
```

打开 `http://127.0.0.1:8000/`。`serve --demo` 在台账尚无 `demo-run` 时会自动初始化并运行 demo，然后启动只读查看器。页面包括运行列表、运行详情、异常证据、归因明细和报告下载。

### 一条命令跑测试

```bash
make test
# 或
python -m pytest -q
```

测试只使用 `tmp_path` 和仓库内 fixture，不访问网络，不写用户主目录。

## 演示数据与预期结果

`fixtures/demo_metrics.csv` 是 35 天 × 3 个虚构渠道 × 4 个指标的日粒度长表，共 420 行。渠道为 `channel_web`、`channel_app`、`channel_store`。demo 规则会触发四类检测器各一次：

| 日期 | 指标 | 检测器 | 预期现象 |
| --- | --- | --- | --- |
| 2025-03-27 | sessions | changepoint | `channel_web` 后窗均值明显高于前窗 |
| 2025-03-31 | orders | threshold | `channel_store` 观测值低于固定下阈值 45 |
| 2025-04-02 | visits | robust_z | `channel_app` 稳健 z-score 超过 3.5 |
| 2025-04-04 | gmv | delta | `channel_web` 环比变化超过 25% |

`fixtures/expected/anomalies_demo.json` 与 `fixtures/expected/attribution_demo.json` 是稳定期望片段，测试用固定 `run_id=demo-run` 并规范化时间字段进行比较。

## 目录结构

```text
drift-attr/
  README.md  Makefile  requirements.txt  pyproject.toml
  drift_attr/
    __main__.py  cli.py  config.py  dataset.py  schema.py
    timeseries.py  attribution.py  normalize.py  engine.py
    report.py  html_report.py  store.py  demo.py  errors.py
    detectors/ threshold.py delta.py robust_z.py changepoint.py
    web/       app.py templates/ static/
  fixtures/        demo_metrics.csv  demo_wide_metrics.csv  rules.yaml
                   expected/anomalies_demo.json  expected/attribution_demo.json
  docs/            CONFIG.md  DETECTORS.md  ATTRIBUTION.md  ERROR_CODES.md
  tests/
```

## 配置说明

完整字段、默认值和宽表适配示例见 [docs/CONFIG.md](docs/CONFIG.md)。最小示例：

```yaml
version: 1

dataset:
  path: fixtures/demo_metrics.csv
  format: long
  encoding: utf-8
  duplicate_policy: warn      # warn | error
  empty_value: error          # error | drop
  date_field: date
  metric_field: metric
  dimension_name_field: dimension_name
  dimension_value_field: dimension_value
  value_field: value

series:
  granularity: day            # day | week
  aggregate: sum              # sum | mean
  fill_missing: zero          # zero | previous | drop
  week_start: monday          # monday | sunday

detection:
  detectors:
    - name: low_orders_store
      type: threshold
      metrics: [orders]
      dimension_names: [channel]
      dimension_values: [channel_store]
      severity: high
      direction: below
      lower: 45

attribution:
  dimensions: [channel]
  top_n: 2
  baseline_window: 7
  comparison_window: 7
  other_label: "其他"
  missing_policy: zero_fill   # zero_fill | common_dates

output:
  directory: output
  database: drift_attr.sqlite3
```

约定：

- 配置和数据路径中的相对路径按**当前工作目录**解析；从仓库根目录执行命令即可。
- 输出目录内每次运行写入 `<output>/<run_id>/`。
- `run_id` 只允许字母、数字、点、下划线和连字符，且不能以点开头；Web 和台账不会接受路径穿越式 ID。
- 未知字段、未知检测器、重复检测器名、重复归因维度、非法窗口和非法 Top-N 都会产生稳定配置错误。
- `dataset.format: wide` 是演示用适配器：配置 `dimension_field`、`dimension_name` 和 `metric_columns`，每个指标列展开成一条规范长表记录。

## 四类检测器

公式、参数边界和示例见 [docs/DETECTORS.md](docs/DETECTORS.md)。所有检测器都使用统一接口，输入一条按日期排序的 `Series`，输出若干确定性 `Detection`。

### 1. threshold（固定阈值）

当观测值严格越过下阈值或上阈值时产生事件：`below: observed < lower`；`above: observed > upper`；`both` 为两者之一。`direction` 为 `below`、`above`、`both`。等于阈值不触发，事件分数为观测值与触发边界的差。

### 2. delta（环比 / 同周期变化）

```text
relative score = (current - baseline) / |baseline|
absolute score = current - baseline
```

`comparison: previous` 比较上一期；`comparison: seasonal` 使用 `lag`（默认 7）找同周期基线。相对变化在基线为 0 时未定义，检测器会跳过该点并保持稳定，不抛异常。`direction` 支持 `increase`、`decrease`、`both`。

### 3. robust_z（稳健 z-score）

```text
median = median(history)
MAD    = median(|history[i] - median|)
scale  = 1.4826 * MAD
z      = (observed - median) / scale
```

历史窗口严格取当前点之前 `history` 期，至少 3 期。`MAD == 0` 时：`mad_zero: skip`（默认）跳过该点；`mad_zero: floor` 用 `floor_scale` 替代零尺度再计算。两条路径都不会抛出未捕获异常。`direction` 支持 `above`、`below`、`both`。

### 4. changepoint（水平突变）

```text
pre_mean  = mean(points[i-pre_window:i])
post_mean = mean(points[i:i+post_window])
scale     = max(stdev(pre), min_scale)
score     = (post_mean - pre_mean) / scale
```

只保留候选序列中的局部峰值，避免同一水平突变连续刷屏。窗口不足时不出事件；`pre_window`、`post_window` 至少为 2。常量序列的前窗标准差为 0，此时由 `min_scale` 提供稳定尺度；前后均值相同则分数为 0。

多个检测器命中同一“指标 × 维度 × 日期”位置时，报告层合并为一个异常位置，但 `raw_events` 和数据库中的原始事件全部保留。

## 维度贡献归因

完整规则见 [docs/ATTRIBUTION.md](docs/ATTRIBUTION.md)。对每个异常日期 `D`：

- **异常/对比窗口**：截至 `D` 的 `comparison_window` 个周期；
- **基期窗口**：紧邻异常窗口之前的 `baseline_window` 个周期。

对每个归因维度的维度值：

```text
current_value  = 异常窗口内该维度值聚合值
baseline_value = 基期窗口内该维度值聚合值
contribution   = current_value - baseline_value
total_change   = 所有维度值 contribution 之和
contribution_rate = contribution / total_change   （total_change = 0 时为 null）
```

实现保证：

- 缺失日期按配置的补值策略进入序列；缺测归因口径由 `attribution.missing_policy` 决定：`zero_fill`（默认）把无观测日期按 0 计入完整日历窗口，`common_dates` 只在所有可比类别共同可观测的日期交集上归因，不可比类别输出 `status: incomparable` 与原因而不伪造数值。
- 每个归因项输出两个窗口的覆盖统计（应有/观测/插补/缺失/实际使用日期数与覆盖率），报告同时记录实际用于归因的日期集合，真实 0 值与无观测始终区分。
- 正负贡献可以混合；贡献率可大于 100% 或小于 -100%，这是总变化较小或正负抵消时的正常现象。
- 总变化为 0 时，所有贡献率输出 `null`，公式标记 `undefined`，解释文本写明“不适用（总变化为 0）”。
- 排序稳定：贡献额绝对值降序，再按维度值字典序升序。
- 超过 `top_n` 的维度值合并为“其他”剩余项；该项保留汇总的当前值、基期值、贡献额、贡献率并标注 `is_other: true`。每个归因项输出可复核公式摘要与模板化解释文本，不调用任何外部模型。

## 报告与 SQLite 台账

每次成功分析生成：

```text
output/<run_id>/report.json            # 机器可读完整报告
output/<run_id>/report.md              # Markdown 复核报告
output/<run_id>/report.html            # 自包含 HTML，CSS 内联
output/<run_id>/attribution.json       # 归因明细导出
output/<run_id>/rules.snapshot.yaml    # 本次运行规则快照
output/drift_attr.sqlite3              # SQLite 台账
```

JSON 报告包含运行元数据、配置/数据指纹、序列摘要、规则摘要、严重度统计、异常位置、每个位置的原始检测事件、归因窗口、归因明细与产物路径。HTML 报告不包含 `<script>`、CDN、远程字体或 `http(s)://` 引用。

SQLite 表：

| 表 | 作用 |
| --- | --- |
| `runs` | 运行 ID、开始/结束时间、状态、配置/数据/规则指纹、计数、错误摘要 |
| `rule_snapshots` | 每次运行的规则原文与指纹 |
| `datasets` | 数据路径、指纹、行数、格式、警告与数值范围 |
| `anomaly_events` | 原始检测器事件，含观测值、基线、分数、严重度、证据 JSON、窗口 |
| `attribution_items` | 归属某个异常位置的维度贡献、排名、公式与解释 |
| `artifacts` | 报告产物类型、相对路径、格式与 SHA-256 |

同一 `run_id` 重跑会在一个事务内替换旧运行及其子记录；事件和归因主键由固定输入确定性生成，不会产生重复主键冲突。

## CLI

```bash
python -m drift_attr demo [--config fixtures/rules.yaml] [--run-id demo-run]
python -m drift_attr init-demo [--force]
python -m drift_attr validate --config fixtures/rules.yaml
python -m drift_attr analyze --config fixtures/rules.yaml [--run-id demo-run]
python -m drift_attr report --run-id demo-run --format markdown --out export.md
python -m drift_attr runs --db output/drift_attr.sqlite3
python -m drift_attr serve --demo --host 127.0.0.1 --port 8000
```

- `validate` 只校验配置、数据、时间序列与检测器参数，不写报告或台账；`analyze` 默认生成新的 `run-...` ID，需要可复现时显式传 `--run-id`。
- `report` 从台账定位 `report_json` 产物并重新渲染；`runs` 打印运行列表；`serve` 仅允许本机地址，错误主机名返回 `E_WEB_HOST`。

Web 路由：

| 路由 | 说明 |
| --- | --- |
| `/` | 运行列表；无数据时显示空状态 |
| `/runs/<run_id>` | 运行详情、异常证据、归因明细、产物 |
| `/runs/<run_id>/artifact/<kind>` | 只读查看报告产物的已知类型 |
| `/healthz` | 本地健康检查 JSON |

## 错误码

错误码表索引见 [docs/ERROR_CODES.md](docs/ERROR_CODES.md)。错误字符串形如：

```text
[E_DATA_DATE] fixtures/demo_metrics.csv:12 field=date: 无法解析日期: 'not-a-date'，期望 YYYY-MM-DD
```

错误包含稳定代码、文件路径、行号、字段名和原因摘要，CLI 只打印错误而不输出未处理 traceback。

## 测试与验收

```bash
make test
python -m pytest -q
```

测试覆盖：CSV 读取与错误码、宽表适配、YAML 严格校验、日/周聚合与三种补值、四类检测器正常与边界、MAD=0、贡献额/贡献率/排序/Top-N/“其他”项、总变化为 0、SQLite 幂等、报告期望片段、CLI smoke、Flask 页面 200/404/空状态、本机监听限制。

手工验收：`make demo` 成功退出且 `output/demo-run/` 下存在三种报告、归因 JSON 和规则快照；SQLite 中 `runs`、`anomaly_events`、`attribution_items`、`artifacts` 有记录；`make run` 后访问 `http://127.0.0.1:8000/` 能看到 `demo-run` 的 4 个异常位置和归因表；打开 `report.html` 后样式完整且不引用远程资源。

## 明确不实现

- 不实现真实数据库连接、数仓连接、API 拉取、消息队列消费。
- 不实现爬虫、采集调度、RPA、文件同步。
- 不实现指标口径定义校准或 SQL 静态解析。
- 不实现采样时钟抖动诊断、湖仓快照、表血缘、数据包准入。
- 不实现机器学习模型训练、自动调参、LLM 解释或外部模型调用。
- 不实现用户登录、权限 RBAC、多租户、审批流。
- 不实现真实告警推送、邮件、短信、Webhook。
- 不实现复杂前端框架、图表库 CDN、数据库迁移框架。
- 不实现任意文件上传、远程 URL 导入、目录遍历式文件浏览。
