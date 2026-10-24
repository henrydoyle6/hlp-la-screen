# HLP 接管筛选：完整框架与 Codex 修改提示词

> 可将本文件完整交给 Codex。不需要提供此前聊天、审查报告或旧版筛选细节。
> 本文件是待实施的需求与验收规格，不是代码已修改、测试已通过或真实数据已验收的声明。
> 编制时核对的仓库 main：`e698b1ce70be366e82e83f1fc6d2b9bbaddca748`。执行修改时应先读取实际最新 main，记录起始提交，不回退或覆盖用户之后的有效改动。

## 0. 任务、范围与优先级

在现有仓库基础上修改，不另起一个不兼容的筛选项目：

```text
https://github.com/henrydoyle6/hlp-la-screen
```

项目用于事后研究：识别 Hyperliquid node fills 中的 HLP 接管记录，按币种、持仓方向和时间聚合 episode；重建每个 episode 最后一笔接管所在区块的块末订单簿；按该订单簿模拟平掉这个 episode 累计接管的数量，计算模拟盈亏 LA。不下真实订单，不接真实交易账户，不把结果称为 HLP 真实持仓或真实已实现收益。

本文件取代与其冲突的旧 FRAMEWORK、旧提示词、旧输出字段、旧 CLI 和旧断点布局。保留已正确的数学、逐单 diff 语义和精确比较算法；修改编排、输入错误隔离、RMP 入口、输出结构、状态保存及缓存。不得以“旧代码未改”代替需求验收，也不得为通过测试放宽精确比较。

### 0.1 本轮明确消除的歧义

1. 用户同时写过“正式结束不包含”和后面的“仍是两端包含”。本版按最后一项确定：**正式筛选区间两端包含**。例：一天的正式区间是 `[01:00:00.000, 23:00:00.000] UTC`，23:00:00.000 本身算正式范围。不是 `<23:00`，也不是截至 22:59:59.999。数据日期仍包含首尾两整天，机器表示采用下一日零点为不包含边界。
2. 用户本轮限定 diff 使用已准备的 START_DATE—END_DATE 日期范围中的文件。**不采用“发现更早文件就读到 START_DATE 之前”的旧审查建议**。允许使用这段已准备日期内的全部必要 diff，包括正式范围之外的一小时缓冲；不额外索取或读取范围外小时。若所选前快照到目标 B 的必要 diff 无法由允许的文件覆盖，跳过依赖它的 episode。
3. “只保留六个文件”指每轮运行的六个长期保留文件。数据库事务可能短暂产生自己的恢复 journal，正常关闭后不应遗留额外文件；崩溃后的恢复 journal 必须先由数据库恢复，不得为凑文件数删除。不得关闭安全事务机制来假装满足文件数。
4. 为使“没有任何有效接管时的输入异常”也能进入 CSV，允许明确标识的 `record_type=issue` 诊断行。诊断行不是接管，不是 episode，不计入数量、均价、episode 数或 LA。正常业务行仍满足 CSV1 每笔接管一行、CSV2/CSV3 每个 episode 一行。

## 1. 唯一正式入口与配置

唯一筛选启动入口：

```bash
python run_hlp.py
```

保留 `run_hlp.py` 顶部集中配置，至少包括：

```python
START_DATE = '2025-11-12'     # 已准备数据的首日，包含当天，UTC
END_DATE = '2025-11-13'       # 已准备数据的末日，包含当天，UTC
FILLS_ROOT = '/data/hyperliquid/fills'
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'
HLP_ACCOUNTS = (...)          # 继承仓库现有配置，可由用户修改，不联网更新
RUN_STAGE = 'all'             # all / csv1 / csv2 / csv3
RESUME = True
CONSOLE_LOG = True
```

继承而非重新认定当前仓库 HLP_ACCOUNTS 的四个默认值：

```text
0x2e3d94f0562703b25c83308a05046ddaf9a8dd14
0xb0a55f13d22f66e6d495ac98113841b2326e9540
0x5e177e5e39c0f4e421f5865a6d8beed8d921cb70
0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308
```

这只是既有研究配置，不声称本次已独立验证其覆盖所有历史 HLP 接管账户。不得增加币种白名单、最低数量、最低金额、LA 正负或盈利性过滤。

保留合理的日志间隔、缓存容量、提交批量等性能参数；它们不得改变筛选语义。`RUN_STAGE`、日志开关不应改变同一运行的业务身份。移除要求用户维护独立 CACHE_ROOT 的必要性：本方案每轮的唯一缓存文件放在该轮输出目录内。

删除旧的 `hlp_la_screen.py --config ...` 筛选 CLI 和旧的交替生成 CSV2/CSV3 的 run_screen 编排，不留下第二套可误用的正式流程。`hlp_la_screen.py` 可以继续作为共享引擎模块。测试、demo 和独立比较工具不算第二个筛选入口，但必须复用同一套引擎、读取器和新 pipeline，不能另抄算法。

## 2. 日期、数据范围、正式范围与缓冲

统一 UTC，用户只输入 YYYY-MM-DD。设：

```text
D0 = START_DATE 的 00:00:00 UTC
E  = END_DATE 次日的 00:00:00 UTC
F0 = D0 + 1 hour
F1 = E  - 1 hour

接管数据允许范围：D0 <= event_time < E
正式入选条件：    F0 <= event_time <= F1
左缓冲：          D0 <= event_time < F0
右缓冲：          F1 < event_time < E
```

代码统一使用不含歧义的 `data_start`、`data_end_exclusive`、`formal_start_inclusive`、`formal_end_inclusive`，不要混用旧的 `data_end - 1ms` 再减一小时来实现正式右端。

例如 START_DATE=2025-11-12，END_DATE=2025-11-13：

```text
原始接管范围：[2025-11-12 00:00:00, 2025-11-14 00:00:00)
正式筛选范围：[2025-11-12 01:00:00, 2025-11-13 23:00:00]
```

两个日期相同表示一整天。23:00:00.000 是正式接管；23:00:00.001 是缓冲接管。缓冲接管仍可能随含正式接管的 episode 一起输出。END_DATE 次日 00:00:00 不读入接管。

