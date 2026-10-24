Ubuntu 使用步骤（完整命令见 README.md）：
1. 安装 Python 3.10+、python3-venv、git；clone 明确发布标签 v0.2.0。
2. 在源码目录创建并激活 .venv，pip install -r requirements.txt。
3. 只改 run_hlp.py 顶部：START_DATE、END_DATE、三个数据根目录和 OUTPUT_ROOT。
4. 日期是已准备好的 UTC 首尾整天；正式范围两端包含，两端各留一小时内部缓冲。
5. python run_hlp.py。默认 all 严格完成 CSV1，再 CSV2，再 CSV3。
6. 同输入中断 True；补数据或重算 False 新轮；阶段切换保持 True。
7. 每轮保存 csv1.csv、csv2.csv、csv3.csv、run.log、progress.sqlite、cache.sqlite。
   数据库崩溃 journal 先让 SQLite 恢复，不手动删除。旧 .state 不能强行混续。
8. 运行中看 run.log；输入异常是局部记录，不预检全量，也不伪造未知接管。
9. CSV2 只含平仓侧；CSV3 仅依赖前两个 CSV。真实比较用同一原生读取器，精确比较无容差。
10. 后续维护：功能分支 -> PR -> 测试验收 -> main -> 标签发布 -> Ubuntu 指定版本。
