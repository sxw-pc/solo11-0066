# 保留期限清理（RETENTION）

`drift_attr purge` 把一个 run 当作单一保留单元：SQLite 明细、Web 可读状态与
已登记产物字节一起处理。默认 dry-run，只有显式 `--commit` 才改动数据。

## 命令

```bash
# 逐项列出 SQLite 行数、artifact 当前/登记 SHA、冲突与最终保留策略
python -m drift_attr purge --run-id demo-run

# 语义A：硬删除（不可恢复）
python -m drift_attr purge --run-id demo-run --commit --strategy hard --reason retention

# 语义B：整组隔离（可恢复/可取证）
python -m drift_attr purge --run-id demo-run --commit --strategy quarantine --reason retention
python -m drift_attr purge --run-id demo-run --restore   # 移回原目录，放弃清理
python -m drift_attr purge --run-id demo-run --discard   # 核对后永久删除隔离副本

python -m drift_attr purge --run-id demo-run --status    # 意图/墓碑状态
python -m drift_attr purge --run-id demo-run --abort     # 放弃尚未破坏字节的意图
```

`--db`、`--output-root` 缺省从 `--config` 的 `output.directory` /
`output.database` 推导；没有可读配置时退回 `output/` 与
`output/drift_attr.sqlite3`。

## 语义A：可审计硬清除

1. 在一个 SQLite 事务里写入 purge intent（完整 path/SHA 清单）并把
   `runs.status` 置为 `purging`；Web 从此刻起对详情和产物返回 404。
2. 逐文件校验：resolve 后仍在 `output_root` 内、不是符号链接、当前 SHA 等于
   登记 SHA、没有其他 run 或未完成意图引用同一规范路径。任一冲突则整组停止，
   冲突对象不删，状态记为 `conflicted`。
3. 逐文件 unlink，每个成功后把进度写回 intent；中途退出后重跑 `--commit`
   按已登记清单继续，已不存在的受管文件视为完成，不做二次判断。
4. 全部字节处理完后，在一个事务里删除 `anomaly_events`、
   `attribution_items`、`rule_snapshots`、`datasets`、`artifacts`、`runs`，
   并写入墓碑。墓碑只保留 run ID、清理时间、策略、原因码、artifact SHA 清单
   与冲突代码，不保留归因值、规则原文或路径。

## 语义B：隔离后清除

1. 同样的路径/SHA/共享校验；成功后把整个 `output/<run_id>/` 目录原子移动到
   `output/quarantine/<run_id>/run`（要求同一文件系统，否则拒绝）。
2. Web 永远不服务 quarantine（不在任何 artifact 路由可达范围），随后执行与 A
   相同的 SQLite 级联删除与墓碑写入；intent 保持 `quarantined`，字节仍在本地。
3. 管理员可 `--restore` 整组移回并恢复 run（数据库明细尚未删除时），或
   `--discard` 在核对后永久删除隔离副本并完成墓碑。

A 删除原件，清理后不可恢复；B 在隔离区保留可验证字节，清理后仍可离线取证。
两者都不删除输入 CSV：项目不拥有这些文件。

## Web 行为

- run 状态为 `purging` 时，`/runs/<id>`、`/runs/<id>/artifact/<kind>` 返回
  404，`/` 列表与 `/healthz` 计数都不包含它；`report` 命令拒绝导出。
- 产物响应前会重新计算 SHA；磁盘内容被替换时按缺失处理（404）。
- `/tombstones` 展示进行中的清理意图与已清理墓碑，不展示归因或规则正文。
