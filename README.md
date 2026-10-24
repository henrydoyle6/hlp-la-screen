# HLP LA Screen · Ubuntu 安装、三阶段运行与断点续跑

本项目识别 HLP 接管并分组，还原目标块末订单簿，再模拟平仓、计算 LA。

**只修改 [`run_hlp.py`](run_hlp.py) 顶部配置，正式启动只运行 `python run_hlp.py`。**

```text
已准备好的 fills → csv1.csv → 前快照 + diff → csv2.csv
                    └─────────────────────────┴──→ csv3.csv
```

- **csv1**：读取 fills、完成分组并保存每笔接管及 episode 汇总。
- **csv2**：只从 csv1 获取 episode，再调用原订单簿引擎；不重新读取 fills。
- **csv3**：只从 csv1 和 csv2 计算模拟成交及 LA；不读取 fills、ABCI 快照或 diff。
- 三个阶段均有持久化断点。默认重新执行相同配置会继续已有进度，而不是重做已完成阶段。

> 仓库没有真实 fills、diff 或 `.rmp` 数据，也不会下载这些数据。首次生成 csv1/csv2 前，请先准备自己的原始数据。没有真实数据时，可以先执行可选测试。csv1、csv2 完成后，单独生成 csv3 不要求原始数据仍然可用。

## 1. Ubuntu 安装与 clone

需要 **Python 3.10 或更新版本**。验证环境为 Ubuntu（WSL2）+ Python 3.12.3。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nano

python3 --version
python3 -c 'import sys; assert sys.version_info >= (3, 10), "本项目需要 Python 3.10 或更新版本"'

git clone https://github.com/henrydoyle6/hlp-la-screen.git
cd hlp-la-screen

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

版本检查失败时，先提供 Python 3.10 或更新版本，再用该解释器创建虚拟环境。已有源码目录时直接进入目录，不必再次 clone。

依赖固定为 `msgpack==1.2.3`、`orjson==3.13.0`。常见 ZIP/RMP/JSONL 输入无需安装 `hl-node`，也不需要手动解压原始 ZIP 或全量转换快照。仅使用 `.zst` / `.lz4` 输入时额外安装：

```bash
python -m pip install zstandard lz4
```

## 2. 日期填写的是“已准备好的数据范围”

只填写年月日，**首尾日期都包含整天，按 UTC 解释**。程序在这个范围内部留两端各一小时，不再向外增加数据需求。

例如：

```python
START_DATE = '2025-11-12'
END_DATE = '2025-11-13'
```

| 边界 | UTC 时间 |
| --- | --- |
| 数据开始边界 | 2025-11-12 00:00:00 |
| 数据结束边界，不包含 | 2025-11-14 00:00:00 |
| 正式开始，包含 | **2025-11-12 01:00:00** |
| 正式结束边界，不包含 | **2025-11-13 23:00:00** |

公式：`正式开始 = 数据开始 + 1小时`；`正式结束边界 = 数据结束边界 - 1小时`，右端不包含。开始、结束日期相同表示已准备一整天，该日正式范围为 `[01:00, 23:00)`。

不再为了上例向你索取 11 月 11 日 23 时或 11 月 14 日 0/1 时的 fills/diff 分片。范围内的首块时间仍从数据读取，处理文件分区时钟与链上时钟差异。

**episode 至少有一笔落入正式范围，就保留整个已读取 episode，包括内部缓冲区的接管。** 所以 csv1 中可以有正式开始之前或正式结束之后的接管行，但不会读取数据范围之外的 fills 来补全 episode。

## 3. 数据目录怎么放

原始数据可以留在现有磁盘或挂载目录，只需将配置改为实际位置。下例与默认路径一致，**目录中要有真实数据，空目录不能替代数据准备**。

```text
/data/hyperliquid/
├── fills/                              # FILLS_ROOT
│   ├── 20251112/
│   │   ├── 0.zip                       # 当天 00 时，属于已准备范围
│   │   ├── 1.zip
│   │   └── ...                         # 至 23.zip
│   └── 20251113/
│       ├── 0.zip
│       └── ...                         # 至 23.zip
├── node_raw_book_diffs_by_block/        # DIFFS_ROOT
│   ├── 20251112/
│   │   ├── 0.zip
│   │   └── ...
│   └── 20251113/
│       ├── 0.zip
│       └── ...
└── periodic_abci_states/               # SNAPSHOTS_ROOT
    └── 20251112/
        ├── 793650000.rmp.zip
        └── 793700000.rmp.zip
```

