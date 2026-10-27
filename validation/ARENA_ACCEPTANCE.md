# v0.3.1 Arena 订单存储兼容验收

日期：2026-10-27。用户提供三组前后 ABCI 快照及三个日期的本地原始 diff，
要求补齐 4 个 skipped episode、重算 56 个 CSV3，并在新分支验收后合并。
本轮使用 WSL Ubuntu / Python 3.12，未扫描原始 fills。

## 已复现的根因与修复范围

未修改的 v0.3.0 `extract_rmp_l4.py` 读取 972200000，MELANIA/TRUMP 均退出失败：
`Missing asset/book_orders in snapshot`。

三份前快照的所有订单簿条目均使用新存储：

| 高度 | 条目数 | 共同字段 |
| --- | --- | --- |
| 972200000 | 707 | halfs, oid_to_key, cloid_tracker, bod, asset, user_states, last_trade_px, total_sz |
| 985700000 | 714 | 同上 |
| 1000750000 | 743 | 同上 |

`bod.e` 是槽数组：`{"o": order}` 为占用槽，`{"v": ...}` 为空闲槽；
`oid_to_key` 为真实 OID 与槽下标的映射。双侧 `halfs[].levels` 保存价格和链首/链尾。
订单中仍有真实 `o`、方向 `c.s`、整数价格 `c.l`、剩余量 `r`，无需猜测或聚合。

新适配器先验证占用数量、映射唯一性、真实 OID、双侧价格、首尾和前后链接及完整覆盖，
再规范化为既有 `asset/book_orders` 输入。空闲槽不当作订单，零剩余量保留给后续 remove。
旧 book_orders 与新 arena 可逐市场并存；成功转换才发布缓存。

31 个保护片段及保护基线未变，包括原 to_l4、精度/剩余量换算、diff、LA 和精确比较。
提取缓存版本升级至 rmp-l4-sqlite-v5；新模块纳入运行哈希，旧数据库不能混续。
Python 3.10 的纳秒时间解析问题不在本次存储修复范围，README 生产安装固定 Python 3.12。

## 自动测试

`python -m unittest discover -s . -p 'test_*.py' -v`：**210 项通过，65.305 秒**。
新增 10 项 arena 测试覆盖空闲槽、空市场、超过 2^53 的真实 OID、零剩余量、
旧新混合、坏映射/计数/槽、坏链/价格/方向、失败局部化、缓存复用、同价独立及原比较入口。
见 [完整日志](arena_full_tests.log)。`tools/check_release.py --unreleased` 通过。

## 原入口真实前后快照精确比较

直接运行 `test_order_book_replay.py --compare`。先只用前 state 与原 diff 回放完整目标块，
所有币种完成后再打开后 state 作独立答案；不以未来快照补洞。
双侧所有活跃订单的 oid、side、price、remaining_qty 精确比较，无容差、无同价合并。

| 前块 → 后块 | 币种 | 回放/答案订单数 | 缺失 / 额外 / 变化 |
| --- | --- | --- | --- |
| 972200000 → 972250000 | BTC | 52191 / 52191 | 0 / 0 / 0 |
| 同上 | MELANIA | 381 / 381 | 0 / 0 / 0 |
| 同上 | TRUMP | 1014 / 1014 | 0 / 0 / 0 |
| 985700000 → 985750000 | BTC | 53843 / 53843 | 0 / 0 / 0 |
| 同上 | WIF | 465 / 465 | 0 / 0 / 0 |
| 1000750000 → 1000800000 | BTC | 48244 / 48244 | 0 / 0 / 0 |
| 同上 | TST | 113 / 113 | 0 / 0 / 0 |

完整结果：[4 月 25 日](arena_exact_april25.log)、[5 月 6 日](arena_exact_may06.log)、
[5 月 18 日](arena_exact_may18.log)。每组前后正文各一次、必要的两个小时 diff 各解码一次。

复现命令（根目录改为实际本地路径；各区间为严格 dense 模式）：

