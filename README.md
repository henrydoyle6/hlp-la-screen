# HLP LA Screen · Ubuntu v0.3.1

识别 Hyperliquid node fills 中的 HLP backstop 接管，聚合 episode，重建最后接管所在 **block B 结束**的订单簿，再模拟平仓与 LA。用于事后研究，不下真实订单。

**只修改 `run_hlp.py` 顶部配置，正式启动只运行 `python run_hlp.py`。**

```text
fills → 完成 CSV1 → 前快照 + 准备日期内的 diff → 完成单侧 CSV2 → 仅 CSV1/CSV2 → CSV3
```

三个阶段严格依次完成。CSV1 完成后不被后续反写；CSV2 不重读 fills；CSV3 不读取 fills、RMP、diff、源目录或原始数据缓存；通过进度库中的 CSV2 字节索引逐 episode 读取，不同时常驻所有订单字段。

**已有正确的完整 CSV1 时，可只接收这一个文件，在新运行中生成 CSV2/CSV3。** 不需要原 fills、旧 progress.sqlite 或旧 cache.sqlite；配置方法见第 4 节。CSV1 是这轮的权威接管输入，程序不重新分组。

## 1. Ubuntu 安装指定版本

**真实 RMP 生产运行统一使用 Python 3.12。** 以下安装命令以 Ubuntu 24.04 为例。Python 3.10 的时间解析器不能读取真实快照中九位小数的纳秒时间，会导致大量快照被排除；本版修复订单存储兼容，仍要求生产使用 3.12。较旧 Ubuntu 请先准备 Python 3.12，不要直接使用默认的 3.10。

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv python3-pip nano
python3.12 --version
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'

VERSION=v0.3.1
git clone --branch "$VERSION" --depth 1 https://github.com/henrydoyle6/hlp-la-screen.git "hlp-la-screen-$VERSION"
cd "hlp-la-screen-$VERSION"
git describe --tags --exact-match
python3.12 -m venv .venv
source .venv/bin/activate
python -c 'import sys; print(sys.version); print(sys.executable)'
python -m pip install -r requirements.txt
```

`git describe` 应显示指定标签；detached HEAD 是固定版本的正常状态。只安装 [Releases](https://github.com/henrydoyle6/hlp-la-screen/releases) 中已经成功发布的版本，正式运行不跟随 main。依赖固定为 `msgpack==1.2.3`、`orjson==3.13.0`，SQLite 在 Python 标准库中。

常见 `.zip`、`.rmp`、JSONL 不需要其他解压程序。只有使用 `.zst` / `.lz4` 输入时额外执行：

```bash
python -m pip install zstandard lz4
```

如果出现 ensurepip/venv 缺失，先完成上面的 apt 安装，并使用满足版本要求的 Python 创建环境。

## 2. 数据如何摆放

程序不下载原始数据，不要求你手填分片清单、asset、精度、快照高度或快照时间。

```text
/data/hyperliquid/
├── fills/                              # FILLS_ROOT
│   ├── 20251112/0.zip ... 23.zip
│   └── 20251113/0.zip ... 23.zip
├── node_raw_book_diffs_by_block/        # DIFFS_ROOT
│   ├── 20251112/0.zip ... 23.zip
│   └── 20251113/0.zip ... 23.zip
└── periodic_abci_states/               # SNAPSHOTS_ROOT
    └── 20251112/
        ├── 793650000.rmp.zip
        └── 793700000.rmp.zip
