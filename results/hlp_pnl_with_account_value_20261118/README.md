# HLP 盈亏改为直接加 accountValue

2026-11-18按用户新口径修改两份CSV。新版已删除M及全部旧的含M计算列，保留U、accountValue和原始价格证据。历史文件作为旧版本留存，不参与新版公式。

## 新公式

`HLP盈亏_加AccountValue_USDC = FIFO仓位盈亏_USDC + accountValue`

HLP仓位盈亏使用此前核验的FIFO价差盈亏，**不使用旧的“FIFO+M”结果作为底数**。多空方向已在FIFO盈亏中体现，不再整体乘负号；accountValue保留原正负，正结果为盈利、负结果为亏损。仍不计手续费和资金费。

每笔按原接管事件编号关联，episode不重新分组。一个episode的结果等于本组所有逐笔新盈亏之和，也严格等于本组FIFO合计加accountValue逐笔合计。222笔仅抵消旧仓的接管仍使用原FIFO归属值0，再加本笔accountValue，不重复加已归入旧批次的退出解释金额。

## 结果

|层级|记录数|盈利|亏损|持平|缺新公式输入|
|---|---:|---:|---:|---:|---:|
|接管逐笔|22,044|18,116|3,928|0|0|
|episode|443|408|35|0|0|

FIFO仓位盈亏合计：73,020,695.700172 USDC。

accountValue逐笔合计：138,885,739.0756509998 USDC。

新公式盈亏合计：**211,906,434.7758229998 USDC**。逐笔与episode汇总严格一致。

## 原5笔缺价记录仍明确标注

|episode|币种|缺失价格字段|新公式状态|
|---|---|---|---|
|EP000009|TRUMP|清算标记价|已计算，原始价格仍缺失|
|EP000013|kPEPE|清算标记价|已计算，原始价格仍缺失|
|EP000019|OM|清算标记价|已计算，原始价格仍缺失|
|EP000151|HYPE|被接管用户平均开仓价|已计算，原始价格仍缺失|
|EP000162|SPX|被接管用户平均开仓价|已计算，原始价格仍缺失|

这些是原来无法计算U/M的5笔，并非FIFO价差或accountValue缺失。此次没有补出其价格；源价格列和U列中的空值原样保留。因为新公式只依赖已齐全的FIFO盈亏和accountValue，5笔及所属episode都能计算。

逐笔增加“历史价格数据缺失”“缺失价格字段”“历史价格缺失说明”“缺失对本次盈亏影响”，原始完整原因保留。episode增加历史缺价接管笔数、事件编号和缺失字段说明。可筛选“历史价格数据缺失”以找到这5笔或5个组；不能将“已计算”解读为原始价格已补齐。

## 文件

- `csv1_22044笔_HLP盈亏加AccountValue.csv`：73列，22,044笔接管加4条原有非接管诊断。旧71列中删除5个M相关字段，其余66列逐格保留，再增加7列新结果及缺价标记。
- `443个episode_HLP盈亏加AccountValue.csv`：19列、443行，保持原episode编号和顺序，删除旧M相关指标及旧缺M部分小计，写入新的accountValue合计、盈亏方向和历史缺价标记。

UTF-8 BOM、CRLF；不统一舍入金额，不将空值改成0。4条诊断不计入接管笔数和盈亏。

## 计算范围与核验

依用户口径直接累加每行accountValue，**未按清算包去重或重新分配**。已知同一清算包的accountValue可能在多笔接管中重复，因此这里是指定逐笔公式的结果，不据此认定HLP去重后的实际净收益。此前FIFO与官方PnL尚未完全对齐的状态仍在逐笔文件中保留。

独立Fraction逐笔复算22,044次相加和443次聚合；核验所有保留单元格、币种/方向/账户/事件对应、全部5个缺价标记，以及新表无M相关列。所有新增单元格经过表格引擎精确文本往返校验。输入/输出SHA256和统计记录在validation.json；合成边界测试与全量已交付CSV回读分别保留。没有重新抓取数据或使用AWS。

仓库根目录复核：

```sh
python pnl_with_account_value.py --verify --source results/hlp_pnl_with_m_20261116/csv1_22044笔_含U_M及HLP盈亏.csv --old-episodes results/hlp_pnl_with_m_20261116/443个episode_含M事件盈亏.csv --fifo results/hlp_fifo_pnl_20261115/takeover_pnl.csv --output results/hlp_pnl_with_account_value_20261118
python -m unittest test_pnl_with_account_value test_hlp_account_value_pnl_result -v
```
