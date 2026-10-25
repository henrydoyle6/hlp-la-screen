Ubuntu v0.3.0 使用步骤（完整命令和配置见 README.md）：
1. 安装 Python 3.10+、python3-venv、git；clone 明确发布标签 v0.3.0。
2. 在源码目录创建并激活 .venv，pip install -r requirements.txt。
3. 只改 run_hlp.py 顶部：START_DATE、END_DATE、三个数据根目录、OUTPUT_ROOT、CSV1_INPUT。
4. 日期是已准备好的 UTC 首尾整天；正式范围两端包含，两端各留一小时内部缓冲。
5. 从 fills 生成：CSV1_INPUT 留空；python run_hlp.py。all 严格完成 CSV1，再 CSV2，再 CSV3。
6. 已有完整 hlp-csv-v3 CSV1：CSV1_INPUT 填绝对路径，日期与源 CSV1 一致，RUN_STAGE=all，
   RESUME=False 开新轮；仍只运行 python run_hlp.py。不读 fills、不重新分组、不需要旧数据库。
   必须提供整份可信、完成的 CSV1，不经 Excel 改写；完整 diff/快照仍须覆盖所需目标。
7. 同输入中断 True；补数据、换输入或重算 False 新轮；阶段切换保持 True。
   导入完成后无需外部 CSV1，但配置保留原 CSV1_INPUT 路径。csv3 要求本轮 CSV1/CSV2 已完成。
8. 每轮保存 csv1.csv、csv2.csv、csv3.csv、run.log、progress.sqlite、cache.sqlite。
   数据库崩溃 journal 先让 SQLite 恢复，不手动删除。v0.2 旧断点不能混续；可导入它的 CSV1。
9. 支持旧 context 和新 locus 快照；首次定位新 ctx 可能需流式读约 406 MB，详见 run.log。
10. CSV2 只含平仓侧；CSV3 仅依赖前两个 CSV。真实比较用同一原生读取器，精确比较无容差。
11. 验收见 validation/RMP_CSV1_ACCEPTANCE.md。功能分支 -> PR -> 验收 -> main -> 标签发布。