fills 不读取或索取范围外的额外小时。diff 从允许日期的文件中按实际 block 依赖读取，不以正式区间裁剪，也不能读取范围外日期来“救回”结果。某个允许的小时文件内部链上时间与文件分区时间有细小差异时，路由以真实 block 信息为准；不能仅因内容时间落在小时边界附近而臆造缺口。fills 的 event_time 仍受 D0/E 过滤。

前置 ABCI snapshot 本身允许早于 D0，但不得因此自动扩展 diff 文件日期范围。必须证明从该 snapshot 的块末 S 到目标 B 的 `(S,B]` 在实际读取的允许文件中完整；无法覆盖则局部跳过，不能把起点强行改成 D0 后的某块并继续计算。

## 3. 原始接管识别与 episode 规则

### 3.1 哪些原始 fills 是接管

node fills 采用每行完整 block 的 JSONL；events 中每笔是 `[owner_address, fill_object]`。

只有同时满足下列条件才是接管业务记录：外层 owner 属于 HLP_ACCOUNTS，`liquidation.method == 'backstop'`，且 owner 不等于 `liquidatedUser`。不把被接管用户那一侧重复计入。正常非目标账户或非 backstop 记录直接略过，不是数据异常。

接管方向以 HLP 接管后的方向解释；原生接收方 `side='B'` 映射 long，`side='A'` 映射 short。保留现有已验证适配，不用显示文案 `dir` 代替此识别。price、qty 为正数；来源中的 px、sz 使用 Decimal，不经二进制 float。

每笔可识别的接管保留原时间、block number、价格、数量和来源位置；同 block 内多笔接管逐笔保留。恢复幂等只防止重复处理同一个来源位置，不因为两笔的币种、时间、数量和价格相同就合并或去重。

### 3.2 聚合

在每个连续可用 fills 段内，按 `(coin, direction)` 各自维护独立序列。先后顺序使用有效事件时间、block 和原始块内位置；保持已经明确的原始顺序，不能用排序掩盖无法解释的乱序。

```text
同一币种 + 同一方向 + 同一连续 fills 段：
相邻两笔时间差 <= 30 分钟：同一个 episode
相邻两笔时间差 >  30 分钟：新 episode
```

其他币种或相反方向插入不打断。episode 总时长没有 30 分钟上限。不存在跨币种、跨方向、跨 fills 缺口合并。

一个 episode 至少有一笔接管满足 `F0 <= t <= F1` 才入选。入选后保留该 episode 在 D0—E 中读到的全部接管，包括缓冲区部分；均价、数量和 Tn 都使用这些保留记录。仅有缓冲接管的 episode 不进入三个 CSV 的 episode 业务结果。

`Tn` 是该 episode 最后一笔保留接管的时间，`B` 是它所在的 block。若同 block 多笔有相同时间，用原始块内位置稳定确定最后一笔，不能丢弃其余记录。

### 3.3 fills 缺失：只分段，不让两侧失效

运行中遇到缺文件、明确缺失覆盖段、无法继续解码的输入，或无法识别的目标接管记录时，在实际位置建立 fills 分段边界。对能定位的局部记录只记对应异常；对无法确定币种/方向的缺失段，结束所有仍开放的组。

关闭缺口前的 episode，其最后一笔已知接管就是 Tn。恢复数据后从新的连续段重新开始。无论两侧相隔 5 分钟还是 65 分钟，都不跨缺口合并。两侧已知 episode 正常保留并计算，不依据它们离缺口多近而跳过，不推测缺失成交，不伪造未知事件或数量。

缺口记录到 CSV1 的 issue 行和 run.log；相邻 episode 的 split_before/split_after 记录对应原因或 issue_id。缺口标记不是 skipped，也不会单独把 partial_episode 改成 true。

空文件不能自动等同缺失：若实际读取是合法空输入，按该段没有可识别接管处理。不能声称本程序能识别所有“被静默删掉但未留下标记的合法行”；不新增全量输入预检来追求这一点。

### 3.4 外侧边界尚不完整

无法确认 episode 在 D0/E 外是否继续时，使用已经读到的部分继续计算，标记 `partial_episode=true` 和具体 `partial_reason`，不跳过、不向范围外补读。

判定使用已经存在的外侧边界逻辑，并明确测试等号：没有 fills 缺口将它与外侧隔开时，首笔距离 D0 小于 30 分钟可能接上更早记录；末笔到 E 的距离小于等于 30 分钟可能接上 E 或之后的记录。只有这类“数据外侧未闭合”才设置本标志；fills 缺口是人为确定的分段边界，不做跨缺口完整性推测。

episode ID 对同一轮相同输入和续跑必须稳定。可继续使用 EP000001 序列：完成分组后按首笔时间、首块、币种、方向和稳定来源位置排序分配，持久化后不因续跑改变。跨新运行用 run_id 区分。

## 4. 三阶段唯一数据流

```text
准备日期内的 fills
    -> CSV1：接管、episode、fills 异常；完成并提交
    -> CSV2：只从 CSV1 取得 episode；前置 RMP + diff 还原；完成并提交
    -> CSV3：只读 CSV1 + CSV2，进行模拟成交与 LA
```

必须完整完成 CSV1 才开始 CSV2；完整完成 CSV2 才开始 CSV3。不是“每个 episode 写 CSV2 后立即计算其 CSV3”。

| 阶段 | 业务输入 | 禁止的业务读取 |
|---|---|---|
| CSV1 | fills；本阶段进度/已处理记录 | 为预检而读取 RMP、diff |
| CSV2 | 已完成 CSV1；前快照、必要 diff；相应缓存 | 重新读取 fills，或绕过 CSV1 从旧 fills 缓存重做 episode |
| CSV3 | 已完成 CSV1、CSV2 | 读取 fills、ABCI/RMP、diff、发现原始目录，或从原始数据缓存补充计算数据 |

progress.sqlite 只用于阶段完成性、游标、事务和上游关联；CSV3 不得从其中旧的 raw/staging 表取代 CSV1 的接管信息。cache.sqlite 在 CSV3 不作为业务数据源，不应为 CSV3 打开或重建。

`RUN_STAGE=csv2/csv3` 的上游未完成时，明确提示先完成上游，不暗中重跑原始输入。无入选 episode 时，仍完成对应 CSV 表头及已发生的诊断，日志明确“无入选 episode”与异常数量，不把它说成“数据完整且全部正常”。

