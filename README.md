# Skill_RSI

## yangyang分支框架入口

[framework](framework/README.md)提供Claude Code rollout采集、轨迹经验、技能管理及独立评审。新增[三模式采集](framework/docs/ROLLOUT_COLLECTION.md)保留分类100题长会话、同100题乱序长会话及300题逐题独立session，并附官方301题全量题单。[双CLI框架](framework/companions/README.md)包含Claude Code CLM和Codex CLM独立源码与测试。

各模式的运行条件、历史实测和本轮CPU验证分开说明，源码来源与文件校验位于`framework/UPSTREAM_SNAPSHOTS.json`和`framework/DELIVERY_PINS.json`。

面向 **Claude Code、Codex、DSH**，持续产生有效、可复用的 trajectory，并根据轨迹不断迭代有利于 **context/memory 管理的 skills**。

## 项目目标

```mermaid
flowchart LR
    A[三个 harness 持续执行] --> B[轨迹采集与质量检查]
    B --> C[提炼或更新 context/memory skills]
    C --> D[加载到后续任务]
    D --> E[结果与成本评估]
    E --> B
```

- 轨迹有效：保留任务、框架与模型版本、关键执行过程、工具反馈和结果，能够核对来源与质量。
- 轨迹可复用：保留适用条件与依赖，可用于经验提炼、后续任务或合格训练数据构造；具备复用条件与已验证迁移分别标注。
- Skills 持续迭代：围绕信息保留、上下文压缩、记忆读写与检索、冲突及过期记忆处理，经历生成、使用、反馈和修订。
- 效果分别验证：三个 harness 分别记录实际加载、任务质量、管理表现和成本，跨框架复用另做验证。小模型训练与上述工作并行。

## 仓库与当前状态

仓库地址：[https://github.com/hzher/Skill_RSI](https://github.com/hzher/Skill_RSI)

| 项目                         | 当前状态                                                                               |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| 项目说明与协作文件           | 以推送到 GitHub 的版本为团队依据                                                       |
| 实现代码与输入配置           | `yangyang`提供[采集与技能框架](framework/README.md)、三模式题单和双CLI源码；验证与数据索引在对应文档 |
| 主分支                       | `master`为共同基线和远端默认分支，本次框架交付位于`yangyang` |
| 成员分支                     | 各成员按约定在自己的长期分支工作，本次发布到`yangyang` |
| 访问与保护规则               | 见[仓库设置与状态](docs/REPOSITORY_SETTINGS.md)，不能把本文约定当成已生效的 GitHub 权限 |

本次`yangyang`已merge当前`master`并导入框架。团队可在`yangyang/framework`阅读和使用，公共主线的集成沿用下述PR流程。

## 分支约定

| 分支              | 用途                                     |
| ----------------- | ---------------------------------------- |
| `master`        | 团队共同主线，由维护者集成经过审核的变更 |
| `yangyang`      | 对应成员自行创建的工作分支               |
| `huangzhenghao` | 对应成员自行创建的工作分支               |
| `zhaozilin`     | 对应成员自行创建的工作分支               |
| `xuyifan`       | 对应成员自行创建的工作分支               |

分支名只是协作约定，不是 GitHub 用户名，也不自动限制其他协作者写入。工作在自己的分支进行，通过 PR 合入 `master`，不互相覆盖成员分支。

### 1. 拉取主代码，创建自己的分支

以 `yangyang` 为例，其他成员替换为自己的约定分支名：

```bash
git clone https://github.com/hzher/Skill_RSI.git
cd Skill_RSI
git fetch origin
git switch --no-track -c yangyang origin/master
git push -u origin yangyang
```

如果自己的分支已经存在于远端，首次切换用 `git switch --track origin/yangyang`；本地已存在则用 `git switch yangyang`，无需再次创建。私有仓库需先获得协作权限，并使用自己的 GitHub 认证。

### 2. 开始修改前，同步主分支

```bash
git fetch origin
git switch yangyang
git merge origin/master
git push origin yangyang
```

遇到冲突先解决并确认结果，再提交和推送。长期成员分支通过 merge 同步，避免 rebase 后强推改写已共享的历史。

### 3. 提交修改，发起 PR

只暂存本次需要提交的文件。例如修改 README 后：

```bash
git add README.md
git commit -m "docs: clarify trajectory reuse criteria"
git push origin yangyang
```

在 GitHub 发起 Pull Request：**base 选择 `master`，compare 选择自己的分支**。填写目标、变更、验证和证据，关联对应 Issue。草稿阶段使用 Draft PR。

至少一位他人审核并解决讨论后，由维护者执行 **Squash and merge**。合并后保留个人分支，按上面的同步步骤继续工作。详细流程见[贡献规范](CONTRIBUTING.md)。

## 协作方式

- 每项任务先建 Issue，写清目标、负责人、验收标准和依赖；一个 Issue 聚焦一个可交付结果。
- 每个 PR 聚焦一个问题。实验结论提供配置、版本、运行 ID 与结果位置，区分“跑通”和“有效”。
- 阻塞及时更新到 Issue；重要接口与路线调整先形成提案，再按结论修改。
- 原始轨迹、录音、模型权重、私有凭证和大体量实验产物放在适当的受控存储，仓库保留经过检查的说明、小样例及索引。
- 当前不要求 CI 状态检查；实现代码导入后，再按实际运行能力增加检查。

## License

 [MIT License](LICENSE)。