```

fills/diff 根目录指向日期目录的上一级。也支持 `根目录/hourly/YYYYMMDD/H`、`YYYY-MM-DD`、小时 0–23 或 00–23。支持无扩展名、`.jsonl`、`.zip`、`.jsonl.zip`、`.gz`、`.jsonl.gz`、`.bz2`、`.xz`、`.zst`、`.lz4`。同一小时只放一种编码；ZIP 应有一个非目录成员。

fills 和 diff 都是一行一个完整 block 的 UTF-8 JSONL，空 `events` 块有效。fills 的事件是原生 `[owner_address, fill_object]`，diff 使用原始逐单 `raw_book_diff`。

```json
{"block_number":793700001,"block_time":"2025-11-12T01:27:40.700Z","events":[]}
```

快照递归发现原始 `.rmp` 或 `.rmp.zip` 等受支持压缩格式，使用快照自身元数据确定高度、时间、市场和精度。不要把单币种导出 JSON 混入原始快照目录。

同时支持旧 `exchange.context` 和新 `exchange.locus`（`cls/scl/ctx`）布局；按真实 pdi、asset 与精度定位所需币种。无关现货条目缺少 `name` 不再阻断所需永续币种；所需币种无法可靠定位时明确失败，不猜测名称或生成假空簿。

v0.3.1 同时支持 `book_orders` 以及新的 `halfs / oid_to_key / bod` 订单存储。`bod.e` 的占用槽、真实 OID 映射和双侧价格链须完整一致；空闲槽不输出，剩余量仍使用原订单的 `r`，不使用原始量 `O` 代替。适配后复用原精度换算与逐单回放。未知或损坏结构明确失败，不当成空簿。

## 3. 需要修改的配置文件

```bash
nano run_hlp.py
```

只修改 `USER CONFIGURATION / 只修改本区`：

```python
START_DATE = '2025-11-12'
END_DATE = '2025-11-13'
FILLS_ROOT = '/data/hyperliquid/fills'
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'
CSV1_INPUT = ''  # 留空：从 fills 生成；已有完整 CSV1：填绝对路径
RUN_STAGE = 'all'
RESUME = True
DETAILED_LOG = True
CONSOLE_LOG = True
LOG_BLOCK_INTERVAL = 1000
```

只填年月日，首尾两天都包含，时间统一为 UTC。上例对应：

| 范围 | 精确时间 |
| --- | --- |
| 已准备的原始接管范围 | `[2025-11-12 00:00:00, 2025-11-14 00:00:00)` |
| 正式筛选范围 | **`[2025-11-12 01:00:00, 2025-11-13 23:00:00]`，两端包含** |

23:00:00.000 本身属于正式范围，23:00:00.001 属于右缓冲。不会向日期范围之外额外索取一小时。一个 episode 只要有一笔正式接管，就纳入这个 episode 已读取的全部缓冲接管；Tn 可以位于右缓冲。纯缓冲 episode 不入选。

输入、输出填写 Ubuntu 的绝对路径；WSL 可以使用 `/mnt/d/...`、`/mnt/e/...`，普通 Ubuntu 使用实际挂载位置。输出放在三个原始数据根目录之外。路径含空格或中文时保留 Python 引号，shell 中也用引号包住路径。

保留四个现有 `HLP_ACCOUNTS` 默认研究账户，除非研究口径明确变更。程序不联网更新名单，不加币种、最低金额、最低数量或盈利过滤。无需配置独立 CACHE_ROOT。

WSL 中原始数据可以继续放在 `/mnt/d`、`/mnt/e`，输出数据库优先放 Ubuntu 本地文件系统。此次实测挂载盘 SQLite 随机 I/O 明显更慢；正式输出使用持久目录，勿用可能被系统清理的 `/tmp` 保存长期断点。

## 4. 执行哪些脚本

```bash
python run_hlp.py
```

默认 `RUN_STAGE='all'` 完成全部阶段。只做某阶段时仍运行同一脚本：

| RUN_STAGE | 业务读取 | 前提 |
| --- | --- | --- |
| `csv1` | CSV1_INPUT 留空时读 fills、识别与分组；有值时导入该 CSV1 | 无 |
| `csv2` | 正式 CSV1、合法前快照和必要 diff | CSV1 已完成，或设置 CSV1_INPUT 导入 |
| `csv3` | 正式 CSV1、CSV2 | CSV1/CSV2 已完成 |

阶段切换保持 `RESUME=True`。未完成上游时会提示；显式设置 CSV1_INPUT 时，all/csv1/csv2 可完成导入。csv3 始终要求本轮 CSV1/CSV2 已完成，不会为补上游去导入或重读原始数据。测试、独立导出与比较工具不用在正常筛选前执行。`hlp_la_screen.py` 只作为共享引擎；旧筛选 CLI 已移除。

### 把完整 CSV1 交给同学，再生成 CSV2、CSV3

生成方使用修复后的完整 fills，设置 `CSV1_INPUT=''`、`RUN_STAGE='csv1'`、`RESUME=False`，运行 `python run_hlp.py`；确认 CSV1 阶段完成，再发送该轮原始 `csv1.csv` 文件。不要从未完成运行截取，不要经 Excel 重存或删改行。发送方保留原六文件运行目录，接收方只需 CSV1。

同学按第 1 节安装 v0.3.1，将收到的文件放在 `/data/hlp_la/input/csv1.csv`。例如源 CSV1 的准备范围是 2025-10-21 至 2026-05-18，在 `run_hlp.py` 顶部填写：

```python
START_DATE = '2025-10-21'  # 必须与提供的 CSV1 记录的准备范围一致
END_DATE = '2026-05-18'
FILLS_ROOT = '/data/hyperliquid/fills'  # 导入模式不读取，可不存在；仍需绝对路径
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output-v0.3.1'
CSV1_INPUT = '/data/hlp_la/input/csv1.csv'
RUN_STAGE = 'all'
RESUME = False  # 第一次新建一轮；中断后改为 True
DETAILED_LOG = True
CONSOLE_LOG = True
LOG_BLOCK_INTERVAL = 1000
```

然后只运行 `python run_hlp.py`。程序校验并导入 CSV1 → 按需提取合法前快照和回放 diff 生成 CSV2 → 仅用 CSV1/CSV2 生成 CSV3。无需运行旧 fills 脚本或手工提取 RMP。diff/快照必须覆盖各 episode 的实际需求；排错样本中的两天数据不能代替整个日期范围。四个默认 HLP 账户保持原研究口径。

导入接受 `hlp-csv-v3` 格式（含 v0.2.0 导出）；旧简版 CSV、日期不符、损坏哈希、重复接管或不完整 episode 会拒绝。新 CSV1 只改成本轮 run_id，保留 episode_id、episode_input_hash、接管与分段/partial 信息。日志和进度库记录来源 SHA256、来源运行与导入进度；读取、校验、输出都可断点恢复。导入未完成前源文件必须保持原样。

导入校验不能证明原 fills 中没有遗漏整组 episode；提供方仍须确认 CSV1 是完整、可信的最终导出。导入汇总的 raw_records / recognized_fills 为 null，因为本轮没有扫描原 fills；imported_fills 才是导入接管数。

中断后保留所有路径和配置，改 `RESUME=True` 再运行。CSV1 导入完成后本轮已保存正式 CSV1，续跑/CSV3 不再依赖外部 CSV1，但配置中仍保留原 CSV1_INPUT 路径以定位本轮。即使外部文件随后变化，已提交 CSV1 也不会自动替换；换正确文件、补数据或重算，必须 `RESUME=False` 新开一轮。只想先算 CSV2 可用 `RUN_STAGE='csv2'`，之后保持 True 改为 csv3。

重新登录后：

```bash
cd /你的源码位置/hlp-la-screen-v0.3.1
source .venv/bin/activate
python run_hlp.py
```

需要关闭 SSH 后继续运行，可使用 tmux/screen；例如先安装 tmux，再在会话中执行同一命令。完整业务日志持续写入运行目录的 `run.log`，无需额外 launcher 文件。

## 5. 恢复与重算

| 场景 | 做法 |
| --- | --- |
| 原始输入没变，只是程序中断 | `RESUME=True`，执行相同命令 |
| CSV1 切换到 CSV2 或 CSV3 | 保持 True，只改 RUN_STAGE |
| 已补 fills、补 diff、改输入，或主动重算 | **`RESUME=False` 新建一轮** |
| 新轮建立后中断 | 改回 True，继续同配置最近新轮 |
| 代码版本/语义与旧断点不兼容 | False 新轮，不修改旧哈希或断点强行继续 |

True 不自动重试已提交的 skipped，也不因补数据重新合并旧 episode。False 有独立 run_id 和缓存，旧轮保留。只改阶段、日志或日志间隔不会改变业务身份。

本版使用 sqlite-stages-v4，提取缓存为 rmp-l4-sqlite-v5；引擎哈希包含新的存储适配模块。v0.3.0 或更早代码的数据库不能混续。升级先保存旧代码/输出，只把顶部配置值填到新脚本，使用 False 新建一轮；不要整份复制旧脚本覆盖新代码。v0.2.0/v0.3.0 的完整 hlp-csv-v3 CSV1 可通过 CSV1_INPUT 导入新轮，不能修改旧哈希或复制旧数据库强行续跑。

## 6. 六个长期运行文件

```text
OUTPUT_ROOT/
└── run_<日期>_<配置标识>_<UTC时间>_<run_id>/
    ├── csv1.csv
    ├── csv2.csv
    ├── csv3.csv
    ├── run.log
    ├── progress.sqlite
    └── cache.sqlite
