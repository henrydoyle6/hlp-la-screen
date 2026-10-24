Ubuntu 通用版：只改顶部配置，然后运行一个入口
================================================

一、首次部署：按顺序执行

1. 把 hlp_la_universal_ubuntu.zip 传到 Ubuntu，放在任意工作目录，然后：

   unzip hlp_la_universal_ubuntu.zip
   cd hlp_la_universal_ubuntu
   python3 --version
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt

   也提供 tar.gz 包，可用 tar -xzf hlp_la_universal_ubuntu.tar.gz 替代 unzip。
   两个压缩包内容相同，选择一个解压即可。若 unzip 未安装可用 tar.gz。
   需要 Python 3.10 或更新版本。已在 Ubuntu（WSL2）/ Python 3.12.3 验证。
   如果提示没有 venv 模块，先安装后再执行上面创建虚拟环境的命令：

   sudo apt update
   sudo apt install python3-venv

   msgpack 用于读取 ABCI 二进制快照，orjson 用于加快大量 diff 的解析。
   requirements.txt 固定本轮验证的 msgpack 1.2.3 / orjson 3.13.0，便于复现。
   普通 .zip / .rmp / .jsonl / .gz / .bz2 / .xz 无须安装 hl-node。
   若输入使用 .zst 或 .lz4，再安装对应的可选依赖：

   python -m pip install zstandard lz4

2. 只编辑 run_hlp.py 顶部 USER CONFIGURATION 区域：

   nano run_hlp.py

   nano 中修改后按 Ctrl+O、回车保存，再按 Ctrl+X 退出。

   以下是 Ubuntu 配置例子，请替换成你机器上的实际绝对路径：

   START_DATE = '2025-11-12'
   END_DATE = '2025-11-13'
   FILLS_ROOT = '/data/fills_250710'
   DIFFS_ROOT = '/data/hyper liquid/node_raw_book_diffs_by_block'
   SNAPSHOTS_ROOT = '/data/hyper liquid/periodic_abci_states'
   OUTPUT_ROOT = '/data/hlp_la/output'
   CACHE_ROOT = '/data/hlp_la/cache'

   日期必须是 YYYY-MM-DD。开始、结束日期都包含当天，按 UTC 解释。
   上例正式筛选范围是 11月12日 00:00:00.000 至 11月13日 23:59:59.999 UTC。
   对应北京时间是 11月12日08:00 至 11月14日08:00之前。
   程序自动加上前后各一小时 fills 缓冲，不需要你手动提前/延后日期。
   START_DATE 和 END_DATE 相同表示筛选一整天。

   必须使用 Ubuntu 的路径，例如 /data/...；不要使用 E:\... 或 D:\...。
   WSL 环境才使用 /mnt/e/...、/mnt/d/...，普通 Ubuntu 不要求这些挂载名。
   路径包含空格、中文可以；Python 字符串保留单引号，shell 参数用双引号包住。
   三个原始数据目录必须已经存在且可遍历/读取；输出和缓存目录由程序创建，需要写入权限。
   快照子目录无法列出会明确停止，避免悄悄漏掉最近前快照而改选旧快照。
   输出、缓存目录放在原始数据目录之外，防止生成文件混入数据发现过程。

   HLP_ACCOUNTS 是识别 backstop 接收方的账户列表，保留原来四个默认账户。
   如研究口径改变，在同一个顶部区域修改。程序不会联网更新账户名单。
   默认仅统计：账户在名单内、liquidation.method 为 backstop，且接收方不是
   liquidatedUser 的 fills。未命中这些规则的普通成交不计为 HLP 接管。

   DETAILED_LOG = True       保留详细日志，建议保持 True。
   CONSOLE_LOG = True        同步显示到终端；False 仍然完整写 run.log。
   LOG_BLOCK_INTERVAL = 1000 每回放 1000 块报告一次进度，另有 10 秒心跳。
   PREPARE_COIN_DIFFS = True 自动生成所需币种的 diff 输入并缓存，建议保持 True。

