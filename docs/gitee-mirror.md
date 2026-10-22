# Gitee 发行包镜像

此镜像在自己的 GitHub fork 中运行，固定读取 [1003129155/jietuba 的最新稳定 Release](https://github.com/1003129155/jietuba/releases/latest)，复制到 [lowvaluetarget/jietuba 的 Gitee 发行版](https://gitee.com/lowvaluetarget/jietuba/releases)。fork 不会因上游发布 Release 而收到本仓库的 `release` 事件，因此使用每小时第 17 分钟的定时检查；GitHub 的定时任务可能延迟，并非实时同步。

只复制同一正式版本的四个原始 ZIP（完整版、轻量版各含 x64 与 ARM64）和 `SHA256SUMS.txt`，下载后核对 SHA256；默认不回填旧版历史。不会重新构建、重打包、重新签名或修改程序内下载源；原有 C++ 应用、共享 Rust 和归档 Python 实现均无改动。GitHub 保留完整发行历史。

已完成核验的稳定镜像会在发行说明中记录核验凭据（receipt）。后续同版本检查中，附件 ID、大小和上游哈希均未变化且记录匹配时，只读取元数据和小校验清单，不重复下载 ZIP；首次接管或记录失配时仍执行完整下载核验。

## 初次启用

1. 将工作流、脚本及测试合入自己的 fork **默认分支**，在仓库的 Actions 页面允许工作流运行。仅推送到功能分支不会开启定时同步。Gitee 目标仓库须公开且已初始化。
2. 已手动上传的 `release-2.1.9` 可以在仍为上游 latest 时接管：同一发行版必须恰好包含上述五个附件，文件名、大小与 SHA256 均须匹配。接管会更新发行说明，并加入脚本的 `<!-- jietuba-mirror:1003129155/jietuba -->` 标记；不要手工给其他发行版添加此标记。上游已有更新版本时，脚本只处理该最新版本，不自动接管旧的手动发行版。
3. 在 GitHub fork 的 **Settings → Secrets and variables → Actions** 中设置：

   | 类型 | 名称 | 值或用途 |
   | --- | --- | --- |
   | Repository variable | `GITEE_REPOSITORY` | `lowvaluetarget/jietuba` |
   | Repository variable | `GITEE_MIRROR_ENABLED` | 初次准备时为 `false`，完成预演后改为 `true` |
   | Repository secret | `GITEE_TOKEN` | 对目标 Gitee 仓库具备发行附件管理权限的私人令牌 |

   令牌只填入 GitHub Secret 或本机进程环境，不要写入源码、提交、命令行参数、日志或聊天。GitHub 读取使用工作流自带的只读 `GITHUB_TOKEN`，无需另建 GitHub 私人令牌。
4. 在 Actions → **Gitee release mirror** → **Run workflow** 中选择默认分支，保留 `dry_run=true`。只读预演不要求开启 `GITEE_MIRROR_ENABLED`；检查目标仓库、待上传/接管附件及拟清理范围，也可以使用下文的本地命令预演。
5. 确认计划后，将 `GITEE_MIRROR_ENABLED` 改为 `true`，再手动运行并取消 `dry_run`，或等待下一次定时同步。**开启此变量就允许每小时任务实际上传及清理**；手动运行的默认值始终是只读预演。

目标仓库为空或手动选择非默认分支时，同步任务跳过；未启用时仅允许手动只读预演。推送与 PR 只运行无 Gitee 凭据的 Python 测试。同步任务固定检出默认分支本次触发、已通过前置测试的同一提交，令牌只传入同步步骤；固定并发组不会取消正在上传的任务。公开仓库连续 60 天无活动时，GitHub 可能自动停用定时工作流，需要在 Actions 中重新启用。

## 保留与容量

默认按版本号保留最近 **3** 个受管理稳定版本；已被新版替代、仍处于预发布状态的旧半包列入清理，不占这 3 个名额。Release 附件预算为 **800 MB**（十进制）。统计包含 API 返回的全部发行附件，非托管附件也占预算但不会被清理。仓库普通附件无法通过此接口完整统计，因此另外预留约 **200 MB** 余量；这**不等于精确的全仓配额查询**。

单个 ZIP 不得超过 **100 MB**。通常先上传、回读并核验新版，再清理最老版本；若上传期间会超预算，只预先清理计划中的旧版，同时完整核验并保留当前最新稳定镜像。无法同时容纳当前稳定版与新版时停止，不删除当前可用最新版。容量紧张时可能保留不足 3 版；`--keep` 可设为 1–20，`--budget-mb` 可设为 1–800。

清理只删除带脚本标记的发行版中那五个约定附件，不删除发行说明、标签、源码或其他文件。上传失败时保留旧的可用最新版；此前已预清理的更老附件不会自动恢复。新建镜像先标为预发布，五个附件均从 Gitee 下载核验后才改为稳定版。**Gitee 预发布仍公开可见，并非私有草稿**，上传期间可能暂时缺少部分附件。完整旧包仍可回到 GitHub 下载。

## 本地预演与测试

使用 Python 3.11 或更新版本，在仓库根目录执行。读取令牌的环境变量为 `GITEE_TOKEN` 和可选的 `GH_TOKEN`；本地自行安全配置，不把令牌作为参数传给脚本。

```powershell
# 默认只读：查看计划，不上传、不删除。
python scripts/mirror_gitee.py --target-repo lowvaluetarget/jietuba

# 明确执行上传、接管及受管理附件清理。
python scripts/mirror_gitee.py --target-repo lowvaluetarget/jietuba --apply

# 调整保留数和附件预算；不带 --apply 仍只预演。
python scripts/mirror_gitee.py --target-repo lowvaluetarget/jietuba --keep 3 --budget-mb 800

# 与此工作流一致的无密钥自动化测试。
python -m unittest discover -s scripts -p 'test_*.py'
```

预演和自动化测试不证明真实上传、Gitee 下载、附件删除或 GitHub 定时触发已通过。首次启用后应从 Gitee 下载核验，并在 Actions 日志检查实际结果。暂停自动同步时将 `GITEE_MIRROR_ENABLED` 设回 `false`；不要通过删除仍可用的最新版处理失败。

参考：[GitHub 事件与默认分支规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[Actions 并发控制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)。