## 5. 不做原始输入预检；在实际处理处隔离问题

启动时允许解析日期、配置结构、代码版本、打开输出/进度库和取得单运行锁。不允许先检查所有小时是否存在、逐个读首块、解压全部文件、遍历验证所有 RMP、扫描所有 diff，成功后才允许处理。

读取一个实际需要的小时分片时可以定位路径、打开和解析该文件；为实际 episode 选快照时可以按需取得必要候选的元数据。这是处理步骤，不是全局通过门槛。最早出现坏文件或异常首块不能阻止后续不相关文件正常处理。异常记录不能推进 last_seen、覆盖边界或缓存成功游标；不得让一个错误的大时间/高度持续污染后续正常分片。缺口建立新连续段时，相应顺序检查状态也按段重置。

源文件在读取中改变，属于该源的输入异常：丢弃该单元未提交结果并记录 SOURCE_CHANGED，后续独立任务仍可处理；不能把前后两个不同版本的片段拼成成功输入。已经提交的输入发生有意变更时，应采用 RESUME=False 新轮，不暗中重算旧轮。

所有已经观察到的输入异常，除了 run.log，还必须进入相应阶段 CSV。能确定 episode_id 的错误写该 episode 的业务行；不能确定任何 episode 的错误用 issue 行，保留真实可知字段，其余留空。

诊断内容至少含 `issue_id, stage, code, message, source_file, source_member, source_line, event_index, block_number`；知道时间或 block 缺口范围就补充，不知道就留空，不能捏造范围。只保存必要的错误摘要，不往 CSV 写完整坏文件或无关账户隐私。

### 5.1 局部错误与系统故障的区别

输入缺失、坏行、坏 snapshot、相关 diff 缺口及可定位的单 episode 校验失败：局部记录并继续。

输出磁盘满、输出文件无法写入、进度库不可恢复、代码/配置与旧断点不兼容、无法定位损坏边界的已提交结果破坏等系统性故障：可以终止且写明原因。这不是把某一原始文件缺失扩散成全部 episode 失败。未知编程异常不得被一律吞成普通数据缺失并伪造成功。

不新增严格 preflight。断点恢复对自身已提交结果的必要检查不等于扫描原始数据，但应使用已有提交摘要或分块摘要，避免每提交一个 episode 就重新哈希整个 CSV。

### 5.2 候选快照的局部隔离

快照头部元数据发现延迟到 CSV2 实际需要选择快照时；发现结果放 cache.sqlite。不在 CSV1 之前批量打开 snapshot。

排序以真实 snapshot 元数据为准，文件名高度只可帮助定位候选，不可替代真实高度/时间。只让元数据成功解析的候选参与时间排序。

已知最近前置 snapshot 被选中但正文读不到/损坏：只跳过依赖该 snapshot 的 episode，不偷偷换旧快照或未来快照。

一个无法取得任何高度/时间的损坏备份文件不能让全部 episode 被阻断：独立记录 issue，不推断它是所有 episode 的依赖。存在这种不可定位候选时，可以使用已成功解析候选中的最近前置 snapshot，但必须在受该目录选择范围影响的 CSV2 行标明 `snapshot_selection_status=nearest_resolved_with_warning`，说明“最近”仅相对已解析候选，不能声称已证明未知文件不更近。正常完整候选集写 `nearest_resolved`。

同一小时或已选快照的多份编码如有无法消除的真实歧义，只作用于该小时/依赖该快照的 episode，不使其他日期、其他快照的处理终止。不为保守起见构造全局 veto。

## 6. 自写 RMP 读取与订单簿还原

### 6.1 完全移除官方 hl 旧依赖

所有正式、独立提取、demo、测试和真实前后快照比较路径统一调用新的共享 RMP 提取器（在现有 rmp_l4.py 基础上修改）。

删除运行参数、配置、函数分支、子进程调用及 README 安装说明中的 `hl_node` / `hl-node` / `compute-l4-snapshots` 旧依赖；不得留下“正式流程不用，但直接 RMP 比较还要用”的例外。通用 JSON L4 适配仍可保留供合成测试使用。

沿用已验证的原生字段适配，从本快照元数据获取市场名称、asset 和数量/价格精度。不得为某些币种手工硬编码缩放倍数，不得使用后快照的元数据帮助还原前状态，不按 S±1 试到答案匹配为止。原生剩余数量使用正确 remaining 字段，不把原始委托数量当剩余数量。

### 6.2 起点和终点

每个 episode 的快照候选必须同时满足：

```text
snapshot_time < Tn
snapshot_completed_block S < B
```

在合规候选中取时间最近者；相同时间按更高的已完成高度排序。快照表示 S 块末，回放 `(S,B]`，包含 B 的全部相关 diff。结果固定：

```text
book_state_point = block_end
```

不是 B-1、不是 Tn 块内瞬间，也不只应用 B 的部分 diff。缺 diff 不允许用 B-1、后 snapshot、插值或臆造空块替代。

对不同 episode 独立选择前快照：一个 snapshot 对较早 episode 是未来，对更晚 episode 可以是合法前置。不因较早 episode 跳过而永久封禁后续独立有效 episode。

### 6.3 回放语义与缺口

沿用新增/更新/删除的已验证语义。`update.newSz` 是新的绝对剩余量，不是增量。保留 update 到 0 后等待 remove 的内部记录；对外可成交订单只保留 qty>0。默认不把 origSz 强行当作上一步剩余量，除非现有明确数据契约要求；不得新增此类业务限制。

按完整 block 的原始事件顺序应用 diff。生产采用现有完整 by-block 数据与 dense 连续覆盖要求，空 events 块有效；不要通过改成稀疏模式来掩盖缺块。新单缺必需方向/价格而无法从真实字段确定时，局部失败，不猜。

同价独立 OID 保留。可以完整回放一个币种的双侧内部状态，以正确处理后续更新和删除，但输出到 CSV2 只取平仓所需单侧。

缺口只影响选择了该路径且确实依赖缺失部分的 episode。目标 B 已完整结束后出现的坏行不能倒过来使该 episode 失败；快照 S 之前的已定位坏段也不应使其失败。范围无法定位的坏行只能按照实际依赖的文件/解码区间影响相关任务，不能泛化为所有日期、币种和 episode。

