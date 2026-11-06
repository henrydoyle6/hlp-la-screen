# 官方历史 L2 生成 CSV2

`official_l2.py` 从已完成的 CSV1 建立任务，通过官方历史 L2 生成每 episode 一行的 CSV2。云端工具位于 `tools/official_l2/`，运行计算与源桶 `hyperliquid-archive` 同在 `us-east-1`。原本地 L4 三阶段流程继续由 `run_hlp.py` 启动。

## 输入与任务范围

支持 `hlp-csv-v3` 的英文或本项目中文 45 列 CSV1。导入校验 episode 哈希、事件数量、接管数量、金额、加权均价和首末时间。已有诊断保存在原样复制的 `csv1.csv` 中，内部矛盾的 episode 单独输出失败原因。未知 `target_block` 保持空值。

本次 CSV2 根据完整 episode 的最后接管时间 Tn 选择任务，范围为 `2025-03-22T00:00:00Z <= Tn < 2025-10-22T00:00:00Z`，包含 10 月 21 日整天。接管组、缓冲接管、partial 与分段标记均从 CSV1 继承。

## 时间选择与档案顺序假设

快照时间来自 `raw.data.time` 的交易所时钟。历史包装中的外层 `time` 是采集时钟，小时对象按采集时钟划分。实际样本出现了下一小时对象包含上一小时交易所时间的记录，因此选择时保留纳秒 Tn，并读取整份相关对象。

选择目标为范围内 `snapshot_time >= Tn` 的最近可用快照，同时记录 `snapshot_lag_ms`。同一币种、同一小时的多个 episode 共用一次对象读取。当前文件中乱序记录会完整比较，同一候选时间内容冲突会输出明确诊断。

本次采用用户确认的 `validated_next_hour` 方法。取得候选后，完整检查后续小时，只有该对象的最小交易所时间严格晚于候选，且采集时钟符合该对象小时，才完成该 episode。该方法依赖已抽样验证的档案顺序假设，对未扫描的更远文件仍存在方法上的边界。

算法 `official-l2-v2` 按每个候选判断异常的实际影响。完整文件中出现更早候选时先更新候选，同一时间内容冲突时保留冲突。当前文件最小时间仍处于候选或之前，则继续检查后续对象；采集时钟偏离对象小时的文件只参与候选比较，由后面的正常完整对象提供完成边界。缺对象时继续向后查找。文件内乱序和相邻文件的时间重叠写入诊断，完整文件中全部时间都晚于某候选时，可以为该候选建立边界。每个 episode 在自己的所需范围内完成后，继续处理下一个需求小时。

初次全量尝试使用的 `official-l2-v1` 曾将文件内乱序等顺序异常扩大为该币种一直扫到范围终点，导致无关时段读取增长。v2 修复这一扫描范围问题，保留完整文件比较、同时间冲突、异常继续查找与精确 Decimal 指标。

`strict` 方法会扫描该币种从最早需求小时直到范围终点。两种方法均没有最大快照时间差筛选阈值。权限错误会停止来源，文件损坏导致无法确认最近候选时，受影响 episode 输出 `L2_SELECTION_UNCONFIRMED`。

## CSV2 的 47 列

原 `CSV2_FIELDS` 的 46 列保持顺序，最后追加 `snapshot_lag_ms`。

| 字段 | L2 内容 |
| --- | --- |
| `episode_id`、`episode_input_hash`、接管与 partial/split 指标 | 继承 CSV1 |
| `tn`、`target_block` | 完整 episode 的最后接管时间与已知高度 |
| `book_state_point` | `post_takeover_l2_snapshot` |
| `book_side` | 多头接管保存 `bid`，空头接管保存 `ask` |
| `snapshot_file`、`snapshot_source_id` | 实际 S3 对象路径与对象身份、记录行的摘要 |
| `snapshot_time`、`snapshot_selection_status` | 交易所时间与 `nearest_at_or_after_tn` |
| `snapshot_member`、`snapshot_block` | 来源实际提供时填写，否则为空 |
| 回放高度、回放覆盖、`diff_sources_json` | 空值 |
| `orders_json` | 所选一条快照的完整已发布单侧价格档 |
| `order_count` | 每档均有可靠 `n` 时为其总和，否则为空 |
| `total_depth`、`side_notional` | 使用 Decimal 累加数量与价格乘数量 |
| `snapshot_lag_ms` | 快照时间减 Tn，单位为毫秒，可含小数 |

价格档按平仓使用顺序排列，买单从高到低，卖单从低到高。例如 `{"price":"100","qty":"2","n":3}` 表示该价格档总数量为 2、订单数为 3。深度计算直接使用 `qty`。空侧保存 `[]` 并标记 `book_ready`。

这些结果使用官方已发布的聚合档位。CSV3 的既有逐单读取器使用 OID，本次 L2 运行在 CSV2 完成后结束。原 L4 回放、LA 公式与 CSV3 计算继续遵循原受保护基线。

## 本地已保存档案的调用

安装 `requirements.txt` 后，可从 Python 显式传入本地历史对象根目录与独立输出目录。

