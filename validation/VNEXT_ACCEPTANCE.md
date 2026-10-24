# v0.2.0 本轮验收

本页只记录本轮重新执行的验证。旧 `CSV_PIPELINE_ACCEPTANCE.txt` 等文件是历史证据；
真实数据与 CI 的合成数据测试分开记录。验收日期为 2026-10-25（北京时间，日志使用 UTC）。

## 版本与执行环境

- 生产运行时代码：`c3610fbfe28149515f90be38c87f34d9098d2b54`；其后仅补充测试、说明和验收证据。
- 在公开仓库的独立 clone 中创建新 venv，单独从 PyPI 安装固定依赖，不使用原开发环境的 PYTHONPATH。
- 本机 Ubuntu 为 WSL Ubuntu / Python 3.12.3 / SQLite 3.45.1，msgpack 1.2.3、orjson 3.13.0。
  没有安装 `hl-node`。这模拟了独立 Ubuntu 用户安装，不能据此宣称所有裸机与磁盘组合都已实测。
- CI 另外覆盖 Ubuntu 24.04 的 Python 3.10 和 3.12。安装、顶部配置、正式运行命令见仓库 README。
- 31 个原始数学、逐单回放、剩余量、精度和精确比较片段仍与 `e698b1c` 相同；
  接口迁移的保护基线说明见 `docs/PROTECTION_MIGRATION.md`。

## 合成与故障注入

完整命令：`python -m unittest discover -s . -p 'test_*.py' -v`。
完整测试 **179 项全部通过**（41.443 秒，`vnext_ubuntu_tests.log`）；其中原有订单簿/筛选测试 60 项仍全部通过。
独立 clone 在补充最后一个链式分组测试前的完整 178 项也全部通过（41.281 秒）。
另执行 `python tools/check_release.py --unreleased`，31 个保护片段检查通过。
`python test_order_book_replay.py --demo --demo-output <独立目录>` 的五个案例均运行结束，
包括手算最后一单部分成交、LA=6 的第五案例，输出经新 pipeline 生成。

规格第 15 节共有 **36 项**（A01–A08、B01–B11、C01–C08、D01–D09）。
以下映射用于定位测试，不能代替测试实际成功的记录：

| 项 | 对应测试位置或明确断言 |
| --- | --- |
| A01 | `test_order_book_replay.ScreeningIntegrationTests`：币向独立、同块三笔；手算 A |
| A02 | 原 30 分钟 / +1ms 测试；`test_vnext_cases` 的 90 分钟连续链测试 |
| A03 | `test_universal`：首尾日期、闰年跨年、正式右端等号与下一日排除 |
| A04 | `test_vnext_cases` 手算 E；原缓冲纳入测试 |
| A05 | 原左右外边界 partial 测试；手算 B、外界恰好 30 分钟 |
| A06 | `test_universal` 缺小时；手算 B 的五分钟 gap |
| A07 | `test_universal` 坏首文件、歧义小时、来源变化、源排序异常；`test_csv_pipeline` 局部错误分段 |
| A08 | `test_universal.test_no_takeovers_with_errors_still_has_issues_not_fake_episodes` |
| B01 | `test_ondemand` 原生/ZIP 适配与前后比较；本轮真实 ZIP RMP 比较 |
| B02 | 原三项快照选择测试；`test_universal` 元数据、未来快照测试 |
| B03 | 原 `test_replay_includes_all_changes_in_target_block_not_next_block` 与生产集成测试 |
| B04 | 原 OrderBook 测试；恢复后 0 量待删与双侧缓存测试 |
| B05 | 原缺块、晚坏行局部跳过；`test_local_projection` 成功前缀测试 |
| B06 | `test_vnext_cases` 回放中断；`test_ondemand` 成功缓存发布隔离；原独立前快照测试 |
| B07 | `test_universal` 未知损坏 backup、已选坏正文、重复已选快照 |
| B08 | `test_universal` 准备日期外禁读；手算 F；原非必要未来文件不预检 |
| B09 | `test_csv_pipeline.test_csv2_contains_only_close_side_and_same_price_orders` |
| B10 | `test_universal` 多币种合并扫描；`test_ondemand` 批量比较；`test_local_projection` 热缓存禁重开；本轮真实计数 |
| B11 | `test_vnext_cases` 长字段半行恢复；`test_csv_pipeline` 空侧与缺数据区分、CSV2 偏移索引压力测试 |
| C01 | `test_vnext_cases` 手算 A/C；原最后一单部分成交、空簿测试 |
| C02 | 手算 C 的多空正负和全 episode 均价；原空头测试 |
| C03 | `test_csv_pipeline.test_simulation_does_not_mutate_csv2_or_other_episodes` |
| C04 | `test_csv_pipeline` 阶段前提和严格先后 spy |
| C05 | `test_csv_pipeline` 删除 fills 后 CSV2 正常；不读取 ingest/group staging |
| C06 | `test_csv_pipeline` 屏蔽原始目录与缓存；本轮真实 CSV3-only 隔离复算 |
| C07 | `test_csv_pipeline` 缺行、重复、坏 JSON、错误关联局部跳过；未知表头损坏终止 |
| C08 | 无接管有诊断测试、严格阶段不反写测试、空簿与 skipped 指标区别 |
| D01 | `test_csv_pipeline` ingest/group/emit 分别注入恢复 |
| D02 | `test_csv_pipeline` 回放/缓存/CSV fsync/进度提交各断点；`test_vnext_cases` 回放中断 |
| D03 | CSV3 长 JSON 半行中断测试；`test_vnext_cases` 长字段恢复 |
| D04 | `test_csv_pipeline` True 不重试 / False 新轮；手算 G 补数据 |
| D05 | `test_csv_pipeline` 代码变化拒绝混续、阶段/日志选项允许变化 |
| D06 | `test_ondemand` 缓存条目局部损坏；`test_vnext_cases` 缓存库重建；进度损坏明确拒绝 |
| D07 | `test_csv_pipeline` 锁测试；`test_vnext_cases` 两个真实进程竞争写入测试 |
| D08 | 生产测试夹具逐次断言六文件；阶段切换测试；本轮真实目录审计 |
| D09 | 原 CLI 退出；demo 调用新 pipeline；唯一正式入口 `run_hlp.py` |

