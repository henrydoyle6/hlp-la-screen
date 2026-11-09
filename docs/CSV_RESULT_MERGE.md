# 离线生成 L2 CSV3 与历史结果合并

`csv_result_merge.py` 读取已完成的 CSV1、47 列 L2 CSV2 和已有 CSV1/2/3 结果 ZIP。它不读取原始 fills、RMP 或 diff，不连接 AWS，不改变 `run_hlp.py` 的原生 L4 路径。

```bash
python csv_result_merge.py \
  --csv1 /data/input/csv1_20250322_20261031_中文.csv \
  --l2-csv2 /data/input/csv2_20250322_20251021.csv \
  --previous-zip /data/input/completed_csv1_csv2_csv3.zip \
  --output /data/results/combined_20250322_20260518
```

本入口针对这两段历史结果：先生成 2025-03-22 至 2025-10-21 的 L2 CSV3，再合入旧包中 2025-10-22 至 2026-05-18 的 56 组。目标日期元数据为 UTC `2025-03-22T00:00:00Z` 至 `2026-05-19T00:00:00Z`（末端不含），正式筛选首尾各保留一小时缓冲；已有完整 episode 不截断、不重新分组。输入不匹配或会改变原有正式/缓冲成员时停止。

## 同一 LA 公式与不同盘口来源

数学计算调用原封未动的 `hlp_la_screen.simulate_close`，使用 60 位 Decimal。多头依次卖向 bid，空头依次买入 ask；LA 为已成交部分的平仓金额与按接管均价分摊的成本之差，方向沿用原定义。未成交部分不伪装为已经平仓。

L2 的一条记录是价格档，不是一个订单。`qty` 已是该档总数量，不能再乘 `n`。执行轨迹按价格档记录，`oid=null`；某档全部消耗时，其订单数可由 `n` 确认，部分消耗且 `n=1` 时也能确认一笔。部分消耗且 `n>1` 时，`executed_order_count` 留空。`executed_price_level_count` 始终表示实际消耗的价位数。

L2 每侧只保存来源发布的有限档位。`calculated_full` 表示其保存档位足够覆盖该组数量；`calculated_partial` 只给出在保存档位内的部分模拟结果，不能推断更深真实盘口没有流动性。L2 CSV3 的现有 `issues_json` 包含 `L2_AGGREGATED_LEVEL_SIMULATION` 及快照时间/lag，CSV2 继续以 `book_state_point=post_takeover_l2_snapshot` 标明来源。原 46 列 CSV3 不添加指标。

旧 L4 结果继续为 `book_state_point=block_end`，包含真实 OID。程序先从提供的 CSV2 重新运行原引擎核对旧 56 组结果，再复用其全部原有数值和执行轨迹；没有重新读取旧原始快照/diff。混合结果不能描述为全部在 block B 块末进行 L4 重建。

## 关联与输入保留

两段事件 ID 生成规则不同。程序按时间、区块、币种、方向、价格与数量的完整多重集核对旧 1,445 笔接管，保留各自原事件 ID。早段组号保持不变，旧段组号映射到权威全量 CSV1 的全局组号，防止 EP000001 等冲突。

运行编号、日期元数据与组编号不在既有 episode 输入哈希中，原哈希继续核验。CSV2 的运行编号/组编号发生关联更新后，CSV3 的 `csv2_row_hash` 必须随之更新；盘口哈希和数值不变。完整映射写入本地进度库，紧凑公开核验记录保留输入与结果 SHA。

4 条 CSV1 全局覆盖诊断保留。缺失数据区间内是否还有未知接管无法由现有输入证明；`partial_episode` 原值不变，也不能解释为原始归档无缺口。BIO/PAXG 的两条无快照诊断与所有未知区块号继续留空，不补造数据。

## 输出与恢复

`l2_csv3_run/` 和 `combined_run/` 各恰好保存 `csv1.csv`、`csv2.csv`、`csv3.csv`、`run.log`、`progress.sqlite`、`cache.sqlite` 六个长期文件。内部 CSV1 为原 45 列英文规范，最终交付副本只翻译为既有中文表头；CSV2 为 47 列，旧 L4 行的 `snapshot_lag_ms` 留空，CSV3 为原 46 列。

本地结果检查点使用独立的 `csv-result-merge-v1` 配置，绑定三份输入 SHA，沿用 CSV fsync 后提交 SQLite 进度与行哈希的协议。同配置可恢复已提交结果；输入或版本变化拒绝混用。不能把本入口的进度库强行交给原生 `run_hlp.py`。

真实结果见[合并结果](../results/csv123_20250322_20260518/README.md)与[前段 CSV3](../results/csv3_l2_20250322_20251021/README.md)。合成回归和真实数据独立复算是两类证据，详见 `validation/combined_csv123_20261109.json`。