失败的部分回放状态绝不标为可复用成功状态。已成功的较早块末可以保留；后续 episode 若仍需跨过同一缺口，应跳过；如果另有其 Tn 之前的独立快照避开缺口，可以正常处理。

### 6.4 单侧输出

```text
HLP long：模拟卖出，取 bid，价格从高到低。
HLP short：模拟买入，取 ask，价格从低到高。
```

保存整侧全部可成交订单，不限制价格距离、档数、top N、数量或 notional。未触发条件单不作为可成交深度。真实存在但远离最优价的挂单不能仅因价格远被删除。

## 7. CSV 通用编码与诊断行

三个文件均 UTF-8 标准 CSV，使用 csv 模块正确转义；价格、数量、OID、Decimal 数值以精确文本表示，避免 Excel 自动格式成为算法依赖。

JSON 字段采用紧凑 JSON，不使用 indent，不含格式化换行。标准 CSV 读取结果中，每个 episode 的 `orders_json` 是一个字段，且每笔原始订单是一项；不要分列截断或按长字符串切成多条业务记录。调高 Python CSV 字段容量，至少覆盖现有大簿；上限属于内存保护不是业务筛选条件，达到限制要明确报错/改用流式写入，禁止静默截断。

每个 CSV 的通用列：

```text
schema_version, run_id, record_type, episode_id, status,
reason_code, reason_message,
error_file, error_member, error_line, error_block, issues_json
```

`issues_json` 是诊断对象数组，至少具有第5节定义的字段。reason_code/message 表示主要失败；正常业务行可为空。非致命备注写 issues_json，不把正常业务状态误改成 skipped。

`record_type=issue` 时 episode_id 只在确知归属时填写；不知道就空。无法识别真实接管的 issue 行不填写伪造 event_id、币种、价格、数量。CSV2/3 的 issue 行同理，不构造“假的 episode”。计数必须先过滤正确 record_type。

## 8. CSV1：接管和 episode 的稳定输入

### 8.1 业务粒度与字段

`record_type=fill`：每笔识别出的、属于入选 episode 的接管成交一行。同 block 多笔保留；episode 汇总重复附在该 episode 的每条接管行上。

除通用列外，至少包含：

| 组别 | 字段 |
|---|---|
| 原始事件 | event_id, event_time, block_number, coin, direction, price, qty |
| 来源 | source_file, source_member, source_line, source_event_index |
| 时间范围 | data_start, data_end_exclusive, formal_start_inclusive, formal_end_inclusive, is_formal_event |
| 分段与 episode | segment_id, episode_start, episode_end, start_block, end_block, event_count, formal_event_count, buffer_event_count, duration_ms |
| 接管汇总 | total_qty, entry_notional, entry_vwap |
| 完整性/分段 | partial_episode, partial_reason, split_before, split_after |
| 关联 | episode_input_hash |

其中：

```text
Q = total_qty = Σ qty_i
entry_notional = Σ(price_i × qty_i)
entry_vwap = entry_notional / Q
formal_event_count + buffer_event_count = event_count
```

`episode_input_hash` 基于稳定的该 episode 原始接管和规则字段生成；不能根据 CSV2/CSV3 的后续计算结果改变。源位置字段真实保留，同一事件恢复后不多写。

### 8.2 状态

| record_type | status | 含义 |
|---|---|---|
| fill | grouped | 本笔及所属 episode 已完成分组，可作为 CSV2 输入 |
| issue | input_issue | fills 缺失、损坏或其他输入诊断，不是接管业务行 |

外侧 partial 和 fills-gap 分段用独立字段，不扩充成一大堆组合状态。CSV1 的 grouped 不表示订单簿或 LA 成功。后续阶段不反写 CSV1，不把它改成 skipped 或 partial_depth。

### 8.3 三个可恢复子阶段

CSV1 内部采用：`ingest -> group -> emit`。

- ingest：流式读取实际分片，只把识别出的接管、来源位置、局部异常及游标保存到 progress.sqlite；不把整个原始 fills 装进内存。
- group：基于进度库中已读取记录完成连续段聚合和正式范围判断，保存开放组/已结束组或分组游标。
- emit：按稳定顺序向 CSV1 写最终 grouped 业务行和诊断行，每批写后提交输出进度。

CSV1 未完成时可以只有表头和已完成的输出前缀，实时进展由 run.log 提供；不必先写 reading 草稿再复制整个 CSV 原子替换。三个子阶段都能恢复，不重新读已经提交的原始 fills。

CSV1 完成后不再从其 ingest staging 重新生成 CSV2 的 episode。CSV2 必须读取正式 CSV1。

## 9. CSV2：目标 block 块末单侧订单簿

### 9.1 业务粒度与字段

`record_type=episode`：每个 CSV1 入选 episode 恰好一行，包括无法还原的 episode。输入计划从 CSV1 按 episode_id 去重取得，核对重复汇总信息，但不得重读 fills。

至少包含：

| 组别 | 字段 |
|---|---|
| 上游与接管摘要 | episode_input_hash, coin, direction, total_qty, entry_vwap, partial_episode, partial_reason, split_before, split_after |
| 目标 | tn, target_block, book_state_point, book_side |
| 快照来源 | snapshot_file, snapshot_member, snapshot_block, snapshot_time, snapshot_source_id, snapshot_selection_status |
| 回放证据 | replay_from_block_exclusive, replay_to_block_inclusive, last_applied_block, replay_coverage_mode, diff_sources_json |
| 单侧指标 | order_count, price_level_count, total_depth, side_notional, best_price, worst_price, book_is_empty |
| 逐单深度 | orders_json, book_hash |

`diff_sources_json` 保存实际使用文件、成员及覆盖范围摘要，不装载所有 diff 事件。`order_count` 是正数量订单条数，`price_level_count` 是不同价格数；同价多单数量在 total_depth 中相加，但订单明细仍独立。total_depth 是整个平仓侧的挂单数量，不是接管量。

orders_json 的固定形式：

```json
[{"oid":"101","price":"100","qty":"2"},{"oid":"102","price":"100","qty":"3"}]
```

不可合并成一项 `100×5`。OID、price、qty 保持文本。相同价格的确定性次序仅用于复现，不声称交易所 FIFO。