D09 另用独立临时工作目录实际启动两个旧式 `--config missing.json` 命令：
`hlp_la_screen.py` 和 `run_hlp.py` 均返回 2 并提示正确入口/顶部配置；没有产生筛选文件。

额外压力测试把约 24 MB 的 CSV2 建成仅含偏移的 SQLite 索引，读取峰值约 2.3 MB Python 分配；
此数字衡量索引建立过程，不代表整个回放的 RSS 上限。模拟时按 episode 读取订单 JSON。
保留源事件位置的错误定位、未知程序异常不伪装为输入问题，也包含在回归中。

## 真实前后快照比较

使用本轮最终运行时，调用原 `test_order_book_replay.py --compare` 入口的多币种适配：

```text
before-state: 20251112/793650000.rmp.zip
before-block: 793650000
before-time: 1762906762143 (ms)
target-block: 793700000
after-state: 20251112/793700000.rmp.zip
coins: SOL POPCAT
diffs: 准备目录 20251112 的小时 ZIP（按需停止）
```

复现时按实际 Ubuntu 挂载位置填写两个根目录：

```bash
SNAP=/data/hyperliquid/periodic_abci_states/20251112
DIFF=/data/hyperliquid/node_raw_book_diffs_by_block/20251112
python test_order_book_replay.py --compare \
  --before-state "$SNAP/793650000.rmp.zip" --before-block 793650000 \
  --before-time '2025-11-12T00:19:22.143Z' \
  --after-state "$SNAP/793700000.rmp.zip" --target-block 793700000 \
  --coins SOL POPCAT --diffs "$DIFF"/{0..23}.zip
```

所有币种都完成前快照 + diff 回放后才读取后快照答案。精确比较双侧活跃订单的
OID、方向、价格、剩余量，无容差、无偏移试探、无答案回灌：

| 币种 | 回放 / 答案活跃订单数 | 缺失 | 额外 | 变动 | 结果 |
| --- | --- | --- | --- | --- | --- |
| POPCAT | 512 / 512 | 0 | 0 | 0 | PASS |
| SOL | 8836 / 8836 | 0 | 0 | 0 | PASS |

本轮比较的 RMP 正文扫描共 2 次（前后各 1），压缩 diff 解码 2 次、缓存命中 2 次。
覆盖本轮筛选实际需要的两个币种；没有宣称全市场或完整 ABCI 账户、持仓、oracle 和队列时间一致。
原始 `.rmp` 路径另由合成测试覆盖。精确比较日志见 `vnext_exact_abci.log`。

## 两日正式筛选与真实阶段隔离

最终一轮是公开仓库独立 clone 中的实际 `python run_hlp.py` 命令，顶部配置填写本地已有数据根目录，
没有调用另一份筛选实现。以空应用缓存开始，UTC 18:40:37 启动、19:18:58 完成（约 38 分 21 秒）；
耗时仅代表本机 WSL、挂载盘原始 ZIP 与 ext4 SQLite 输出组合。