```python
from official_l2 import LocalSource, run_csv2

run_csv2(
    "/data/input/csv1.csv",
    "/data/results/l2-csv2-v0.3.4",
    LocalSource("/data/hyperliquid-archive"),
    start="2025-03-22T00:00:00Z",
    end="2025-10-22T00:00:00Z",
    resume=True,
    policy="validated_next_hour",
)
```

本地根目录包含 `market_data/YYYYMMDD/H/l2Book/COIN.lz4`。输出目录保存六个长期文件 `csv1.csv`、`csv2.csv`、`csv3.csv`、`run.log`、`progress.sqlite`、`cache.sqlite`。本次 `csv3.csv` 仅建立表头。

恢复校验输入副本和运行配置，验证 CSV2 表头与已提交记录的字节摘要，并截去未提交尾部。已完整扫描的对象由缓存复用，中断的压缩对象可从安全位置重新读取。

运行配置同时保存算法版本和 `source_fingerprint`。算法版本变化，或来源与批准计划的 fingerprint 变化，都会拒绝续用旧检查点。`official-l2-v2` 使用新运行目录，保留 v1 尝试及其原始输出作为诊断证据。上传 bundle 的 SHA-256 另行验证所部署源码的身份。

## 受限的同区云端运行

`cloud_worker.py`、`infra_sample.yaml`、`deploy_sample.sh` 用于样本；`cloud_worker_full.py`、`infra_full.yaml`、`deploy_full.sh` 用于已批准的全量范围。部署和删除资源属于有费用与权限影响的操作，需要本次任务明确的范围、限额和有效期。

工作机从 IMDSv2 获取指定实例角色，部署器使用明确批准的 CloudShell 会话。程序在数据请求前核验账户及角色，确认桶区域为 `us-east-1`。源桶访问设置 `RequestPayer="requester"`，IAM 使用 `aws:SourceVpce` 限制源数据请求经过本次 S3 Gateway Endpoint。实例与源桶同区，模板使用 S3 Gateway Endpoint 路由。

计划包括 CSV1 SHA-256、episode 清单、允许搜索的对象范围，以及请求数、字节数、单对象、缓存、输出、数据扫描时间和实例运行时间上限。允许对象清单是搜索上界，程序只访问仍有需求的币种小时。预算或有效期耗尽时保留未完成进度。既有云端样本的实例与身份记录属于本地运行证据，公开验证仅保留必要的汇总信息。

`generate_full_plan.py` 为这次已确认的 CSV1 创建可审阅计划，绑定 354 个 episode 和输入 SHA-256。离线生成示例为

```bash
python tools/official_l2/generate_full_plan.py \
  --csv1 /data/input/csv1.csv \
  --output /data/review/full_plan.json
```

样本与全量启动器接收经过批准的 bundle，并使用 `deploy`、`fetch`、`cleanup` 操作管理本次临时资源。获取并核验六个结果文件后，再执行本次资源清理。实际运行以审核后的计划、资源配置和发布版本为准。

## 验证范围

已完成的真实样本有 10 个 episode，8 条 `book_ready` 和 2 条诊断。8 条结果的继承指标、单侧 Decimal 指标、哈希、缓存候选和后续小时证据均经过独立复核，其中 2 条 BTC 结果另用同身份原始文件的 6,549 条记录重新选择。其他 6 条的独立复核依赖保留的缓存证据，完整原始云端流未保留。

BIO 的样本从 Tn 所在小时至范围终点共 42 个对象确认不存在。PAXG 的历史样本 `SAMPLE_SEARCH_SCOPE_EXHAUSTED` 只表示三个样本小时后的范围当时尚待检查；最终全量另行检查至范围终点。73 项相关离线测试覆盖 L2 选择、恢复、云端身份、区域、全量计划限制、累计预算和 v2 的候选作用域回归。

[最终完整 CSV2](../results/csv2_l2_20250322_20251021/README.md)覆盖全部 354 个 episode：352 条 `book_ready`、2 条 `NO_L2_AT_OR_AFTER_TN_IN_RANGE`；47 列、39 个未知目标区块保持原约定。352 条可用结果的 lag 为 0–3633 ms，中位数 144 ms，93 条为 0。5 条已处理的 `L2_OBJECT_TIME_DISORDER` 备注仍属于 `book_ready`，不是错误跳过；缓存损坏对象为 0。

BIO EP000354 从 Tn `2025-10-20T06:35:11.250Z` 所在小时至终点的 42 个小时对象、PAXG EP000353 从 Tn `2025-10-16T22:10:29.953Z` 所在小时至终点的 122 个小时对象均不存在。结论仅限这些 Tn 后的请求范围，不指整个币种历史缺失。

全量独立审计验证全部 354 条提交哈希及继承字段，重算全部 352 条可用结果的单侧 Decimal/`n`、簿哈希和 lag，并复核保留的逐对象候选与完整扫描边界。完整原始云端流未保留，未据此声称全部原始记录已独立再次扫描；部署来源绑定和运行源码独立取回的边界见[紧凑核验记录](../validation/official_l2_full_20261106.json)。
