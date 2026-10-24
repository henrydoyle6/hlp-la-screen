# HLP LA Screen · Ubuntu 运行指南

从 node fills 识别 HLP 接管、按 episode 分组，再用前置 ABCI 快照和 raw book diff 还原目标块末订单簿，模拟平仓并输出 LA。

**正式使用只修改 [`run_hlp.py`](run_hlp.py) 顶部配置，然后运行 `python run_hlp.py`。** 原始数据定位、按需提取、缓存和详细日志均由入口自动处理。

> 仓库提供源码、测试和验证记录，**不包含真实 fills、diff 或 `.rmp` 数据，也不会自动下载这些数据**。请先准备或挂载自己的三类输入数据。没有真实数据时，可以先执行下方的可选测试。

## 1. Ubuntu 安装与 clone

需要 **Python 3.10 或更新版本**。当前验证环境为 Ubuntu（WSL2）+ Python 3.12.3。

在 Ubuntu 终端依次执行：

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

如果版本检查失败，请先提供 Python 3.10 或更新版本，再用该解释器创建虚拟环境。已有源码目录时，直接进入该目录，无须再次 clone。

`requirements.txt` 固定已验证的 `msgpack==1.2.3` 和 `orjson==3.13.0`。输入为常见 `.zip`、`.rmp`、`.jsonl`、`.gz`、`.bz2` 或 `.xz` 时，不需要安装 `hl-node`、手动转换全量快照或解压原始 ZIP。

仅当数据使用 `.zst` 或 `.lz4` 压缩时，额外安装：

```bash
python -m pip install zstandard lz4
```

## 2. 准备数据目录

数据可以保留在已有磁盘或挂载目录，只需把下一节的路径改成实际位置。下面的布局与程序默认路径一致；**目录中必须有真实数据，创建空目录不能替代数据准备**。

```text
/data/hyperliquid/
├── fills/                              # FILLS_ROOT
│   ├── 20251111/
│   │   └── 23.zip                      # 左侧一小时缓冲
│   ├── 20251112/
│   │   ├── 0.zip
│   │   ├── 1.zip
│   │   └── ...                         # 当天其他小时
│   ├── 20251113/
│   │   ├── 0.zip
│   │   └── ...
│   └── 20251114/
│       ├── 0.zip                       # 右侧一小时缓冲
│       └── 1.zip                       # 小时边界探测，实际相交时读取
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

路径和覆盖范围按以下规则填写：

- `FILLS_ROOT`、`DIFFS_ROOT` 指向**日期目录的上一级**，例如 `/data/hyperliquid/fills`，不能只填 `/data/hyperliquid/fills/20251112`。
- `SNAPSHOTS_ROOT` 下递归寻找原始 `.rmp` 及其压缩文件；程序从快照本身读取真实高度、时间、币种和精度。
- fills 需要覆盖正式日期及前后各一小时缓冲。程序还会探测下一小时首块，处理 `local_time` 分区与链上时间的边界差异。
- diff 必须覆盖**选中的前快照后第一块至 episode 目标块**，包含目标块。前快照早于正式筛选日期时，也要提供从该快照开始所需的更早 diff。
- 三个输入目录需要可遍历、可读取。输出和缓存应放在输入目录之外，并选择当前用户有写入权限的位置。

### 文件格式和命名

fills/diff 支持 `根目录/YYYYMMDD/H` 和 `根目录/hourly/YYYYMMDD/H`；日期也可写作 `YYYY-MM-DD`，小时可为 `0`–`23` 或 `00`–`23`。

小时文件支持无扩展名、`.jsonl`、`.zip`、`.jsonl.zip`、`.jsonl.gz`、`.gz`、`.bz2`、`.xz`、`.zst`、`.lz4`。每个 ZIP 只允许一个非目录成员，例如 `hourly/20251112/0`。

解压后的内容是 UTF-8 JSONL，**一行一个完整 block**：

```json
{"block_number":793700001,"block_time":"2025-11-12T01:27:40.700Z","events":[]}
```

fills 的事件使用原始 node `[owner_address, fill_object]` 格式；diff 使用原始逐订单 `raw_book_diff` 格式。空 `events` 的区块也要保留，原回放逻辑会检查区块连续性。

**同一小时只保留一种文件编码；同一高度/时间的快照也只保留一份。** 例如不要在同一输入范围同时放 `0.zip` 和 `00.jsonl`，也不要同时保留同一个快照的 `.rmp` 与 `.rmp.zip`。歧义会被明确报告，程序不会任意挑选一份。

## 3. 只修改一个配置文件：`run_hlp.py`

在源码目录、已激活虚拟环境的终端中执行：

```bash
nano run_hlp.py
```

找到文件顶部 `USER CONFIGURATION / 只修改本区`，修改以下七项。示例与源码默认值相同；把目录替换成自己 Ubuntu 上的数据和可写输出位置：

```python
START_DATE = '2025-11-12'
END_DATE = '2025-11-13'

