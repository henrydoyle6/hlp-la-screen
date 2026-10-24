# 维护、验收与发布

长期规则：**功能分支 → PR → 自动测试与验收 → 合并 main → 标签与 Release → Ubuntu 指定版本**。
不直接提交到 main，不绕过失败测试，不移动或覆盖已发布标签。

## 1. 修改与 PR

```bash
git switch main
git pull --ff-only
git switch -c fix/具体问题
# 修改源码、必要测试与文档
# VERSION 填新的 X.Y.Z；CHANGELOG.md 增加 ## X.Y.Z；README 安装示例同步版本
python -m unittest discover -s . -p 'test_*.py' -v
python tools/check_release.py --unreleased
git diff --check
git diff
# 逐个选择应提交文件，检查 git diff --cached 后再提交
git add 文件1 文件2
git diff --cached
git commit -m "说明具体变更"
git push -u origin fix/具体问题
```

在 GitHub 建立指向 main 的 PR，按模板填写行为变化、验证结果与配置/CSV/断点迁移。
每个正式发布使用新的语义版本：修复增加 PATCH，兼容功能增加 MINOR，不兼容变化增加 MAJOR。
PR 的 Acceptance 会检查新版本高于已存在的所有正式标签。

禁止提交原始 fills/diff/RMP、缓存、业务输出和凭据。RMP 读取及原回放/LA/精确比较保持受保护；
`tools/check_release.py` 对照既有基线检查文件哈希及发现函数源码哈希。只有用户明确授权修改相应组件，
并提供精确比较证据后，才可以在同一 PR 说明并更新保护基线，不能为通过 CI 直接改基线。

## 2. 自动测试与合并

`.github/workflows/ci.yml` 在 PR 和 main push 上运行：

| 必须通过的检查 | 内容 |
| --- | --- |
| Ubuntu tests (3.10) | Ubuntu 24.04 + Python 3.10，安装固定依赖，运行完整测试 |
| Ubuntu tests (3.12) | Ubuntu 24.04 + Python 3.12，运行同一完整测试 |
| Acceptance | 完整矩阵必须成功；版本/变更记录/受保护引擎检查；重跑 31 项 CSV 阶段验收 |

GitHub Actions 保留测试与验收日志。目前完整测试为 151 项（原生产测试 142 项，加发布门禁 9 项）。自动验收覆盖原始读取隔离、断点崩溃恢复、
双侧订单簿恢复、日期边界、缺口、缺 diff 和精确十进制。CI 没有本地真实大数据；历史真实验收见 `validation/`，
不能把“已有报告存在”当成此次重新验收。变更涉及真实数据行为时另行执行对应验收并在 PR 说明。

main 设置为必须 PR、三个指定检查成功、分支更新到最新 main、解决所有对话后才能合并，管理员也遵守。
当前不额外强制另一位人工审批者，以免单维护者无法合并；自动测试和 PR 仍然是必需步骤。
使用 Squash merge，合并后删除功能分支。合并完成还要等待 **合并后 main 同一提交** 的 CI 成功，不能只看 PR 测试。

## 3. 标签与发布

先确认没有新的未验收提交进入准备发布的位置，且 main 的 CI 页面为成功：

```bash
git switch main
git pull --ff-only
VERSION=$(cat VERSION)
git log -1 --oneline
python tools/check_release.py
git tag -a "v$VERSION" -m "HLP LA Screen v$VERSION"
git push origin "v$VERSION"
```

`.github/workflows/release.yml` 自动重跑完整 CI，然后校验：标签与 VERSION 相同、标签提交位于 main 历史中、
GitHub 上同一提交的最新 main push CI 成功。只有这些条件均满足才创建 GitHub Release，附上：

- `hlp-la-screen-vX.Y.Z.tar.gz`：该提交的 Git 跟踪源码，不打包本地未提交文件。
- `RELEASE_INFO.json`：标签、完整提交 SHA、成功 main CI 链接和保护检查。
- `RELEASE_NOTES.md`：该提交的变更记录。
- `SHA256SUMS`：上述三个附件的 SHA-256。

已存在 Release 时工作流拒绝覆盖。发布失败时先看 Actions 日志；同一合法标签可以通过 Actions 重跑失败任务。
如果标签选错、版本号不符或需要代码修复，提交新 PR、增加版本并发布新标签，不删除重打旧版本。
不要在只有标签、Release 尚未成功时通知 Ubuntu 升级。

## 4. Ubuntu 使用

只选择成功发布的明确版本，按 README clone 指定标签，或校验 Release 附件后解压。
每个版本使用独立源码目录和 venv；原始数据、输出、缓存放在源码目录之外。
复制 `run_hlp.py` 顶部配置值到新版本，不复制整份旧脚本覆盖新代码。
同版本可继续断点；计算/编排代码升级默认新建一轮，保持旧目录以便回溯。

助手按上述流程完成用户授权的后续更新，不需要重复询问日常提交、PR、合并或发布许可。
遇到认证、CI 或网络阻塞，明确报告停在哪一步，不宣称未完成的发布已经成功。