**删除 CSV2 的 replay_state_json 及双侧完整簿内容。**双侧订单、内部零数量记录、恢复游标只存在 cache.sqlite；不能换个列名再次放回 CSV2。

### 9.2 状态

| record_type | status | 含义 |
|---|---|---|
| episode | book_ready | 所需单侧订单簿成功还原；包括确实为空的单侧 |
| episode | skipped | 本 episode 无法还原，订单簿指标/订单数组留空并写原因 |
| issue | input_issue | 无法关联到具体 episode 的快照/diff 异常或候选诊断 |

成功的空簿：order_count=0，total_depth=0，orders_json=`[]`，book_is_empty=true，best_price/worst_price 为空。

失败的簿：orders_json 为空，不写 `[]`；total_depth 等未知量为空，不写 0。必须区分“成功且没有订单”和“未能知道有没有订单”。

CSV2 不使用 partial_depth 或 empty_book 作为最终成交状态，不提前计算 LA。book_ready 不承诺有足够成交量。

## 10. CSV3：只基于 CSV1/CSV2 的模拟成交和 LA

### 10.1 计算规则

CSV1 是接管数量/成本的权威输入，CSV2 的相应摘要只作关联核对。CSV2 的单侧 orders_json 是唯一成交深度来源。

从最优价格开始逐档成交；同价可按稳定 OID 遍历以保留成交轨迹，最后一笔/档允许部分成交。

```text
filled_qty = 实际可成交数量
unfilled_qty = total_qty - filled_qty
close_notional = Σ(实际成交价 × 实际成交量)
exit_vwap = close_notional / filled_qty，filled_qty=0 时留空

long:  LA = close_notional - entry_vwap × filled_qty
short: LA = entry_vwap × filled_qty - close_notional
```

无论只成交多少，entry_vwap 始终来自整个已读取 episode，不挑“最有利的一批接管”来对应已成交量。LA 正为盈利、负为亏损。按数量加权，不取简单均价；不对未成交部分估值。无手续费、资金费、额外冲击函数或未批准的滑点门槛。

价格与数量原值使用 Decimal；沿用至少60位有效精度的计算。先累计成交 notional 再算 LA，不先将均价显示成两位小数再乘数量。不得使用浮点容差使真实订单差异通过。

每个 episode 使用独立静态历史簿，模拟不修改 CSV2 或缓存里的历史数量，不影响下一个 episode。

### 10.2 字段

`record_type=episode`：对每个入选 episode 保留一行，包括 skipped。

至少包含：

| 组别 | 字段 |
|---|---|
| 关联 | episode_input_hash, csv2_row_hash, book_hash |
| episode 摘要 | coin, direction, episode_start, tn, target_block, event_count, total_qty, entry_notional, entry_vwap, partial_episode, partial_reason, split_before, split_after |
| 簿摘要 | book_side, order_count, price_level_count, total_depth, best_price, worst_price |
| 成交 | filled_qty, unfilled_qty, fill_ratio, close_notional, exit_vwap, entry_cost_of_filled_qty, executed_order_count, executed_price_level_count, last_execution_price |
| 最终指标 | la, depth_insufficient, fill_trace_json |

fill_trace_json 记录实际用到的每笔订单：oid、price、available_qty、filled_qty、notional。深度多余时不把没成交订单假装写入轨迹。

### 10.3 状态

| status | 规则 |
|---|---|
| calculated_full | 成功且 filled_qty=total_qty |
| calculated_partial | 成功且 0<filled_qty<total_qty |
| empty_book | 簿成功还原但 filled_qty=0 |
| skipped | 上游订单簿失败或本 episode 的 CSV 关联/解析不能成立 |

empty_book：filled_qty=0、unfilled_qty=total_qty、close_notional=0、la=0、exit_vwap 为空、depth_insufficient=true、轨迹 `[]`。这只是零成交部分的 LA，不是全仓可无损平仓。

skipped：无法计算的数量、均价、LA 留空，不以 0 代替；已知的接管摘要可以保留。继承 CSV2 的原因及错误文件。局部 CSV2 缺行、坏 JSON、重复 episode 等能定位时仅跳过受影响 episode，不让所有正常 episode 失败；无法确定源结果结构/完成性的整体损坏另按系统故障处理。

不因为 LA 为负、为正、为0、深度不足或 partial_episode 而筛掉结果。

## 11. 六个长期保留文件

每轮输出目录最终只有：

```text
run_<dates>_<run_id>/
    csv1.csv
    csv2.csv
    csv3.csv
    run.log
    progress.sqlite
    cache.sqlite
```

每轮各自一套六个文件。RESUME=False 新建运行目录，不覆盖上一轮；本方案不额外建立全局共享缓存或指针文件。新轮默认重新处理，旧轮缓存不直接作为新轮数据。要恢复同配置最近一轮，可从有序运行目录名与 progress.sqlite 的配置身份定位，不生成 `.latest_*.json`。

删除旧运行输出中的多文件设计：不再生成 .state 下多个 JSON/JSONL、独立锁文件、user_config.json、fills_discovery.json、book_discovery.json、resolved_config.json、process_exit.json、summary.json、source 副本、source_sha256.json、散落的 RMP JSON 和逐币种 diff 文件。相关必要内容进两个数据库或 run.log。不要删除用户已有旧运行；这是新版本输出约束，不是清理旧资料的授权。

源代码、仓库 README、测试代码和 CI 配置不在“运行产物六个文件”的计数内。

### 11.1 progress.sqlite：进度与不可丢的运行状态

至少存储：run_id、配置快照、起始 commit、语义版本、源码版本标识、日期边界、HLP 配置；三阶段状态及 CSV1 子阶段；已提交来源游标；已读取 HLP 接管和 fills 缺口；分组开放状态/分组索引；稳定 episode ID；每个 CSV 已提交字节偏移、行数、阶段摘要/哈希；最后成功业务单元；异常索引与运行汇总；必要的输入身份；恢复时引用的缓存状态键。

不把全体原始 fills/diff 无差别复制到进度库。HLP 接管 staging 是为了不重复读取 fills，可在 CSV1 完成且保证恢复所需数据已独立持久化后清理；不可为了省空间破坏 CSV1 恢复。

