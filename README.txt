Ubuntu v0.3.1 使用步骤（完整命令和配置见 README.md）：
1. 真实 RMP 运行使用 Python 3.12。以 Ubuntu 24.04 为例安装 python3.12、python3.12-venv、git，clone 明确标签 v0.3.1。
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
   数据库崩溃 journal 先让 SQLite 恢复，不手动删除。v0.3.0 及更早旧断点不能混续；可导入它的 CSV1。
9. 支持 context/locus 元数据及 book_orders/halfs-arena 订单存储；真实 OID、剩余量和原算法保持不变。
   Python 3.10 无法解析真实纳秒时间；先在激活后的 venv 用 python --version 确认是 3.12。
10. CSV2 只含平仓侧；CSV3 仅依赖前两个 CSV。真实比较用同一原生读取器，精确比较无容差。
11. 本版验收见 validation/ARENA_ACCEPTANCE.md。功能分支 -> PR -> 验收 -> main -> 标签发布。
