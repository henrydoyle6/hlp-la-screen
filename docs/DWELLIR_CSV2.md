# Dwellir ABCI + diff 与新增段 CSV2

`dwellir_csv2.py` 只使用 `periodic_abci_states` 和 `node_raw_book_diffs_by_block`。
它准备已完成 CSV1 的导入副本、验证获取清单和前后原生 RMP，调用原 `run_hlp.py`
的 CSV2 阶段，再核验交付文件。原生 RMP、逐单回放、LA、精确比较和 31 个保护片段未改。

## 当前状态

2026-11-09 已在已登录免费账户的 Archive Data 页面检查这两个数据集。
申请窗口说明后续回复密钥和报价。用户已提交仅询问免费 ABCI/diff 样本的申请，
成功提示已核对；尚未获得真实档案。申请不等于免费权限或购买授权。
公开文档的 diff JSON 示例是合成数据，不能作为真实验收。

- [历史档案访问与报价](https://www.dwellir.com/docs/hyperliquid/historical-data)：按数据集单独报价。
- [原生 ABCI](https://www.dwellir.com/docs/hyperliquid/data-catalog/node-feeds/abci-states)：历史 RMP 档案或 Dedicated Node Cluster gRPC。
- [raw diff](https://www.dwellir.com/docs/hyperliquid/data-catalog/node-feeds/book-diffs)：历史小时档案或相应 gRPC 方法。

控制台显示存储为 `s3.ap-southeast-1.wasabisys.com`，桶为 `hyperliquid-historical-data`。
这是 Wasabi S3 兼容存储，不是 AWS 桶。未尝试凭据或数据请求。
本工具费用上限为 0，只接受 Dwellir 域名的直接 HTTPS 交付，拒绝重定向、S3、AWS、
Wasabi 等其他网络入口；不调用 boto3/STS、不加载 AWS 凭据链。原 AWS 暂停及账户守卫不变。
若未来只能提供 S3，需要另行明确接入范围，不能把 Wasabi 密钥当成指定 AWS 账户凭据。

## 准备固定输入

正式 CLI 锁定修正版 CSV1 SHA-256：
`89bbee26d9ebad3f43d759d8406785bd049062d460365f6aab814e158f4d0022`。
按完整 episode 的 Tn 选择 `[2026-05-28T00:00:00Z,2026-11-01T00:00:00Z)`：
EP000411—EP000443，共 33 组、965 笔接管、22 币种、15 个目标小时。
只映射中文表头；原始成员、组号、指标、哈希、日期元数据及四条来源诊断全部保留。

```bash
python3.12 dwellir_csv2.py prepare --csv1 /data/input/csv1_zh.csv --output /data/dwellir/prepared
python3.12 dwellir_csv2.py access --output /data/dwellir/no_access.json
```

准备目录内为 tasks.json、csv1_selected.csv、csv1_sample.csv；与六文件原生运行目录分开。
同内容可再次准备，已有不同文件拒绝覆盖。准备日期仍沿用原 CSV1，不因选取新段而重写正式/缓冲标记。

有免费权限后运行 `access --evidence /data/private/access.local.json --output ...`。
本地观察记录必须有 registered、email_verified、free_archive_access=true，cost_usd=0，
transport=dwellir_https，prefixes 包含两个指定前缀，observation_reference 指向真实免费访问依据。
这是操作员观察记录，不是提供商签发的权限凭证；实际 HTTP 和校验和仍需成功。
RPC 额度不能代替档案权限。邮箱、API Key、签名 URL、账户截图均留在本地。

## 清单契约与下载

获得真实索引后才创建本地 JSON 清单；不能猜快照高度、大小或校验和。

| 字段 | 定义 |
| --- | --- |
| plan_hash | tasks.json 中绑定输入和任务的哈希 |
| stage | sample 或 full |
| data_kind | provider_archive；合成测试使用 synthetic |
| cost_usd | 必须为 0 |
| objects | 每对象有真实 key、size_bytes、sha256；fetch 还需要 url |
| windows | 每窗口有 id、episode_ids、before_key、diff_keys |
| 样本附加字段 | after_key、真实 after_block |

允许的 key 为 `periodic_abci_states/YYYYMMDD/HEIGHT.rmp.lz4` 和
`node_raw_book_diffs_by_block/YYYYMMDD/H.lz4`。每对象只登记一次，窗口可共享。
覆盖必须恰好为样本六组或全量33组，拒绝重复组、额外对象、未登记依赖和范围外文件。
样本窗口固定为 june=[EP000411]、aug19=[EP000414,EP000415]、
aug22=[EP000418,EP000420]、october=[EP000443]。
样本上限8份ABCI、12个diff小时对象、16 GiB压缩数据。
可先争取单窗口免费样本；最终解锁全量仍需六组真实验收。

```bash
python3.12 dwellir_csv2.py validate-manifest --plan /data/dwellir/prepared/tasks.json --manifest /data/private/sample.local.json
python3.12 dwellir_csv2.py fetch --plan /data/dwellir/prepared/tasks.json --manifest /data/private/sample.local.json --raw-root /data/dwellir/raw --evidence /data/private/access.local.json
python3.12 dwellir_csv2.py verify-sample --plan /data/dwellir/prepared/tasks.json --manifest /data/private/sample.local.json --raw-root /data/dwellir/raw --output /data/dwellir/sample_acceptance.json
```

任何请求前先检查整个清单、免费访问证据及限额。Content-Length、实际字节、SHA-256 均须匹配，
才将 .part 提升为完成对象。中断对象重新获取，校验通过的完整对象复用，已有不同对象拒绝覆盖。
网络错误和终端输出不包含签名 URL 或 API Key。没有隐式 S3 回退。

## 原生回放与导出

前快照时间严格小于 Tn、S严格小于B，生产完整回放 `(S,B]`。原 dense 连续块要求不变，
不能将缺块猜为空 events。样本另行回放 `(S,A]`，原比较器在回放完成后才解码后快照，
逐单精确比较 OID、方向、价格、剩余量。文件完整性校验不代替回放验收。
后快照排除在生产发现目录之外，只有已登记前快照和 diff 经同卷硬链接路由给原生入口。

```bash
python3.12 dwellir_csv2.py run --plan /data/dwellir/prepared/tasks.json --manifest /data/private/sample.local.json --raw-root /data/dwellir/raw --output /data/dwellir/sample_runs
# 全量 fetch/run 使用 full 清单，并追加 --sample-receipt /data/dwellir/sample_acceptance.json
python3.12 dwellir_csv2.py export --plan /data/dwellir/prepared/tasks.json --run-dir /data/dwellir/full_runs/RUN_DIRECTORY --output /data/dwellir/csv2_20260528_20261031.csv
```

样本运行可用于调试；全量获取和运行必须有六组真实样本通过的凭据。合成比较不能解锁。
输入、清单或引擎改变时拒绝混用凭据。原生目录始终恰好六个长期文件；外围文件不混入运行目录。
export 只读检查已提交字节、CSV2完成状态、原始输入单元格和覆盖，再检查平仓侧、B末dense高度、
OID/订单排序、Decimal数量金额和盘口哈希。跳过行必须有原因。
交付47列为原46列加空snapshot_lag_ms，状态点仍为block_end；只交付新增段，不计算CSV3。

## 验证与版本

测试使用明确标记的合成原生 RMP/.lz4：新增、更新、零剩余量后删除、缺块、坏文件、
免费权限/主机/重定向/限额、下载中断重取、答案隔离、六文件与恢复、47列及提交字节损坏。
合成测试不证明 Dwellir 真实数据完整。原生检查点格式不变；工具升级或输入修复使用新运行，
不强制恢复旧检查点。真实生产使用Python3.12或更新版本。