3. 正式运行只需要一个命令：

   python run_hlp.py

   不需要先执行 extract_rmp_l4.py，不需要运行旧版 scripts/0_convert.py 等脚本，
   不需要手动解压原始 ZIP，也不需要填写 chunks、快照 block/time 清单、asset 或精度。
   不需要 prepare_full_run.py、finalize_full_run.py 或上次工程的账户 CSV/审计文件。
   入口会自动完成配置转换、文件定位、分组、提取、回放、LA、输出和汇总。

   启动后立刻打印：
   Run directory: /data/hlp_la/output/run_2025-11-12_2025-11-13_<UTC时间>_<随机后缀>
   RUN.LOG: /data/hlp_la/output/run_.../result/run.log

   新终端查看进度，把路径替换成上面实际打印的路径：

   tail -n 50 -f "/data/hlp_la/output/run_.../result/run.log"

   如需关闭 SSH 后仍在后台运行，可以在已激活虚拟环境后使用：

   nohup python -u run_hlp.py > launcher.log 2>&1 &
   tail -f launcher.log

   launcher.log 只是入口终端输出；完整明细仍在每次运行自己的 result/run.log。
   下次新开终端/重新登录时，先 cd 到源码包目录，再 source .venv/bin/activate，
   然后执行 python run_hlp.py。每次都创建新目录，不覆盖历史 CSV。
   持久化缓存会复用；中途停止后也应重新运行，不要手工把旧 CSV 接着追加。

二、输入目录应该长什么样

   /data/fills_250710/
     20251111/23.zip
     20251112/0.zip ... 23.zip
     20251113/0.zip ... 23.zip
     20251114/0.zip
     20251114/1.zip

   /data/hyper liquid/node_raw_book_diffs_by_block/
     20251112/0.zip ... 23.zip
     20251113/0.zip ... 23.zip

   /data/hyper liquid/periodic_abci_states/
     20251112/793650000.rmp.zip
     20251112/793700000.rmp.zip

   FILLS_ROOT 指向 YYYYMMDD 的上一级，不是指向某一天或整个包含多类数据的总目录。
   DIFFS_ROOT 同理。快照根目录会递归发现 .rmp 及支持的 .rmp 压缩文件。
   原始快照可以保持 ZIP，无需另存同一份解压 .rmp。

   fills/diff 的小时文件也支持：根目录/hourly/YYYYMMDD/H；日期 YYYY-MM-DD；
   小时 0..23 或 00..23；无扩展名、.jsonl、.zip、.jsonl.zip、.gz、.jsonl.gz、
   .bz2、.xz、.zst、.lz4。请在每个日期小时只保留一种文件编码。
   同时出现 0.zip 和 00.jsonl 等多个候选时，不会任意挑选：fills 作为缺口分段，
   diff 使依赖它的 episode 跳过，并写明所有候选路径。请整理数据根目录后再跑。
   同一高度/时间的 .rmp 和 .rmp.zip 都在快照根目录时，选中它的 episode 会报告
   AMBIGUOUS_SNAPSHOT；把快照根目录指向只含一份的整理目录即可。

   每个 fills/diff ZIP 必须只有一个非目录成员。成员路径可以是 hourly/YYYYMMDD/H。
   内容必须是 UTF-8 JSONL，一行一个完整 block：

   {"block_number":793700001,"block_time":"2025-11-12T01:27:40.700Z","events":[...]}

   fills 的 events 使用 node 原始 [owner_address, fill_object] 格式；
   diff 的 events 使用原始逐订单 raw_book_diff 格式。
   空 events 的 block 也必须保留，dense 回放逐块校验连续性。
   小时名是文件分区，实际链上时间以记录 block_time/fill.time 为准；程序探测
   下一小时的首块，避免 local_time 分区边界前几毫秒的成交被漏掉。
   正常目录按小时连续递增；第一块时间/高度倒序会明确报错，不静默重排。

   必须提供：正式范围及两端缓冲所需 fills；每个选中前快照到目标块的全部 diff；
   每个 episode Tn 前合规的最近快照。前快照可能早于筛选开始很多天，diff 也必须
   从该快照后第一块开始，不能只准备正式筛选日期当天的 diff。
   程序按 episode 实际需求确定这段范围，不要求未来快照，也不会下载缺失数据。