```

没有 `.latest` 指针、独立锁文件、源码副本、发现清单、汇总 JSON 或散落的提取/逐币种 diff 文件。程序从运行目录与进度库定位同配置最近一轮。保存整轮六文件，不只保存 CSV。

SQLite 使用 DELETE journal 与 FULL 同步。事务/崩溃时可能短暂出现恢复 journal；**先由 SQLite 恢复，不手动删除**。正常关闭后是六个长期文件。不要在运行时复制数据库作为可靠断点；先停止进程正常关闭，再备份整轮目录。

`progress.sqlite` 保存配置/版本、CSV1 ingest/group/emit（或导入的读取/校验/输出游标）、已提交接管、分组游标、CSV 偏移/摘要、行关联与汇总。`cache.sqlite` 保存快照元数据、按需提取、成功回放双侧状态及必要已解析 diff；零数量待 remove 的内部记录保留在这里。

提交顺序是：必要缓存提交 → 完整 CSV 写出并 flush/fsync → 进度提交 → 释放当前结果。恢复截去未提交 CSV 尾部；已提交的无法定位损坏不猜测修复。压缩分片中断时未提交部分可能重新解压，不承诺任意字节无成本续跑。

## 7. 三个 CSV 如何理解

通用 `record_type`：CSV1 正常接管是 `fill`；CSV2/3 正常业务结果是 `episode`；输入诊断是 `issue`，不计入接管量、episode 数、均价和 LA。issue 没有未知接管的假价格或假数量。

| CSV | 正常粒度 | 状态 |
| --- | --- | --- |
| CSV1 | 入选 episode 每笔接管一行，含重复 episode 汇总 | `grouped`；诊断行 `input_issue` |
| CSV2 | 每个入选 episode 一行，包括失败 | `book_ready` / `skipped`；另有 `input_issue` |
| CSV3 | 每个入选 episode 一行，包括零成交/失败 | `calculated_full` / `calculated_partial` / `empty_book` / `skipped` |

CSV1 记录原时间、高度、来源成员/行号/块内位置、正式/缓冲计数、partial 与分段标记。相邻同币种同方向间隔 ≤30 分钟合并，其他币种/方向插入不打断；fills 缺口强制分段但不废弃两侧。外侧未闭合部分继续计算并标记 partial。

CSV2 从 Tn 之前最近的合法快照开始，要求快照 `time<Tn` 且 `S<B`，完整应用 `(S,B]`。只读准备日期中的 diff，包括缓冲小时；缺必要块局部 skipped，未来快照不能补洞。无法解析元数据的备份快照独立诊断；选择存在未知候选时标记 `nearest_resolved_with_warning`。

CSV2 只保存平仓侧全部活跃原始订单：long 取 bid，short 取 ask。每个 OID 独立，`orders_json` 为紧凑单字段 JSON，价格和数量是十进制字符串；不限制深度、不合并同价订单。没有 `replay_state_json` 或其他双侧列。

成功空侧为 `book_ready + orders_json=[] + total_depth=0`；无法还原为 `skipped + orders_json空 + total_depth空`，不能混淆。

CSV3 的 `fill_trace_json.oid` 对应 CSV2 订单，含原可用量、实际成交量和金额。最后一单可只成交一部分，模拟不修改历史订单。

```text
entry_vwap = Σ(price × qty) / Σqty
long LA  = close_notional - entry_vwap × filled_qty
short LA = entry_vwap × filled_qty - close_notional
```

部分成交仍使用整个 episode 的接管均价，不估值剩余量。空簿的 LA=0 仅表示零成交；skipped 的 LA 为空。CSV2 可定位的缺行、坏 JSON、关联错误会让对应 CSV3 episode 跳过，其他合法行继续；未知结构损坏明确停止。

## 8. 运行日志与异常

```bash
tail -n 50 -f "/你的输出/run_.../run.log"
```

日志持续 flush；长时间 RMP 扫描有心跳，不捏造百分比。记录阶段/子阶段、实际源和成员、RMP 头探测与正文扫描、diff 解码次数、缓存命中、分段、每个 episode 的目标/结果、模拟轨迹、CSV 提交/恢复及汇总。

启动只检查配置和自身断点，不预读全部原始文件。缺文件、局部坏行、坏 snapshot 等在实际处理时记录到对应 CSV。磁盘满、进度库不可恢复或代码不兼容可终止。完成有异常/跳过时汇总明确标记，不能把所有情况显示为 PASS。

缓存命中仍可能做 stat/边缘身份读取；不等于完全没打开源文件。RMP 正文、头探测和压缩 diff 解码分开计数。

新 locus 布局的 ctx 可能位于约 406 MB 的解压位置。首次定位会流式跳过前置内容，记录实际 header_bytes_read/context_offset，不能期待所有快照都在几 KB 内找到元数据。结果按源身份与布局版本缓存；正文仍按所需币种批量提取，成功才进入提取缓存，失败不充当成功。布局探测、元数据定位和正文提取是不同读取，不承诺单次物理打开 ZIP。

## 9. 测试、独立提取与真实比较

```bash
python -m unittest discover -s . -p 'test_*.py' -v
python test_order_book_replay.py --demo --demo-output /tmp/hlp_demo
```

测试不需要真实市场数据。demo 也调用新 pipeline。完整规格与手算例子在 [docs/vnext_spec.md](docs/vnext_spec.md)。
v0.3.1 的 210 项自动测试、新 arena 存储三组真实前后快照共 7 组逐订单零差异，以及本次 4 个跳过项补齐的范围，见 [本版验收记录](validation/ARENA_ACCEPTANCE.md)。本轮没有重新扫描 210 天 fills，接管分组来自用户提供的完整 CSV1。
[v0.3.0 快照布局与 CSV1 导入验收](validation/RMP_CSV1_ACCEPTANCE.md) 保留为历史证据。
[v0.2.0 两日真实数据验收](validation/VNEXT_ACCEPTANCE.md) 是历史证据，分别保留。

可选独立提取（不是正式筛选的前置步骤）：

```bash
python extract_rmp_l4.py --source /数据/793650000.rmp.zip --coins SOL POPCAT --output-dir /独立测试/l4
```

元数据自动读取；正式筛选和独立比较复用同一原生 RMP 提取器，不需要额外节点转换程序。

真实前后 ABCI 订单簿对照需要明确目标高度，然后执行：

```bash
python test_order_book_replay.py --compare \
  --before-state /数据/793650000.rmp.zip --before-block 793650000 \
  --before-time '填写前快照实际UTC时间' \
  --after-state /数据/793700000.rmp.zip --target-block 793700000 \
  --coin SOL --diffs /测试diff/0.jsonl /测试diff/1.jsonl