### 11.2 cache.sqlite：可重建的唯一缓存文件

集中存储：按需发现的源身份/快照头；RMP 提取的目标币种簿；需要复用的已解压/已解析 block 或分块内容及索引；必要的逐币种 diff 投影；指定源快照与币种的成功块末恢复状态，包括双侧和零数量待删除订单。

同一源快照的待处理币种先从已完成 CSV1 任务中汇总，尽量一次完整 RMP 扫描提取完，不每个 episode 扫一次。只保留实际需要币种；若元数据在订单后面，需要暂存的记录放缓存数据库，不散落成永久临时文件。

同一压缩 diff 文件首次实际处理时流式解码并按完整块/有界块组入缓存，保留所有观察到的 block 覆盖信息，包括空 events；随后不同币种、episode 和重启可直接使用该缓存，减少重复解压。不得为填缺块人为生成 events=[]。

不要把“整个文件成功”作为已经完整验证的前缀块可用的唯一条件：尾部错误不应抹去此前目标 B 的成功状态。缓存需区分已提交有效前缀、完整源成功、已定位缺口和未提交尾部。缓存不能将失败/残缺片段伪装成完整覆盖。

RMP 正文扫描、快照元数据探测、压缩分片解码的次数分开统计；不得把探测头部说成从未打开过文件，也不得以新进程为借口完全忽略已经成功的缓存。

价格/数量仍精确存储，使用 TEXT、整数缩放或无损 blob；不存 SQLite REAL 作为计算依据。缓存键至少区分源身份、解析版本、币种、源快照和已覆盖区间。双侧回放状态不能跨币种或跨来源快照误用。

输入身份在实际读取/使用时获取；可沿用路径、size、mtime_ns、ctime_ns、成员信息与轻量边缘指纹等，不先哈希全量输入。完整哈希需要时随真实读取流累计，不为了命中缓存再完整扫一遍 RMP。RESUME=True 的前提是原始数据未变；不能声称轻量指纹能证明任意人为修改不存在。

缓存可损坏后丢弃对应条目并按合法前置快照/已读数据重建，不能因为一个缓存条目损坏跳过所有 episode。progress.sqlite 是恢复依据，缓存不是唯一不可恢复的业务事实来源；CSV1/2 完成后的 CSV3 完全不依赖缓存。

### 11.3 磁盘、内存与临时文件

SQLite 使用安全事务，优先 DELETE journal 模式与适当同步级别，不使用长期残留的 WAL/SHM 作为额外交付，不把 journal_mode=OFF 或 MEMORY 当成满足六文件的办法。正常关闭后六个长期文件；崩溃时可能有临时恢复 journal，先恢复再清理。

不把全部 RMP、全量原始历史或所有币种大簿载入 RAM。分块写缓存，按有限数量的币种簿回放。缓存可以按已完成消费者释放不再需要的数据，必要时支持明确的容量/LRU 选项；淘汰导致重读时记录原因，不能宣称任意空间上限下永不重读。

不承诺压缩 ZIP 在任意字节处断点恢复：已经成功提交的解析块不重做；中断时正在解压且尚未提交的部分允许从该压缩成员的安全起点重读，日志如实说明。不得为追求零重读破坏幂等。

## 12. 断点协议与 RESUME 的唯一语义

```text
RESUME=True：输入未变，只恢复同一运行未完成进度。
RESUME=False：补过 fills/diff、改了原始输入或希望重算，创建新轮。
```

已经提交的 skipped 和 input_issue 也是该轮结果。True 不会自动补算旧 skipped、不重新拼接已提交的 fills 分段；补数据需要 False。新轮保留旧结果，并清晰记录新 run_id。

阶段切换保持 True。代码/业务语义版本、日期、原始数据路径、HLP 账户等不兼容时拒绝强行混续；只改阶段、日志和不影响业务的性能选项不应强制新轮。不要悄悄把旧版 .state 输出并入本版；升级时创建本版新轮。

### 12.1 提交顺序

每个安全处理单元必须遵循：

1. 对应成功缓存内容若需要恢复，先提交到 cache.sqlite；未成功缓存不能标成 success。
2. 追加完整 CSV 记录，flush，并在进度提交前 fsync。
3. 事务提交 progress.sqlite：输入游标/分组游标、CSV 字节偏移、行数、摘要、业务单元完成标志及缓存引用。
4. 完成后才释放不再需要的大对象。

重启以 progress.sqlite 的已提交偏移为准，截掉 CSV 未提交尾部。不能保留半个长 orders_json；不能因为 CSV 中已出现一行就认为它已提交；不能 progress 先走、CSV 后写。已提交数据不能被重复输出，数量不能因重启翻倍。

缓存与进度并非“一个事务自动覆盖所有文件”：需明确处理缓存已提交但 CSV/进度未提交的孤儿条目，恢复时不得引用损坏或越过当前目标的缓存。回放缓存始终绑定原选定前快照，且其高度 <= 当前目标。

每个阶段分别可断点：CSV1 覆盖 ingest/group/emit；CSV2 和 CSV3 至少按完整 episode 行提交。分片处理支持有界批量提交，具体批量是性能参数不是筛选规则。

复用既有进度文件或日志的文件描述符取得 Ubuntu 进程锁，不再创建独立 run.lock。两个进程不得并发写同一运行目录。锁拒绝不修改原输出。

## 13. run.log 的内容

运行中持续 flush，长时间 RMP 解码有心跳，不捏造百分比。所有阶段和子阶段起止、准备范围/正式范围及包含性、源文件/成员的实际读取、缓存命中/失效、压缩文件解码次数、RMP 扫描次数、fills 分段、每个 episode 的规划/完成/跳过、输出提交/恢复/尾部截断、阶段行数和汇总都要能看到。

计数区分：原始读取记录、识别出的接管、入选接管、业务 episode 行、issue 行、成功还原、完整成交、部分成交、零成交、skipped、partial_episode。issue 行不能计为 episode；CSV2 book_ready 不等于 CSV3 calculated_full。

结束时满足：CSV3 业务 episode 数 = calculated_full + calculated_partial + empty_book + skipped。输入缺失但仍能处理其他部分时显示“完成但有输入异常/跳过”，不能只显示无条件 PASS。