三、自动执行顺序和保留规则

   1. 自动读取时间范围的 fills。每个 coin + HLP 接管方向独立分组，相邻间隔
      <=30 分钟属于同一 episode；遇 fills 缺口强制分段，两侧已知 episode
      正常保留，禁止跨缺口合并。
   2. 正式范围至少命中一笔接管就保留整个已读取 episode，包括缓冲区部分。
      外侧边界不足以确认完整时仍计算，并标记 partial_episode。
   3. 写 csv1 初稿，按 episode 去重，确定 coin、Tn、目标 block B。
   4. 读取 ABCI 小段 context 元数据，得到真实完成高度和时间；按原 select_snapshot
      规则选择 time < Tn 且 snapshot_block < B 的最近前快照。文件名不用于替代
      实际元数据。context 探测最多 1 MiB，不是完整订单簿扫描；头信息成功后缓存。
      选中快照完整提取时还会验证 context 和整个文件完整性。
      若可能是最近前快照的文件损坏、无法识别元数据，会明确跳过相关 episode，
      不静默退回旧快照。损坏文件的数字高度名仅用于保守阻断，不用于成功选择；
      无法判断其位置时会保守阻断，需先排查日志中的该文件。
   5. 按选中的原快照汇总所需币种。每份所需 RMP 本次只完整扫描一次，直接按自身
      perp/HIP3/spot 元数据识别 asset 和精度；只导出所需币种的双侧逐订单 JSON。
      真 oid、方向、价格、剩余量均保留；数值是十进制字符串；同价订单不合并。
      单币种 JSON 不加入原始快照候选列表，不会选错币种。
   6. 自动发现从快照到各目标所需 diff 小时文件。分片 block 范围是路由边界，
      不代表数据已通过完整性检查；原 iter_diff_blocks + replay_blocks 仍逐块验证。
      可先按所需币种投影 diff 以减小解析量；不造空块、不修正数值、不改变事件顺序。
      发现异常则交回原始读取器按实际目标处理，不把未来错误扩散到更早的成功目标。
   7. 目标 block 递增处理，完整应用目标 B 内全部 diff 后取块末订单簿。缺 diff
      跳过依赖它的 episode，不用未来快照补救。long 模拟卖出吃 bid，short 买入吃 ask。
      模拟成交不修改历史订单簿。无深度或不足深度按原 LA 口径输出。
   8. csv2/csv3 写出并刷新后清空 ep.book_result、ep.la_result；保留尚有后续使用者
      的 manager 回放缓存。结束时原子回填 csv1 最终状态，并写汇总。

   保留三类缓存：
   - headers：源文件身份、版本对应的首块/context 元数据；命中时不重复解析头。
   - rmp_l4：源快照身份 + 转换版本 + coin 对应的完整成功 JSON，包含 SHA256 校验。
   - diff_inputs：源 diff 身份 + 币种/范围 + 投影版本对应的完整成功 JSONL。
   回放缓存另外在本次进程内按 coin + 原快照保存成功高度和簿，继续向前回放；
   不作为跨进程断点。最后一个使用者刷新输出后释放。
   失败工作簿不会推进成功缓存，失败提取不写成功收据。
   身份包含路径、大小、mtime/ctime、设备/inode、原始文件首尾指纹。
   原始数据应视为不可变；不同挂载路径/编码/版本可能正常导致缓存未命中。
   新运行先验证成功收据及输出哈希，再复用。无需你维护清单或手动删除半成品。

四、输出和怎么看是否成功

   OUTPUT_ROOT/
     run_<开始日期>_<结束日期>_<UTC时间>_<随机后缀>/
       result/
         csv1.csv                 每笔接管一行，附所属 episode 汇总和最终状态
         csv2.csv                 每 episode 一行，块末可用于平仓那一侧的逐订单簿
         csv3.csv                 每 episode 一行，模拟成交轨迹、平仓均价和 LA
         run.log                  全过程日志
       user_config.json           本次顶部配置的副本
       resolved_config.json       自动生成的实际输入配置，供审计，无须手工填写
       fills_discovery.json       fills 分片定位、首块和缺失信息
       book_discovery.json        episode 与快照对应、diff 定位及异常
       source/                    本次运行源码副本
       source_sha256.json         源码指纹
       run_summary.json           汇总，成功处理完所有 episode 后生成
       process_exit.json          COMPLETED 或 FAILED

   csv2 的 orders_json 是模拟前的完整订单量。csv3 的 fill_trace_json 以同一 oid
   对应 csv2 的订单；最后一笔可能只使用一部分，csv2 中的剩余量不会因此减少。
   csv2 只输出平仓所需一侧；提取缓存 JSON 和 manager 中仍为双侧订单簿。

   run_summary 中 calculated + skipped = episodes。calculated 可含 partial_depth；
   depth_insufficient 是深度不足数量，不是回放失败。skipped 的原因看 CSV 和 run.log。
   COMPLETED / 退出码 0 表示流程完整结束，可能仍有 skipped 或没有匹配 episode；
   不等于每个簿都已与另一份真实 ABCI 快照验收。请检查 skipped、partial_episodes。
   退出码 2 表示配置或致命执行错误，详情看终端及 run.log；Ctrl+C 为 130。
   处理失败不会覆盖上次运行。再次执行会生成新输出目录，复用已完成缓存。

   常见日志事件：
   INPUT_HEADER_PROBE / INPUT_HEADER_CACHE_HIT  自动发现头信息/缓存命中
   INPUT_HOUR_UNAVAILABLE / FILLS_GAP          缺失、歧义或损坏输入
   CSV1_DRAFT_WRITTEN / EPISODE_BOOK_PLAN      初稿完成及 episode 快照计划
   RMP_SCAN_START / RMP_SCAN_DONE             实际完整 RMP 扫描
   EXTRACT_CACHE_HIT                          单币种 JSON 提取缓存命中
   DIFF_INPUT_SOURCE_START / ...PROGRESS      按需 diff 输入准备
   DIFF_INPUT_CACHE_HIT / DIFF_INPUT_USE_ORIGINAL  复用/交回原始读取器
   REPLAY_BLOCK_PROGRESS / BOOK_CACHE         回放进度/回放缓存命中
   SIMULATED_FILL / EPISODE_DONE              模拟成交及 LA
   EPISODE_SKIPPED                            局部失败，仅相关 episode 跳过
   CSV_OUTPUT_FLUSHED / EPISODE_RESULTS_RELEASED  写入和释放临时结果
   CACHE_SUMMARY / RUN_DONE                   扫描次数、命中统计和最终汇总
   HEARTBEAT / PROGRESS                       长步骤仍在运行