```

先完成前快照+diff 回放，再打开后快照作为独立答案；后 state 不参与选前快照或修复。比较双侧所有活跃订单的 OID、方向、价格、剩余量，精确相等，无容差。它不是整个 ABCI 账户/持仓/oracle/队列时间的一致性证明。独立比较路径由用户明确指定，不受正式筛选日期路由限制。多币种可将 `--coin SOL` 改为 `--coins SOL POPCAT`，共用前/后快照的合并提取和压缩 diff 缓存；全部币种完成回放后才读取后快照。

## 10. 维护与版本流程

**功能分支 → PR → 自动测试与验收 → main → 标签与 Release → Ubuntu 指定版本。**

main 必须通过 `Ubuntu tests (3.10)`、`Ubuntu tests (3.12)`、`Acceptance`，管理员同样遵守。发布前核对合并后 main 同一提交的 CI；Release 包含源码、提交信息和 SHA256SUMS。

升级使用新版本目录和新 venv，只复制顶部配置值，原始数据和输出都放在源码目录外。维护者操作见 [RELEASE.md](RELEASE.md)，长期规则见 [AGENTS.md](AGENTS.md)，本轮数学保护基线迁移见 [docs/PROTECTION_MIGRATION.md](docs/PROTECTION_MIGRATION.md)。