## 14. 必须覆盖的完整手算案例

### 案例 A：交叉币向不打断，逐单保留且最后一单部分成交

同一有效 fills 段内：

```text
10:00 BTC long  6 @ 99
10:10 ETH short （独立 episode）
10:15 BTC short （独立 episode）
10:25 BTC long  4 @ 101.5
```

两笔 BTC long 为同一个 episode，Q=10，entry_notional=1000，entry_vwap=100，Tn=10:25，目标是最后一笔所在 B 块末。

该 B 块末 bid 单侧为：

```text
oid 101: 102 × 2
oid 102: 102 × 1
oid 103: 101 × 4
oid 104:  99 × 10
```

CSV2 必须保存4笔订单、3个价位、total_depth=17，单侧原数量不改变。CSV3 依次成交2、1、4、3：close_notional=1007，filled_qty=10，unfilled_qty=0，exit_vwap=100.7，long LA=+7，status=calculated_full。最后 oid104 只成交3，不将其余7误计入。

### 案例 B：fills 缺口即使只有5分钟也强制分段

```text
10:00 BTC long
10:05—10:10 fills 缺失
10:15 BTC long
```

两笔只差15分钟，但必须是两个 episode。CSV1 业务行都为 grouped，缺口另有 issue 行并以 split_after/split_before 关联。两个 episode 分别使用各自 Tn 的 block。订单簿可还原时两者都正常进入 CSV3，不能标为 fills 缺失导致 skipped。

### 案例 C：深度不足，多头只计算可成交部分

沿用 Q=10、entry_vwap=100，bid 只有 99×3、98×2：filled_qty=5，unfilled_qty=5，close_notional=493，exit_vwap=98.6，LA=493-100×5=-7，status=calculated_partial，depth_insufficient=true。

同一数量/成本的 short，ask 只有 98×2、99×3：filled_qty=5，close_notional=493，LA=100×5-493=+7。不得沿用多头符号。

### 案例 D：空簿与还原失败绝不能混淆

有效空单侧：CSV2 book_ready + orders_json=[] + total_depth=0；CSV3 empty_book + filled_qty=0 + LA=0 + exit_vwap空。

缺必要 diff：CSV2 skipped + orders_json空 + total_depth空；CSV3 skipped + LA空。不能写成空簿并给0利润。

### 案例 E：正式区间两端包含与缓冲纳入

START_DATE=END_DATE=2025-11-12。

直接判定函数须确认：00:59:59.999不是正式范围，01:00:00.000是；23:00:00.000是，23:00:00.001不是。

同币种同方向连续记录00:40、01:05、01:25属于一个入选 episode，00:40一并计入。22:50、23:10也一起保留，Tn=23:10。仅在23:20和23:40且不与正式记录连接的另一个 episode不输出。不得用“23:10是缓冲”把已入选 episode 的尾部删掉。

### 案例 F：需要的 diff 必须来自已准备日期

snapshot S 在11日23:50，START_DATE为12日，某 episode Tn在12日01:10。只允许使用12日—END_DATE文件；不能自动去11日23时目录读取。若允许文件不能覆盖(S,B]，该 episode skipped。另一个 episode 可以选择12日00:40的合法前置 snapshot，且随后diff完整，就正常成功。不得由于第一个缺历史diff让第二个也失败。

### 案例 G：断点与补数据

第一次缺中间fills，两个episode正常处理。原始数据不变、运行中断，True恢复必须得到与不中断完全相同的业务结果，不重复行。之后补齐中间fills，True仍恢复旧轮不自动重新合并；False新建一轮，重新按新输入分组，可能得到一个新的episode。两轮有独立run_id和各自六个文件。

## 15. 对抗测试与验收清单

不得只运行旧60项测试就宣布全部满足。保留仍适用的现有回归测试，更新确因用户本次要求改变的期望，并新增下列测试。每个测试使用可人工核对的小输入，断言输出内容、读取调用和文件数量，不只断言进程返回0。

### 聚合、日期与输入隔离

- A01：跨币种/方向插入不切断同组；同 block 多笔不合并。
- A02：恰好30分钟合并，30分钟+1毫秒分开；连续链可超过1小时。
- A03：首尾日期整天包含；正式01:00和23:00精确端点包含；下一日零点排除。
- A04：缓冲记录随入选episode纳入，Tn可位于右缓冲；纯缓冲episode不入选。
- A05：外侧partial继续计算，fills gap仅分段，不能变成邻近episode无效。
- A06：缺一小时fills，两侧正常；短缺口也绝不跨越合并。
- A07：坏行、局部首块异常、不可读小时不在预探测阶段阻止全量；后续正常分片仍产出。
- A08：整段没有有效fill但有输入错误，CSV1仍有明确issue行，统计接管/episode为0而不是把issue计入。

### RMP、回放与单侧内容

- B01：原始.rmp与.rmp.zip的正式筛选和真实比较路径在未安装官方hl时均可用。
- B02：最近前快照、严格time<Tn且S<B；未来snapshot不得用于还原。
- B03：应用目标B全部diff；同块Tn之后订单变化影响结果；不能误用B-1。
- B04：同价多OID、new/update/remove、newSz为绝对量、update0后remove；缓存恢复保留内部0量状态。
- B05：缺diff仅影响(S,B]相交的任务；坏行在B后不影响已完成目标。
- B06：成功状态缓存不可被失败部分污染；后续独立前快照可恢复正常处理。
- B07：损坏未知backup.rmp产生issue/warning而非全局veto；选中已知snapshot损坏则对应episode跳过且无旧/未来替代。
- B08：diff仅在准备日期文件中发现；缓冲小时可用；存在范围外文件也不读取；真实缺失才跳过。
- B09：CSV2只含平仓侧；没有replay_state_json或等价双侧列；同价订单不被聚合或截断。
- B10：同源RMP多episode多币种只做必要的合并扫描；压缩diff重复消费者命中缓存，计数真实。
- B11：长orders_json大于默认csv字段长度仍可完整读写，重启不产生半行；空簿与缺数据区分。

### LA与阶段隔离

