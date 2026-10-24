# v0.2.0 保护边界迁移

用户 2026-10-25 明确授权根据 vNext 修改现有项目：原生 RMP 共享入口、SQLite 缓存/进度、单侧 CSV2、局部错误、唯一新入口及比较适配。因此旧版“整个读取/测试模块逐字节不变”与本轮接口变更冲突。

保护没有取消：`tools/protected_engine.json` 从实际基线 `e698b1ce70be366e82e83f1fc6d2b9bbaddca748` 提取并核对 31 个函数/类源片段，CI 逐个验证 SHA-256。保护内容包括 OrderBook（含逐单 new/update/remove/zero）、snapshot 选择与 JSON L4 适配、dense replay、接管识别、simulate_close、RMP market_info/整数缩放/remaining 映射、活跃订单和精确比较等。

这些源片段与基线相同；原有 bounded RMP header helper 仅从 rmp_header 重命名为 _rmp_header，外层统一将解析异常转为可局部处理的输入错误。LimitedReader 保持不变。没有按结果更新数量/精度，没有尝试 S±1，没有容差。

迁移允许改变的是：旧转换器/旧筛选 CLI 的移除、共享读取入口、RMP 暂存与缓存位置、发现/路由、CSV 编排与 SQLite 恢复，以及明确授权的正式右端/输出状态。已有 full-file hash 审计保留在历史 validation 中，但不冒充本版未修改模块的证明。

测试同步保留原 60 项适用回归，原假转换器用例换成真实共享原生适配；生产稀疏覆盖的旧期望改为 dense 失败；外侧恰好30分钟按新规格判 partial。原精确答案错误必须失败、目标之后坏行不影响已完成 B 等断言仍在。新测试覆盖阶段隔离、局部错误、三子阶段、长字段/半行、缓存孤儿与零量恢复、六文件与双进程锁。

最终真实数据/安装验收由本次实际运行记录报告；旧 validation 的 PASS 不能替代。本保护清单只证明被列出核心片段未改，不声称整个新编排已因哈希而自动正确。