- `FILLS_ROOT`、`DIFFS_ROOT` 指向**日期目录的上一级**，不能只填某一天的目录。
- `SNAPSHOTS_ROOT` 递归发现原始快照，原读取器从快照中识别高度、时间、币种、asset 和精度。
- 仍选择 `Tn` 前合规的最近前快照，并完整回放至目标 block 结束。需要的 diff 如果未在已准备范围内得到完整覆盖，就跳过相关 episode，不向范围外查找额外小时，也不用未来快照补救。
- 三个输入目录需可读、可遍历。输出和缓存选择当前用户有写入权限的位置，并放在原始数据目录之外。

### 命名与格式

fills/diff 支持 `根目录/YYYYMMDD/H`、`根目录/hourly/YYYYMMDD/H`，日期也可为 `YYYY-MM-DD`，小时可为 `0`–`23` 或 `00`–`23`。

小时文件支持无扩展名、`.jsonl`、`.zip`、`.jsonl.zip`、`.jsonl.gz`、`.gz`、`.bz2`、`.xz`、`.zst`、`.lz4`。ZIP 只能有一个非目录成员，例如 `hourly/20251112/0`。

内容为 UTF-8 JSONL，一行一个完整 block，空事件块也要保留：

```json
{"block_number":793700001,"block_time":"2025-11-12T01:27:40.700Z","events":[]}
```

fills 使用 node 原始 `[owner_address, fill_object]`，diff 使用原始逐订单 `raw_book_diff`。

**同一小时/同一快照只保留一份编码。** 不要同时放 `0.zip` 与 `00.jsonl`，也不要同时保留同一快照的 `.rmp` 和 `.rmp.zip`；存在歧义会明确报告。

## 4. 唯一需要修改的文件：`run_hlp.py` 顶部

```bash
nano run_hlp.py
```

找到 `USER CONFIGURATION / 只修改本区`，修改数据日期和五个目录：

```python
START_DATE = '2025-11-12'
END_DATE = '2025-11-13'

FILLS_ROOT = '/data/hyperliquid/fills'
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'
CACHE_ROOT = '/data/hlp_la/cache'
```

| 配置 | 含义 |
| --- | --- |
| `START_DATE` / `END_DATE` | 已准备的数据首日 / 最后一日，不是直接填写正式筛选边界 |
| `FILLS_ROOT` | fills 日期目录的上一级 |
| `DIFFS_ROOT` | diff 日期目录的上一级 |
| `SNAPSHOTS_ROOT` | 原始 ABCI 快照根目录 |
| `OUTPUT_ROOT` | 运行目录根位置；相同配置默认继续最近一轮 |
| `CACHE_ROOT` | 持久化提取/输入缓存根位置，与输出根目录分开 |

使用 Ubuntu 的实际绝对路径，不要填 `E:\...`、`D:\...`。WSL 才可能使用 `/mnt/e/...`。中文和空格可用，保留 Python 引号，shell 中用双引号包住路径。

通常保留以下默认值：

```python
DETAILED_LOG = True
CONSOLE_LOG = True
LOG_BLOCK_INTERVAL = 1000
PREPARE_COIN_DIFFS = True
RUN_STAGE = 'all'
RESUME = True
```

`HLP_ACCOUNTS` 保留当前四个默认接收方账户；研究口径变化时才调整，程序不会联网更新名单。`CONSOLE_LOG=False` 仍完整写入 `run.log`。

nano 中按 **Ctrl+O → 回车** 保存，**Ctrl+X** 退出。无需手填快照高度/时间、币种 asset/精度、分片清单或额外 JSON 配置。

## 5. 运行哪些脚本

### 完整运行及继续断点

```bash
python run_hlp.py
```

默认 `RUN_STAGE='all'` 按顺序完成三个 CSV。中断后保持日期、路径等业务配置不变，保留 `RESUME=True`，重新执行同一命令即可。

