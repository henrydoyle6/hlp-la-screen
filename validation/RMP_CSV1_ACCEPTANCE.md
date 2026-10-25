# v0.3.0 快照兼容与 CSV1 导入验收

验收日期：2026-10-26（北京时间）。运行环境为本机 WSL Ubuntu、Python 3.12、
msgpack 1.2.3 / orjson 3.13.0。运行代码在最终兼容性修正后冻结；随后只修改文档与 CI。

## 自动测试与保护检查

`python -m unittest discover -s . -p 'test_*.py' -v`：**200 项通过，108.227 秒**。
见 [完整测试日志](rmp_csv1_tests.log)。新增 11 项布局测试和 10 项 CSV1 导入测试；
包括无 name 的无关现货、cls 的实际 pdi、ctx 在深处、元数据顺序变化、元数据失败局部化、
错误答案比较失败，以及导入读取/校验/输出中断、来源变化拒绝、损坏哈希、重复与日期错误。

`python tools/check_release.py --unreleased`：通过。31 个受保护源码片段保持原样，
未修改原逐单 diff、单位/精度换算、剩余量、LA、精确比较或保护基线。
CI 在 Ubuntu Python 3.10 / 3.12 重跑完整测试，另在 Acceptance 显式运行新增两组测试。
CI 使用合成输入，不声称能访问本地真实 RMP。

## 真实快照：原比较入口

输入来自用户的 `E:\20261025_hyperliquid_check`。直接运行原入口
`test_order_book_replay.py --compare`，通过同一生产提取器读取前快照，
用原始逐单 diff 完整回放目标块，**全部回放结束后才读取后快照作为答案**。
后快照不用于定位、补洞或修正。双侧所有活跃订单的 oid、side、price、remaining_qty
精确比较，不使用容差或同价聚合。

| 布局 | 前块 → 后块 | 币种 | 回放/答案订单数 | 缺失 / 额外 / 字段变化 |
| --- | --- | --- | --- | --- |
| exchange.context | 890950000 → 891000000 | BTC | 36863 / 36863 | 0 / 0 / 0 |
| exchange.context | 同上 | SOL | 8572 / 8572 | 0 / 0 / 0 |
| exchange.context | 同上 | UNI | 1465 / 1465 | 0 / 0 / 0 |
| exchange.locus | 928150000 → 928200000 | AZTEC | 247 / 247 | 0 / 0 / 0 |
| exchange.locus | 同上 | BTC | 43819 / 43819 | 0 / 0 / 0 |
| exchange.locus | 同上 | SOL | 8593 / 8593 | 0 / 0 / 0 |
| exchange.locus | 同上 | UNI | 953 / 953 | 0 / 0 / 0 |

完整机器结果：[旧布局](rmp_legacy_exact.log)、[locus 布局](rmp_locus_exact.log)。
两份前后快照各正文提取一次；旧布局 diff 解码两份、缓存命中四次；
locus diff 解码三份、缓存命中九次。

Ubuntu 命令（`ROOT` 换成实际挂载目录）：

```bash
ROOT=/mnt/e/20261025_hyperliquid_check
python test_order_book_replay.py --compare \
  --before-state "$ROOT/periodic_abci_states/20260211/890950000.rmp.zip" \
  --before-block 890950000 --before-time 1770817373961 \
  --after-state "$ROOT/periodic_abci_states/20260211/891000000.rmp.zip" \
  --target-block 891000000 --coins UNI BTC SOL \
  --diffs "$ROOT/node_raw_book_diffs_by_block/20260211/13.zip" \
          "$ROOT/node_raw_book_diffs_by_block/20260211/14.zip"
python test_order_book_replay.py --compare \
  --before-state "$ROOT/periodic_abci_states/20260319/928150000.rmp.zip" \
  --before-block 928150000 --before-time 1773881761966 \
  --after-state "$ROOT/periodic_abci_states/20260319/928200000.rmp.zip" \
  --target-block 928200000 --coins BTC SOL UNI AZTEC \
  --diffs "$ROOT/node_raw_book_diffs_by_block/20260319/0.zip" \
          "$ROOT/node_raw_book_diffs_by_block/20260319/1.zip" \
          "$ROOT/node_raw_book_diffs_by_block/20260319/2.zip"
```

两种问题的原因与处理：

- 旧转换器会提前转换无关现货元数据，部分真实 spot_infos 没有 name，导致所需永续也被 `'name'` 阻断。
  现在先定位所需市场再换算，所需名称仍无法解析则明确失败，不伪造名称。
- 新快照把 context/perp/spot 元数据移至 locus.ctx/cls/scl，ctx 在约 406 MB 解压位置。
  现在结构化分派、流式跳过大对象后读取真实 ctx，保留旧布局的受保护头读取器。
  首次头定位比旧布局昂贵，日志记录实际字节与偏移，结果按源身份缓存。

## 真实 CSV1 → CSV2 → CSV3

从用户原反馈 CSV1 中取实际 UNI episode EP000047 的完整一行，作为文件导入验收夹具；
这不是重新生成的全量修复 CSV1，也不代表 210 天全量重跑。
准备日期沿用源 CSV1 的 2025-10-21 至 2026-05-18。

调用生产 `run_hlp.run()`，CSV1_INPUT 指向该文件，fills 目录不存在，
并让 ingest/group 调用直接抛错，证明未扫描 fills、未重新分组、未依赖源 progress.sqlite。
读取用户真实快照和 diff，结果为：

| 指标 | 结果 |
| --- | --- |
| CSV1 | 1 fill / 1 episode，源文件 SHA256 不变；导入只改 run_id |
| CSV2 | UNI、snapshot 890950000、target 890962993、book_ready |
| CSV3 | calculated_full、filled_qty 1124.7、unfilled_qty 0、LA -28.91595 |
| 完成后 all 续跑 | 阻断来源、原始缓存与 ingest/group；三个 CSV 字节不变 |
| CSV3 独立重算 | 无 cache.sqlite 的隔离副本；阻断全部原始/导入来源；CSV3 字节一致 |
| 持久文件 | 正常关闭后六个，结果副本保留在本机 outputs，未提交原始数据或数据库 |

见 [生产调用和隔离断言日志](rmp_csv1_real_import.log)。结果进度库的 engine_hashes
与此次冻结源码逐一相同。导入摘要 raw_records/recognized_fills 为 null，input_mode 为
imported_csv1，避免把未读取的原 fills 统计误报成 0。

## 验收范围与部署

本轮验证两种真实布局、上述七组币种和一个真实 UNI 导入；不宣称覆盖全部币种、
所有未来快照布局、整个 ABCI 账户状态或完整 210 天筛选。用户已确认 fills 修复，
全量正确 CSV1 将另行提供，同学须使用 README 的 CSV1_INPUT 路径开启新轮。
导入不能识别整组 episode 从源 CSV1 消失的情况，提供者负责完整性。

保留正式区间两端包含、30 分钟聚合、缺 fills 强制分段、合法最近前快照、
缺 diff 跳过和模拟不改历史簿等既定规则。v0.2.0 CSV1 可导入，旧数据库不混续。
PR、精确合并提交 CI、标签与公开 Release 依次成功后，Ubuntu 固定安装 v0.3.0。