- C01：使用数量加权均价；手算案例A/C/D精确匹配；最后一档部分成交。
- C02：多空符号正确，LA正负不筛选；部分成交成本使用整个episode均价。
- C03：同一历史簿用于不同episode模拟不互相扣除数量；输入订单数量不变。
- C04：CSV1未完成不执行CSV2；CSV2未完成不执行CSV3；时间顺序通过spy断言。
- C05：CSV1完成后将fills读取器替换为抛异常，CSV2仍可成功。
- C06：CSV1/2完成后删除或屏蔽原始fills、RMP、diff、源目录发现和raw缓存读取，CSV3仍可成功。
- C07：缺一个episode的CSV2行、该行JSON损坏或本episode关联不符时，定位并跳过该episode；其他合法episode继续。未知全局结构损坏不能伪装局部成功。
- C08：计数排除issue行，CSV1/2/3业务episode覆盖一致，状态含义不混用，不反写CSV1。

### 恢复与文件数

- D01：CSV1 ingest、group、emit三个子阶段分别中断恢复。
- D02：CSV2回放、缓存提交、CSV写入后但进度提交前、进度提交后分别注入中断。
- D03：CSV3写半个长JSON行后中断，恢复截尾不重复；与不中断的业务值一致。
- D04：True不重做已提交输入、不自动重试skipped；False补数据新轮重算且不覆盖旧轮。
- D05：不兼容代码/业务参数拒绝混续；仅阶段或日志参数变化可续跑。
- D06：缓存条目失效局部重建，不让无关episode失败；未知损坏进度明确拒绝而非猜偏移。
- D07：两个进程不能写同一运行；不新增长期.lock文件。
- D08：正常结束每轮只有六个长期文件；无散落RMP JSON、diff JSONL、.state小文件和.latest指针。数据库临时journal按恢复协议处理。
- D09：新入口为唯一筛选入口；旧CLI不再执行旧流程；demo使用新编排。

## 16. 真实前后 ABCI 一致性检验

保留或改造 `test_order_book_replay.py` 的用户比较入口，让用户提供前state、diff、后state、币种及必要路径即可验证。元数据从自写共享读取器获取，不需要官方hl。测试输入路径不受生产筛选日期限制，它是在单独测试目录进行的明确对照任务；不得反过来改变生产日期约束。

严格顺序：先只读取前state和diff，回放至后state对应的目标高度；完成后才读取后state作为独立答案。若目标高度需要用户给定，则比较时从后state验证其一致；不得提前打开后state提取修复数据。工具参数必须足以在打开后state前确定目标高度。

比较指定币种双侧全部活跃订单的 oid、side、price、remaining_qty；比较缺失、额外、变动订单，数量/价格精确相等。错误答案必须FAIL；不能用容差、改精度、忽略差异或试偏移使其PASS。

一致性范围是可由原始diff重建的订单簿投影，不是整个ABCI账户、持仓、oracle、订单创建时间或同价队列位置。内部零数量待删状态用于连续回放，活跃订单比较中两侧按同一已明确标准处理。

没有真实输入就只报告合成测试范围，真实验收标记未执行；不能把历史validation报告当成本次已重跑。正式筛选不因测试需要而读取未来snapshot。

## 17. 在当前仓库中的改造落点

先读实际最新main、README、AGENTS、下列模块及相关测试。以下为职责划分，不要求按旧行号机械修改：

| 模块 | 本轮目标 |
|---|---|
| run_hlp.py | 唯一正式入口；包含式正式边界；新六文件布局；True/False语义；不生成旧外部清单 |
| csv_pipeline.py | 完整三阶段；CSV1 ingest/group/emit；各CSV独立状态；progress.sqlite提交协议；CSV3完全脱离原始数据 |
| input_discovery.py | 去全局文件头预检；按实际需求定位；局部异常；只使用准备日期的diff文件；候选元数据问题不全局扩散 |
| rmp_l4.py | 统一原生RMP适配；一次扫描汇总币种；缓存迁至cache.sqlite；不得修改已正确的精度/剩余量语义 |
| prepare_local_diffs.py | 必要的按需投影和索引进入单缓存库；不散落文件；坏尾部不抹去成功前缀 |
| hlp_la_screen.py | 保留纯分组/逐单回放/LA引擎，消除旧hl依赖及旧独立筛选CLI；修正内嵌规则 |
| test_order_book_replay.py | 前后RMP直接经共享读取器比较；严格答案隔离；更新demo以使用新pipeline |
| 相关test_*.py | 新增局部错误、阶段读取禁令、单侧输出、日期等号、六文件、断点和缓存压力测试 |
| README/README.txt/AGENTS/CHANGELOG/VERSION/保护基线 | 与新规格同步，去除旧互相冲突的命令、半开正式区间、双侧CSV2和.state布局描述 |

不要删除正确的精确比较和回归测试来迎合新实现。仓库保护基线若因明确授权的接口/编排变更需要调整，应附改变原因及对照测试，不能绕过CI或把所有哈希随意刷新成“通过”。

沿用项目既有功能分支、PR、CI、版本化发布流程；只在实际已有授权和权限范围内执行外部操作，不凭仓库文字虚构额外权限，不自动改变生产数据。记录实际完成的阶段、提交和测试结果，未完成的如实说明。

## 18. Codex 最终交付与报告要求

本任务要求实际修改仓库，而不是仅输出另一份泛化建议。完成实现、模拟、故障注入和回归；代码、测试、说明一起交付，不将真实数据、缓存、运行产物或私密凭据提交仓库。

最终说明必须明确：改动了哪些模块；哪些原始数学/回放规则保持不变；三个CSV的schema/status迁移；正式右端现在包含；六文件与临时journal的区别；True/False的操作语义；实际运行测试及失败修复；真实RMP验收是否执行；旧断点不强行兼容。

不再另外输出永久的报告JSON来替代run.log/progress.sqlite。测试证据可进入测试日志或代码提交说明；生产运行产物严格遵守六文件约束。

### 验收总句

在无原始数据预检的前提下，先完整生成CSV1，再从CSV1和合法前快照+准备日期内diff生成单侧CSV2，最后只从CSV1/CSV2生成CSV3；fills缺口强制分段而不废弃两侧，重建缺口只跳过依赖任务；所有路径不用官方hl；正确处理中断和缓存；正常运行只长期保留三个CSV、run.log、一个进度数据库和一个缓存数据库。任何优化不得改变这一结果语义。