| 文件 | 用途 | 是否单独执行 |
| --- | --- | --- |
| `run_hlp.py` | 顶部配置、选择运行目录、锁和三阶段入口 | **是，唯一正式启动脚本** |
| `csv_pipeline.py` | CSV 依赖、校验、持久化断点及阶段执行 | 否，入口自动调用 |
| `input_discovery.py` | 定位所需小时分片和前快照 | 否，仅需要原始数据的阶段调用 |
| `rmp_l4.py` | 原有 RMP 按需提取与成功缓存 | 否，读取实现保持不变 |
| `prepare_local_diffs.py` | 原有币种 diff 投影与缓存 | 否，自动调用 |
| `hlp_la_screen.py` | 原分组、订单簿回放及 LA 算法 | 否，作为共享引擎使用 |
| `extract_rmp_l4.py` / `test_order_book_replay.py` | 可选独立提取、测试与精确比较 | 正式筛选不需要 |

### 分阶段运行

只需修改同一顶部配置的 `RUN_STAGE`，每次仍执行 `python run_hlp.py`：

| `RUN_STAGE` | 行为 | 前置条件 |
| --- | --- | --- |
| `'all'` | 依次完成/续跑 csv1、csv2、csv3 | 默认选项 |
| `'csv1'` | 读取 fills，完成分组 CSV | 已准备 fills |
| `'csv2'` | 读取已完成 csv1，生成/续跑订单簿 CSV | 同一轮 csv1 已完成 |
| `'csv3'` | 只读 csv1、csv2，生成/续跑模拟成交 CSV | 同一轮 csv1、csv2 已完成 |

阶段切换时保持 **`RESUME=True`**。不要每个阶段都设置 `False`，否则会新建一轮，找不到上一阶段的输出。csv3 阶段不会为前置 CSV 自动回头扫描原始数据；前置阶段未完成会明确报错。

`RESUME=False` 用于主动从头新建一轮，原输出保留。新建后若再次中断，把它改回 `True` 会继续同配置最近新建的一轮。

重新登录时：

```bash
cd /你的源码目录/hlp-la-screen
source .venv/bin/activate
python run_hlp.py
```

需要关闭 SSH 后继续运行时，在已激活环境的源码目录启动：

```bash
nohup python -u run_hlp.py > launcher.log 2>&1 &
tail -f launcher.log
```

## 6. 断点粒度、CSV 内容和日志

| CSV | 内容和状态 | 断点提交单位 |
| --- | --- | --- |
| csv1 | 每笔接管一行；分组完成后 `status=grouped`，汇总信息稳定，不被后续阶段反写 | 一个 fills 小时分片 |
| csv2 | 每 episode 一行；`ok` 表示订单簿还原成功，`skipped` 表示还原失败 | 一个 episode |
| csv3 | 每 episode 的模拟成交和 LA；`ok`、`empty_book`、`partial_depth` 或 `skipped` | 一个 episode |

csv1 读取期间可以有 `reading` 草稿行，完成后原子替换为正式分组结果。已提交小时不重读；中断时正在处理且未提交的 ZIP 小时可能重读，**不是 ZIP 内任意字节位置续读**。

csv2 保留原 `orders_json` 平仓侧逐订单量，新增 `replay_state_json` 保存双侧完整回放状态（包括等待 remove 的零数量订单），用于断点恢复后继续向前回放。同价订单不合并。

csv3 使用 csv1 的接管信息与 csv2 的 `orders_json`，调用原 LA 算法；不使用原始 RMP/diff/fills。`fill_trace_json` 中的 `oid` 对应 csv2 订单，最后一单可部分成交；模拟不改变 csv2 的历史订单量。

三个 CSV 均包含 `episode_input_hash` 关联校验，csv3 另保存 `csv2_row_hash`。校验文件损坏或上游不匹配会报错，不会靠容差、改数量或静默重做掩盖。

### 输出目录

启动时会打印本轮目录和 `RUN.LOG` 的完整路径：

```text
OUTPUT_ROOT/
├── .latest_<配置标识>.json
└── run_<数据开始日>_<数据结束日>_<配置标识>/
    ├── result/
    │   ├── csv1.csv
    │   ├── csv2.csv
    │   ├── csv3.csv
    │   ├── run.log
    │   └── .state/                 # 必须保留：三个 CSV 的断点、日志式中间记录和运行锁
    ├── user_config.json
    ├── fills_discovery.json        # csv1 首次定位时生成
    ├── book_discovery.json         # csv2 需要定位时生成
    ├── resolved_config.json
    ├── source/
    ├── source_sha256.json
    ├── stage_summary.json
    ├── run_summary.json            # all / csv3 完成后生成
    └── process_exit.json
```

