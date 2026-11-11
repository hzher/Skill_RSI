# 仓库设置与状态

核对日期：2026-11-11。仓库：[hzher/Skill_RSI](https://github.com/hzher/Skill_RSI)。

## 已完成的设置

| 项目 | 实际状态 |
|---|---|
| 文档发布 | README、协作规范、模板及配置已发布；实现代码尚未导入 |
| 默认分支 | `master`，本地跟踪 `origin/master` |
| 分支 | 保留原 `main`；成员分支由本人随后创建，本次未代建 |
| `master` 保护 | 必须通过 PR、解决讨论、保持线性历史；禁止强推和删除，管理员同样受约束 |
| 初始化审核 | 批准人数为 0；其他有写权限的审核成员接受邀请后，再启用至少 1 位他人批准 |
| CODEOWNERS | 当前 `@hzher` 统一负责；不强制唯一 CODEOWNER 批准 |
| 合并方式 | 仅启用 Squash merge；merge commit 和 rebase merge 已关闭 |
| 分支清理 | 自动删除来源分支关闭，保留长期成员分支 |
| 功能入口 | Issues、Projects 开启，Wiki 关闭；未创建项目看板 |
| 自动合并与 CI | 自动合并关闭；未设置不存在的必需 CI 检查 |
| 负责人访问 | 已向 `@sheepyedu` 发出协作者邀请，待对方接受；接受后具备读写与 PR 合并权限 |
| 可见性 | 当前仍为 `public`，本次未改变可见性 |
| 本地 Git 行为 | `pull.ff=only`、`fetch.prune=true`、`push.default=simple`，仅对此仓库生效 |

GitHub 插件已安装并用于本次发布；插件认证与本机 Git 命令行认证不同，后续直接使用 `git push` 仍需配置本机 GitHub 凭据。远端发布通过 GitHub 插件完成，初始发布提交 `ab74a1a` 的文件树已与本地核对一致；本轮设置状态修订通过 PR 同步。

## 当前保护的含义

任何人，包括所有者，都需通过 PR 修改 `master`。初始化阶段尚无其他已接受邀请的协作者，暂不要求他人批准；自查不会被记录为他人审核。负责人接受邀请并核验写权限后，再把批准数提高为 1。

协作者可以按保护规则提交、审核和合并 PR，不能直接绕过保护。个人账号仓库不能给第二人单独授予 Admin；负责人若要管理仓库设置或访问控制，需要组织仓库方案，须另行确定组织和迁移安排。

## 待完成事项

1. 负责人接受邀请后，核对写权限，启用一位他人审核及旧批准失效。
2. 按实际分工补充 CODEOWNERS；其他成员的账号和权限由本人确认。
3. 维护者整理实现代码，通过 PR 导入；成员随后自行创建工作分支。
4. 实现代码具备实际运行检查后，再启用对应 CI。
5. 早期计划提过私有共享，目前仓库仍公开。若后续改为私有，先核对套餐是否支持所需保护；本次不改变可见性或购买套餐。

## 设置清单与权限边界

[repository-settings.json](../.github/repository-settings.json) 记录后续团队就绪时的目标设置，不是自动执行文件；其中私有可见性和一位审核者要求尚未应用。GitHub 不会自动读取并执行它。

`CODEOWNERS` 只定义审核归属，不授予访问权限；个人分支名也不隔离成员间的写权限。参考 [个人仓库权限说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)及[分支保护说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。

## 项目跟踪

每项工作用 Issue 记录，按 `Todo → In progress → Review → Done` 更新。初期使用 Issues 即可，需要看板时再建立 Project。资源请求、运行预算和实验结果进入 Issue，PR 关联对应任务。
