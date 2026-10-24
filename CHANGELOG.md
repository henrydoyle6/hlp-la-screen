# 更新记录

## 0.2.0

- 按 vNext 规范迁移为六文件 SQLite 进度/缓存与严格三阶段输出。
- 正式 UTC 筛选区间两端包含；CSV2 单侧 book_ready/skipped；CSV3 calculated_full/calculated_partial/empty_book/skipped。
- 逐单 diff、精度与 LA 公式保持原样；所有 RMP 路径使用共享原生读取器。
- 输入异常局部进入相应 CSV 诊断行，fills 缺口只分段；仅从准备日期读取 diff。
- 旧 .state 断点不兼容，升级使用 RESUME=False 新建一轮；不删除旧资料。
- 数学保护基线迁移说明见 docs/PROTECTION_MIGRATION.md。

## 0.1.0

首个带标签的可复现发布，基于已经验收的三阶段筛选器。

- CSV1 → CSV2 → CSV3 顺序执行与独立断点；CSV3 只读取前两个 CSV。
- 已准备 UTC 日期范围内部两端各保留一小时；保留按需 RMP 提取及双层缓存。
- 保留原 RMP、diff 回放、LA 和精确比较实现。本次建立发布流程，未修改计算代码。
- 功能分支和 PR、Ubuntu Python 3.10/3.12 自动测试、阶段验收、main 保护和标签发布。
- Ubuntu 按发布标签安装；源码包附 SHA256SUMS 和发布提交信息。

验证基线：151 项自动测试（原生产测试 142 项，加发布门禁 9 项）；31 项三阶段关键验收包含在完整测试中。已有真实数据验收的范围与限制见
`validation/CSV_PIPELINE_ACCEPTANCE.txt`，CI 不声称重新读取或比较全部真实市场。