五、哪些脚本要执行

   正式筛选：只运行 python run_hlp.py。
   下列文件会由入口自动调用，不需单独运行：
   input_discovery.py       自动发现，生成清单和元数据缓存
   hlp_la_screen.py         原分组、订单簿回放、LA、CSV 和日志主体
   rmp_l4.py                原始 RMP 到按需双侧 L4 JSON 的共用提取后端
   prepare_local_diffs.py   所需币种 diff 投影/缓存，原回放函数消费其输出

   可选：第一次部署后执行全部测试（不需要真实数据，不修改原始数据）：

   python -m unittest discover -s . -p 'test_*.py'

   test_order_book_replay.py 是原比较/测试入口，其比较函数未修改。
   test_ondemand.py 验证提取、缓存与释放；test_local_projection.py 验证输入投影；
   test_universal.py 验证自动定位和同一个正式入口，含冷/热缓存端到端测试。

   可选：查看完整合成案例：

   python test_order_book_replay.py --demo --demo-output /tmp/hlp_demo

   可选：严格核对真实前后快照。先用 extract_rmp_l4.py（和正式筛选共用后端）
   分别按真实元数据提取前/后快照的目标 coin，然后把两个 L4 JSON 路径交给原比较入口：

   python test_order_book_replay.py --compare \
     --before-state /path/to/before_coin.json \
     --after-state /path/to/after_coin.json \
     --before-block 793650000 \
     --before-time '2025-11-12T00:19:22.143Z' \
     --target-block 793700000 \
     --coin SOL \
     --diffs /path/to/needed_diff_0.jsonl /path/to/needed_diff_1.jsonl \
     --diff-mode dense

   python extract_rmp_l4.py --help
   python test_order_book_replay.py --help

   此可选核对是逐 oid、方向、价格、剩余量精确比较，PASS/FAIL，无浮点容差。
   不一致只报告，不会自动修复 diff 或快照，不会自动圆整尾数。
   比较的是订单簿，不是账户、仓位等完整 ABCI state。
   正常筛选不需要执行这两个可选命令，也不需要手动维护这些参数。

六、本轮验证记录

   部署前可查看 validation/VALIDATION.txt 及同目录 JSON/测试日志。
   包内没有原始行情数据；这些记录不属于运行依赖，无须修改。

七、GitHub 获取和后续更新

   公开仓库：https://github.com/henrydoyle6/hlp-la-screen
   同学可以直接查看、下载或克隆；查看代码不需要单独邀请。

   Ubuntu 第一次获取：

   git clone https://github.com/henrydoyle6/hlp-la-screen.git
   cd hlp-la-screen
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   nano run_hlp.py
   python run_hlp.py

   配置日期、数据路径和输出路径的方法仍见第一节，只改 run_hlp.py 顶部。
   之后在工作区没有未提交修改时，可用 git pull --ff-only 获取新版本。
   run_hlp.py 中的本地配置属于工作区修改，更新前先妥善保留，勿覆盖自己的路径设置。

   本项目已记录用户授权：助手完成代码更新和相应验证后，创建描述性 commit，
   并推送至 origin/main。每次推送后核对本地和远端提交号。不会后台监控文件编辑。
   手动更新时，在本仓库内使用 git status、git add、git commit、git push；
   推送需要你自己的 GitHub 认证。公开查看不自动获得推送权限。
   原始 RMP、fills/diff 压缩包、缓存、CSV/JSONL 运行结果及凭据均不提交到仓库。
