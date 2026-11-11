# 仓库设置与状态

核对日期：2026-11-11。仓库：[hzher/Skill_RSI](https://github.com/hzher/Skill_RSI)。

**本文件区分已观察状态与目标配置。** `.github/repository-settings.json` 是设置清单，GitHub 不会自动执行它；`CODEOWNERS` 也不会自动授予权限或强制审核。

## 实际状态

| 项目 | 核对结果 |
|---|---|
| 远端可见性 | `public`，尚未改成私有 |
| 远端默认分支 | `main`，尚未切换成 `master` |
| 本地工作分支 | 已将 `main` 重命名为 `master`，清除旧 `origin/main` 跟踪关系；未创建成员分支 |
| 远端分支 | 仅观察到 `main`；本次不创建成员分支 |
| 主分支保护 | `main` 未保护；仓库 rulesets 返回空列表 |
| Issues／Projects／Wiki | 三项功能当前均开启；未创建项目看板 |
| 成员账号与邀请 | 尚未核实，不发送邀请 |
| 当前初始化范围 | README、协作说明、模板和设置清单；实现代码由维护者随后上传 |
| GitHub 写入 | 推送和设置需要仓库认证；是否生效以 GitHub 实际状态为准 |
| 文档提交 | 本批纳入说明与协作配置；实现代码由维护者随后整理上传 |
| 本地 Git 默认行为 | `pull.ff=only`、`fetch.prune=true`、`push.default=simple`，仅对此仓库设置 |

## 目标配置

| 设置 | 目标与原因 |
|---|---|
| 可见性 | 延续私有共享仓库的安排，目标为 `private` |
| 默认分支 | `master`，作为稳定集成主线 |
| 合并方式 | 启用 Squash merge，关闭 merge commit 和 rebase merge |
| 分支清理 | 关闭自动删除来源分支，保留长期成员分支 |
| `master` 审核 | 通过 PR，至少一位他人批准；新增修改后旧批准失效，合并前解决讨论 |
| `master` 历史 | 线性历史，禁止强推和删除；保护规则应用于管理员 |
| CODEOWNERS | 由 `@hzher` 统一负责；暂不强制 code-owner 批准，避免唯一负责人无法审核自己的 PR |
| 状态检查 | 当前没有 CI，不设置不存在的必需检查 |
| 功能入口 | 保留 Issues、Projects；关闭 Wiki，说明统一放仓库文档 |
| 自动合并 | 暂不启用，按团队规范由维护者执行合并 |

注意：Squash merge 是 GitHub PR 的合并方式；成员在自己的分支执行 `git merge origin/master` 用于同步，两者不冲突。

## 应用顺序

1. 维护者核对文件并使用自己的 Git 身份提交，整理和上传初版主代码。
2. 在 GitHub 发布或重命名主分支为 `master`，设为默认分支，并确认本地跟踪关系；不提前删除旧分支。
3. 将可见性调整为 `private`，应用合并方式、功能开关与分支保留设置。
4. 成员准备接入时，由本人提供真实 GitHub 账号，再授予协作权限；成员按照 README 自建工作分支。
5. 至少一位其他审核者就绪后，再启用必需审核及管理员保护，避免初始化阶段无人可审。私有仓库能否启用保护取决于账户套餐，需核对服务端结果；不能为了启用保护改成公开。
6. 用一个小型文档 PR 验证审核、合并、长期分支保留和同步流程，更新本文件的实际状态。

当前未确认成员账号，也未建立对应远端分支。成员自建分支不等于获得写权限，仍需仓库协作者访问。

远端 `master` 就绪后，维护者可执行 `git fetch origin`，再执行 `git branch --set-upstream-to=origin/master master` 恢复主分支跟踪；首次推送主分支时使用 `git push -u origin master` 也会建立跟踪。

## 权限边界

这是个人账号名下的仓库，当前方案区分仓库所有者与协作者，不承诺组织仓库的细分角色。个人分支归属、由维护者合并属于团队协作规范；分支名和 `CODEOWNERS` 不构成成员间的访问隔离。参见 [GitHub 个人仓库权限说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)。

主分支保护的套餐要求和可用设置见 [GitHub 分支保护说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。如果当前套餐不支持，记录“保护未强制生效”，先执行 PR 约定，再由维护者决定后续方案；不将文档约定写成平台已实施。

## 项目跟踪建议

每项工作用 Issue 记录，按 `Todo → In progress → Review → Done` 更新。初期直接使用 Issues 即可，需要跨任务看板时由维护者建立 Project。资源请求、运行预算和实验结果进入 Issue，PR 关联相应任务。当前尚未创建线上看板，也未自动分配任务。
