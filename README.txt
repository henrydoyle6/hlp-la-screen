Ubuntu 三阶段 CSV 运行指南（当前版本）
====================================

完整安装、目录布局、配置、恢复和运行步骤请看同目录 README.md，或仓库首页：
https://github.com/henrydoyle6/hlp-la-screen#readme

1. 安装 Python 3.10+，在源码目录创建并激活 .venv，安装 requirements.txt。
2. 只修改 run_hlp.py 顶部：已准备数据的 START_DATE / END_DATE、三个输入路径、
   输出路径、缓存路径。日期均包含当天，按 UTC 解释。
3. 正式开始 = 数据开始 + 1小时；正式结束边界（不包含）= 数据结束边界 - 1小时。
   例如数据 2025-11-12 至 2025-11-13：正式范围 [12日01:00, 13日23:00) UTC。
   不再向外索取额外一小时的 fills/diff。
4. 保持 RUN_STAGE='all'、RESUME=True，执行 python run_hlp.py。
   csv1 读取 fills 并分组；csv2 只从 csv1 获取 episode，调用原簿引擎；
   csv3 只读 csv1/csv2，不再读取 fills、ABCI 或 diff。
5. 中断后保留整个 OUTPUT_ROOT 和本轮目录（含隐藏 .state），相同配置重新执行即可。
   csv1 按小时分片提交进度，csv2/csv3 按 episode 提交。未提交尾部可回滚；
   已提交内容损坏会报错。正在处理但未提交的 ZIP 小时可以重读。
6. 如需单阶段，把 RUN_STAGE 改为 csv1、csv2 或 csv3，仍运行同一个脚本；
   阶段切换保持 RESUME=True，且前置 CSV 必须完成。
7. RESUME=False 用于主动新建一轮。更换算法版本或补齐数据后需重算时使用；
   新建后要继续该轮，改回 True。

状态：csv1 grouped 为稳定分组结果；csv2 ok/skipped 为还原状态；csv3 给出最终
ok、empty_book、partial_depth 或 skipped。后续阶段不反写 csv1。

RMP 读取模块、原回放/LA/精确比较实现保持不变；新增的 csv_pipeline.py 负责
CSV 依赖和持久化断点。正常使用无需单独运行提取或比较脚本。

可选测试：python -m unittest discover -s . -p 'test_*.py'
验收记录：validation/CSV_PIPELINE_ACCEPTANCE.txt