FILLS_ROOT = '/data/hyperliquid/fills'
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'
CACHE_ROOT = '/data/hlp_la/cache'
```

| 配置项 | 填写内容 |
| --- | --- |
| `START_DATE` | 正式开始日期，`YYYY-MM-DD`，包含当天 |
| `END_DATE` | 正式结束日期，`YYYY-MM-DD`，包含当天，不能早于开始日期 |
| `FILLS_ROOT` | node fills 的日期目录上一级 |
| `DIFFS_ROOT` | raw book diff 的日期目录上一级 |
| `SNAPSHOTS_ROOT` | 原始 ABCI 快照根目录 |
| `OUTPUT_ROOT` | 输出根目录；每次自动新建独立运行目录 |
| `CACHE_ROOT` | 持久化缓存根目录；重跑时复用，不能与输出根目录完全相同 |

**日期按 UTC 解释，首尾当天都包含。** 上例正式范围是 `2025-11-12 00:00:00.000` 至 `2025-11-13 23:59:59.999 UTC`；程序自动增加两侧一小时 fills 缓冲，无须自己改日期。开始、结束日期相同表示筛选一整天。

使用 Ubuntu 的实际绝对路径，不要填写 `E:\...` 或 `D:\...`。WSL 才可能使用 `/mnt/e/...`、`/mnt/d/...`。路径含空格或中文可以，Python 中保留引号，shell 命令中用双引号包住路径。

顶部其他配置通常保持默认：

```python
DETAILED_LOG = True
CONSOLE_LOG = True
LOG_BLOCK_INTERVAL = 1000
PREPARE_COIN_DIFFS = True
```

- `DETAILED_LOG`：记录逐笔接管、模拟成交、回放进度及缓存统计。
- `CONSOLE_LOG`：是否同时输出到终端；即使为 `False`，仍会写 `run.log`。
- `LOG_BLOCK_INTERVAL`：每多少块报告一次回放进度，另有约十秒一次的心跳。
- `PREPARE_COIN_DIFFS`：自动准备本次所需币种的 diff 输入并复用缓存。
- `HLP_ACCOUNTS`：保留现有四个默认接收方账户即可；研究口径改变时才调整，程序不会联网更新名单。

nano 中按 **Ctrl+O → 回车** 保存，再按 **Ctrl+X** 退出。**不需要填写分片清单、快照高度/时间、asset、精度或额外 JSON 配置。**

## 4. 正式运行：一个入口

```bash
python run_hlp.py
```

入口自动完成：定位 fills → 接管分组 → 写 csv1 初稿 → 按 episode 选择最近前快照 → 汇总所需币种并提取 RMP → 定位及准备 diff → 完整回放目标 block → 模拟平仓和 LA → 写 csv2/csv3 → 回填 csv1 最终状态。

不需要单独执行提取或 diff 准备脚本，也不需要旧的 `scripts/0_convert.py`、`prepare_full_run.py`、`finalize_full_run.py` 或历史工程清单。

| 文件 | 作用 | 正常运行时是否手动执行 |
| --- | --- | --- |
| `run_hlp.py` | 顶部配置和完整筛选入口 | **是，唯一正式启动脚本** |
| `input_discovery.py` | 小时分片、快照元数据和所需输入定位 | 否，自动调用 |
| `rmp_l4.py` | 按元数据提取所需币种的双侧逐订单 JSON | 否，自动调用 |
| `prepare_local_diffs.py` | 所需币种 diff 输入准备及缓存 | 否，自动调用 |
| `hlp_la_screen.py` | episode、原订单簿回放、LA 和 CSV 输出 | 否，入口调用 |
| `extract_rmp_l4.py` | 独立提取快照的可选工具 | 否，仅独立核对时使用 |
| `test_order_book_replay.py` | 原测试、模拟案例与精确比较入口 | 否，仅测试/核对时使用 |

新开终端或重新登录后，先进入自己的源码目录并重新激活环境，再运行：

```bash
cd /你的源码目录/hlp-la-screen
source .venv/bin/activate
python run_hlp.py
```

如果希望断开 SSH 后继续运行，在已激活环境的源码目录执行：

```bash
nohup python -u run_hlp.py > launcher.log 2>&1 &
tail -f launcher.log
```

## 5. 查看输出和详细日志

启动后会立刻打印本次运行目录和日志完整路径，例如：

```text
Run directory: /data/hlp_la/output/run_2025-11-12_2025-11-13_<UTC时间>_<随机后缀>
RUN.LOG: /data/hlp_la/output/run_.../result/run.log
```

每次运行的目录结构如下：

```text
OUTPUT_ROOT/
└── run_<开始日期>_<结束日期>_<UTC时间>_<后缀>/
    ├── result/
    │   ├── csv1.csv                # 每笔接管一行，附 episode 汇总及最终状态
    │   ├── csv2.csv                # 每 episode 的块末平仓侧逐订单簿
    │   ├── csv3.csv                # 模拟成交轨迹、平仓均价和 LA
    │   └── run.log                 # 全过程明细、进度、缓存命中和异常
    ├── user_config.json            # 本次配置副本
    ├── resolved_config.json        # 程序自动生成的输入清单，不需修改
    ├── fills_discovery.json        # fills 文件定位和缺失记录
    ├── book_discovery.json         # episode、快照、diff 对应信息
    ├── source/                     # 本次源码副本
    ├── source_sha256.json          # 本次复制源码的指纹
    ├── run_summary.json            # 所有 episode 处理完毕后的汇总
    └── process_exit.json           # COMPLETED 或 FAILED