```text
run_id: fb6edbd0
prepared dates: 2025-11-12 .. 2025-11-13
raw interval: [2025-11-12T00:00:00Z, 2025-11-14T00:00:00Z)
formal interval: [2025-11-12T01:00:00Z, 2025-11-13T23:00:00Z] (inclusive)
raw block records: 2,128,676
recognized / selected takeover rows: 41 / 41
episodes: 3; partial episodes: 0
CSV1: 41 grouped; CSV2: 3 book_ready
CSV3: 2 calculated_full + 1 calculated_partial
input issue rows: 0; skipped: 0; completion: completed
```

三个 episode 的真实结果：

| episode | 币向 | 目标 block | 接管总量 | 模拟成交量 | LA |
| --- | --- | --- | --- | --- | --- |
| EP000001 | POPCAT short | 794315275 | 67074 | 67074 | -77.316626 |
| EP000002 | POPCAT long | 794341899 | 152390290 | 21321289 | -1072505.16841289092501234822769875954695013704613331991165579 |
| EP000003 | SOL long | 794336900 | 0.22 | 0.22 | 1.001814 |

逐项审计与上一已验收版本的共同业务字段：41 行接管身份、分组、数量、价格和均价一致；
3 个 CSV2 的完整平仓侧订单数组（OID/顺序/价格/剩余量）一致；CSV3 原有全部成交字段、VWAP、
LA 和每条轨迹字段一致。只对规格要求的状态/schema/来源身份迁移作明确映射；没有容差或价格数量舍入。
新金额列另以 Decimal 复算检查。此项是共同字段的回归，不代替上节独立后快照答案比较。
真实事件没有恰好落在正式端点，端点等号由合成测试专门验收。

最终轮真实日志计数（不是所有中间试跑的累计次数）：

| 操作 | 次数 |
| --- | --- |
| fills 分片完整读取 | 48，全部唯一、均在准备日期内 |
| RMP 头探测 | 2 |
| RMP 正文扫描 | 1，合并提取 SOL + POPCAT |
| 压缩 diff 解码 | 16，全部唯一，为 20251112 的 0–15 时分片 |
| diff 缓存命中 | 18 |
| 前向回放缓存命中 | 1 |

三个目标都使用 793700000 前快照；按目标 block 递增处理。日志明确区分头探测、正文扫描和解码。
没有额外读取准备日期之外的 diff，也没有用未来快照修复缺口。
关闭程序与数据库后保存的完整目录只有六文件，`run.log` 为 446450 字节，cache.sqlite 约 3.95 GB。
清单与已完成进度、运行时哈希逐项核对，均成功。

在**单独测试副本**中进一步执行真正的 CSV3 重算：

1. 复制 CSV1/2/3、run.log、progress.sqlite，故意不复制 cache.sqlite。
2. 只在副本重置 CSV3 游标/提交记录，并删除 ingest/assignment/groups staging。
3. 拦截原始根目录文件打开、目录发现、RMP 读取、diff 读取、Cache 初始化及 cache.sqlite 打开。
4. 调用相同 `run_hlp.run()` 生产入口执行 `RUN_STAGE='csv3', RESUME=True`。
5. 三个 episode 全部复算，原始数据/缓存访问尝试 **0**；CSV3 与完整轮逐字节一致，CSV1/2 未变。
6. 再在同样屏蔽条件下执行已完成的 `all + RESUME=True`，零原始读取、零重复行、结果不变。

CSV3 SHA256：`9f076c862f0505f75db876e5b113e5de6d18341e88b66654bbfa8d2faeac5a0f`。
这不修改或删除原始完整六文件结果。证据见 `vnext_real_audit.log`、`vnext_csv3_only_real.log`。

完整原始产物保留在工作区之外的 `outputs/vnext_native_student_20261025/` 中，运行目录末尾为
`20261024T184037613461Z_fb6edbd0`。原始市场数据、数据库和业务 CSV 不进入 Git 仓库。

## 本轮发现和修正

验收期间发现的资源问题均在最终冻结运行时之前处理：CSV3 改为持久偏移索引逐 episode 取行；
diff 缓存只校验当前回放区间，前向缓存恢复时间游标；源事件错误保留原始位置。
没有改数学/剩余量/逐单 diff/精确比较来匹配答案。

最初把 SQLite 输出放在 WSL 挂载盘的试跑因随机 I/O 很慢而主动中断，随后在 Ubuntu ext4 重跑。
另一次中间版本试跑为修正上述内存与查询问题主动中断；两者都不计为完成验收，也没有删除原始数据。
正式长期输出应使用 Ubuntu 持久目录，本轮临时 ext4 输出在关闭数据库后复制保留。

旧 `.state` 断点不强行兼容。升级只复制顶部配置值，`RESUME=False` 新建一轮；同版本同输入用
`RESUME=True` 继续已提交进度。已提交的 skipped/issue 不会自动重试，补数据后另开新轮。
正常每轮只有六个长期文件；SQLite 临时 journal 是故障恢复数据，不能为凑文件数删除热 journal。