```bash
python test_order_book_replay.py --compare \
  --before-state /mnt/e/periodic_abci_states/20260425/972200000.rmp.zip \
  --before-block 972200000 --before-time 2026-04-25T11:41:53.441888458 \
  --after-state /mnt/e/periodic_abci_states/20260425/972250000.rmp.zip \
  --target-block 972250000 --coins MELANIA TRUMP BTC \
  --diffs /mnt/e/node_raw_book_diffs_by_block/20260425/11.zip /mnt/e/node_raw_book_diffs_by_block/20260425/12.zip
python test_order_book_replay.py --compare \
  --before-state /mnt/e/periodic_abci_states/20260506/985700000.rmp.zip \
  --before-block 985700000 --before-time 2026-05-06T04:10:47.897434197 \
  --after-state /mnt/e/periodic_abci_states/20260506/985750000.rmp.zip \
  --target-block 985750000 --coins WIF BTC \
  --diffs /mnt/e/node_raw_book_diffs_by_block/20260506/4.zip /mnt/e/node_raw_book_diffs_by_block/20260506/5.zip
python test_order_book_replay.py --compare \
  --before-state /mnt/e/periodic_abci_states/20260518/1000750000.rmp.zip \
  --before-block 1000750000 --before-time 2026-05-18T03:05:44.921943120 \
  --after-state /mnt/e/periodic_abci_states/20260518/1000800000.rmp.zip \
  --target-block 1000800000 --coins TST BTC \
  --diffs /mnt/e/node_raw_book_diffs_by_block/20260518/3.zip /mnt/e/node_raw_book_diffs_by_block/20260518/4.zip
```

## 用户 CSV 的补齐方式

输入为用户的 `20261026_1 2.zip`：1,445 笔接管、56 个 episode、52 个原成功订单簿、4 个失败。
在新六文件运行中通过原 run_hlp.py 导入完整 CSV1；复核并用原 AppendCSV 提交 API
保留 52 个 book_ready 行，记录来源 ZIP/CSV2 SHA256 和原 run_id，然后由原生产入口
只回放剩余 4 项，再由原 CSV3 阶段仅根据 CSV1/CSV2 重算全部 episode。

这是本任务的一次明确迁移，未新增默认 CSV2 自动导入入口。原 52 行除新 run_id 外须完全相同；
原 CSV1 除新 run_id 外须完全相同；原 52 个 CSV3 除 run_id/csv2_row_hash 外须完全相同。
复用行不宣称本轮重新读取了它们的原始快照或 diff。原输入 ZIP 不修改。

补齐结果：CSV1 **1,445 行 / 56 episode**；CSV2 **56 book_ready / 0 skipped**；
CSV3 **47 calculated_full / 9 calculated_partial / 0 skipped**，终态 completed。
原 52 个结果按上述规则完全保留。四个新增结果如下（LA 原文件保留完整十进制精度）：

| Episode | 币种 | 状态 | 已成交量 | 未成交量 | LA（展示到 6 位） |
| --- | --- | --- | --- | --- | --- |
| EP000053 | MELANIA | calculated_full | 8893.2 | 0 | 51.852271 |
| EP000054 | TRUMP | calculated_full | 338099.8 | 0 | -17702.830390 |
| EP000055 | WIF | calculated_full | 210604 | 0 | -1939.601608 |
| EP000056 | TST | calculated_partial | 33212708 | 26896776 | -319739.355377 |

全部 56 行独立 Decimal 复算、CSV1/2/3 关联及订单簿哈希通过；模拟轨迹共 **14,312 笔**。
将正式 CSV1/CSV2 与进度复制到无 cache.sqlite 的隔离目录，清空副本 CSV3 的提交位置，
阻断原始数据读取、缓存构造、快照发现及外部 CSV1 身份读取，再运行原 CSV3 入口：
56 行 CSV3 **字节完全一致**，CSV2 字节未变，且未创建 cache.sqlite。
见 [独立与隔离核查日志](arena_final_56_audit.log)。

本轮总 LA 为 -45608487.666120447759970973283316495194096065216509050726741793011，
只含已成交部分，不把未成交量估值为零损益。运行目录六文件已正常关闭并保留；
供用户查看的 ZIP 仅打包三个 CSV 与 run.log，不属于可独立续跑的数据库备份。

本地验证后至发布期间只更新文档/CI；最终产出进度库的 engine_hashes 与冻结源码逐一相同。
本轮范围是四个目标 episode 和上述七组真实比较，不宣称重新扫描了全量 fills、
重放了复用的 52 个订单簿，或验证整个 ABCI 账户/仓位状态。