```

把下面示例替换成启动时打印的 **实际 `RUN.LOG` 完整路径**：

```bash
tail -n 50 -f "/data/hlp_la/output/run_.../result/run.log"
```

`launcher.log` 是后台启动时的终端输出；完整业务明细始终以本次 `result/run.log` 为准。长步骤会持续记录进度或心跳。

检查 `run_summary.json` 的 `episodes`、`fills`、`calculated`、`skipped`、`depth_insufficient` 和 `partial_episodes`。`calculated + skipped = episodes`；深度不足可以正常计算，`COMPLETED` 也可能包含跳过或零接管，需结合数量和原因判断。

每次都会新建输出目录；已完成的提取和 diff 输入缓存可以跨运行复用。进程内回放缓存只在本次运行内继续向前推进。中断后重新运行即可，不要手工追加旧 CSV。

## 6. 可选测试：没有真实数据也能先验证安装

在源码目录和已激活的虚拟环境中执行：

```bash
python -m unittest discover -s . -p 'test_*.py'
```

查看完整合成案例及四个核心输出：

```bash
python test_order_book_replay.py --demo --demo-output /tmp/hlp_demo
```

独立真实快照提取和原精确比较入口的参数说明：

```bash
python extract_rmp_l4.py --help
python test_order_book_replay.py --help
```

现有测试及真实数据校验范围见 [`validation/VALIDATION.txt`](validation/VALIDATION.txt)。测试、独立提取、比较均为可选操作，不是正式筛选的前置脚本。

## 7. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| `venv` / `ensurepip` 不可用 | 安装 `python3-venv` 后重新创建 `.venv` |
| 缺少 `msgpack` 或 `orjson` | 确认已 `source .venv/bin/activate`，再运行 `python -m pip install -r requirements.txt` |
| `directory does not exist` / `Permission denied` | 核对顶部路径、挂载位置及目录读写权限；输出和缓存放到有写入权限的位置 |
| 没有匹配接管 | 检查正式 UTC 日期、`FILLS_ROOT` 是否指向日期上一级、真实数据是否到位及 `HLP_ACCOUNTS` 口径 |
| `FILLS_GAP` | 查看具体缺失文件；缺口强制分段，两侧已知 episode 保留，不跨缺口合并 |
| 缺少前快照或 diff，episode 为 `skipped` | 补齐该目标前快照及其到目标块的完整 diff；程序不会用未来快照补救 |
| `AMBIGUOUS_SNAPSHOT` / 同小时多个候选 | 同一快照、同一小时保留一种编码，或将数据根目录指向整理后的唯一副本 |
| `partial_depth` / `depth_insufficient` | 历史簿深度不足以全部模拟成交；结合 `filled_qty`、`unfilled_qty` 和轨迹查看结果 |

## 8. 获取后续代码更新

没有未提交的本地修改时，在源码目录执行：

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`run_hlp.py` 的顶部配置是本地修改。**如果修改过该文件，先保存自己的日期和路径，再更新代码**。只有这份配置有未提交修改时，可按以下顺序暂存、拉取并恢复：

```bash
cp run_hlp.py ../run_hlp.local.backup.py &&
git stash push -m "local Ubuntu configuration" -- run_hlp.py &&
git pull --ff-only &&
git stash pop
```

如果拉取失败，先排查失败原因，不要继续执行恢复步骤；如果恢复时提示冲突，依据仓库外备份合并配置，再运行程序。不要使用 `git reset --hard` 覆盖自己的配置。其他未提交修改需先检查、另行保存。

助手完成本项目的代码更新和相应验证后，会提交并推送到 `origin/main`，约定见 [`AGENTS.md`](AGENTS.md)。公开仓库允许同学直接阅读和 clone；推送仍需拥有写权限的账户认证。

完整格式约定、原计算规则和独立比较示例见 [`README.txt`](README.txt)。