主动新建时，目录名另附 UTC 时间和后缀。**续跑请保留整轮目录（包括隐藏 `.state`），不要只保留三个 CSV，也不要手动修改断点 JSON。** 保持 `OUTPUT_ROOT` 不变以自动定位最近一轮。

把下面路径替换成终端打印的实际日志完整路径：

```bash
tail -n 50 -f "/data/hlp_la/output/run_.../result/run.log"
```

`launcher.log` 是后台终端输出，完整业务日志在 `result/run.log`。日志记录分片进度、CSV 断点提交/复用/尾部回滚、回放缓存恢复、RMP/输入缓存命中、模拟成交和跳过原因。

进度只在 CSV 数据写入、flush/fsync 成功后提交。重启时会校验已提交前缀，丢弃未提交尾部；已提交内容损坏则明确停止。并发写同一运行目录会被运行锁拒绝。

`process_exit.json`：`STAGE_COMPLETED` 为单独 csv1/csv2 完成，`COMPLETED` 为 all/csv3 完成，`INTERRUPTED` 可按断点继续，`FAILED` 需查看原因。`run_summary.json` 中 `calculated + skipped = episodes`；深度不足是模拟结果，不等于订单簿还原失败。

## 7. 可选测试与验收记录

无需真实数据即可运行全部测试：

```bash
python -m unittest discover -s . -p 'test_*.py'
```

原合成案例、独立提取/比较工具：

```bash
python test_order_book_replay.py --demo --demo-output /tmp/hlp_demo
python extract_rmp_l4.py --help
python test_order_book_replay.py --help
```

当前三阶段/断点/日期规则验收见 [`validation/CSV_PIPELINE_ACCEPTANCE.txt`](validation/CSV_PIPELINE_ACCEPTANCE.txt)，原先的历史校验范围见 [`validation/VALIDATION.txt`](validation/VALIDATION.txt)。RMP 读取、原订单簿回放和精确比较没有为本次改造更改。

## 8. 常见问题与更新代码

| 现象 | 处理 |
| --- | --- |
| venv / ensurepip 缺失 | 安装 `python3-venv` 后创建环境 |
| 缺少 msgpack / orjson | 激活 `.venv`，重新安装 `requirements.txt` |
| 找不到目录 / 权限不足 | 核对当前阶段需要的数据根目录及读写权限 |
| 没有接管 | 检查 UTC 已准备日期、正式区间、数据和 HLP 接收方口径 |
| fills 缺口 | 原规则强制分段；两侧已知 episode 保留，不跨缺口合并 |
| snapshot/diff 不足，csv2 为 skipped | 补齐数据后主动新建一轮重算；已提交的 skipped 也属于该轮结果，不自动重试 |
| CSV 校验失败 | 保留报错文件与日志，排查损坏；不要修改校验值强行通过 |
| 代码版本不匹配 | 改为 `RESUME=False` 新建一轮，不能混用不同版本的断点 |

修改日期、原始路径或 HLP 账户等业务配置会选择不同任务；阶段或日志选项变化不改变原任务。旧版没有本版断点的新/旧输出不会强行拼接，首次使用本版会建立自己的运行目录。

工作区没有未提交改动时获取更新：

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如果只有 `run_hlp.py` 的本地配置尚未提交，先在仓库外备份，再暂存、拉取、恢复：

```bash
cp run_hlp.py ../run_hlp.local.backup.py &&
git stash push -m "local Ubuntu configuration" -- run_hlp.py &&
git pull --ff-only &&
git stash pop
```

恢复配置出现冲突时，根据备份合并后再运行；不要用 `git reset --hard` 覆盖自己的配置。代码更新后若提示旧断点版本不同，请主动新建一轮。

助手完成本项目代码更新和验证后会提交并推送到 `origin/main`，见 [`AGENTS.md`](AGENTS.md)。公开查看和 clone 不需要邀请；推送仍需要有写入权限的账户。
