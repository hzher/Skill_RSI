# PAST-Bench × Codex 数据生产

## 全量 204 题接线与启动

全量适配器为 `pastbench_full.py`，批次编排为 `pastbench_campaign.py`。使用上游 `generate_manifest` 生成实际题序、history mode、锚点、机制开关与 seed 路径；不按 history_plan 的分组遍历顺序执行。支持 notes、config、helpdesk/KB、Plonk、X 原服务，统一为每个任务分配独立回环端口并重置业务状态。

新增 MCP `session_search` 仅检索本历史分支中提供的原 seed 和真实先前会话，不检索其他任务族。原始 seed 导入保留来源摘要，已有更新不因同一 seed 的重复声明而被盲目覆盖。无持久化及单机制开关沿用上游 `resolve_episode_tool_config`：关闭的 memory/skills/session 不暴露给 Executor。

全量评分改用原上游 `grade_episode`、LLMJudge、artifact diff、retrieval signals、mechanism scores 和 hard-pass 条件。原 judge 请求通过框架 HTTPModel 记录，原评分逻辑不修改。此处 notes_memory 的 hard_pass 与旧八题试跑的简单 task_score 阈值判定不同，不能把两批数字拼接。原 LLM judge 的 parser/retry 仍由固定上游实现控制，原始回复与每次请求保留。

为评估分离的执行/技能管理管线，全量 adapter 在 score-blind Curator 结束后独立评分：技能创建/更新计数只来自实际 Curator `applied_ops`，明确标记 actor，不冒称原生 Codex 做了 `skill_manage`。Executor 的 Skill/memory/session 读取仍只统计实际 MCP 事件。Curator 不接收评分。此适配不等于 Hermes/Hermes+ 复现。

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/full204/campaign-summary.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench_campaign \
  --source upstream/PAST-Bench --config framework/examples/pastbench-codex.json \
  --output .runtime/pastbench/full204 --workers 4
```

每个任务族是独立进程，冻结同一源码快照；族内按上游题序执行。失败/缺评分原样保留，完整性不足的任务不写成已验收完成。全量运行继续尝试后续 episode，不静默重放不完整调用。`family-progress.json` 和原 `hold.json` 记录实际状态；受失败学习位置影响的后续历史需要结合谱系审计解释。

2026-11-12 全量预检：204 个任务/seed/评分入口通过，6 类原服务实际启动、重置、审计通过，无模型调用。34 项框架与适配合同测试通过。首个全量批次位于 `.runtime/pastbench/full204-20261112-v1/`；开始运行不等于已完成验收。

本机批次先以 4 个 worker 启动，再以同一冻结源码增添 4 个 worker。额外编排记录为 `extension-4-workers/plan.json`，新旧调度都用原子创建 family 目录占用任务，不重复执行。原调度遇到已占用目录会留下调度错误观察；最终需从每族实际 `worker-result.json` 另生成 `campaign-reconciled.json`，不覆盖原 `campaign-summary.json`，导出明确引用所用汇总及 SHA。占用冲突不能混算成任务/模型失败。

批次结束后生成可读总表、逐题 Markdown 轨迹、独立技能 Markdown、成功轨迹 JSONL 和 unresolved 清单：

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/full204-export/manifest.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.export_campaign \
  --campaign .runtime/pastbench/full204 --output .runtime/pastbench/full204-export
```

入口为导出目录的 `RESULTS.md`，表格为 `families.csv`，逐题为 `traces/<family>/<task_id>.md`，技能为 `skills/<name>--<sha>.md`。明确区分原提供的 seed 技能与 Curator 生成的版本；没有完整 wire、pipeline 完整性或原通过判定的样本不混入 successful-trajectories。

## 2026-11-12 实际验收

SM01 八题全部执行完成，原 grader 5 题通过、3 题未通过、0 个缺失评分。实际回执见 [汇总](../reports/pastbench-codex-sm01-20261112.json)。冻结运行源码、八题 memory/skill 历史分支、原评分与导出摘要审计通过；31 项框架及适配合同测试通过。

| Episode | 原 task_score | 原通过判定 |
|---|---:|---|
| cold_style_baseline | 0.6000 | 未通过 |
| learn_a_tsv_preference | 1.0000 | 通过 |
| learn_b_preference_reinforcement | 1.0000 | 通过 |
| eval_near_apply_saved_style | 1.0000 | 通过 |
| eval_far_transfer_saved_style | 1.0000 | 通过 |
| control_shortcut_visible_style_rule | 0.8667 | 未通过 |
| control_style_preference_control | 1.0000 | 通过 |
| control_no_persistence_style | 0.6000 | 未通过 |

近、远迁移会话均继承真实 memory 并采用所学格式；本轮没有任何 Skill 工具全文读取，故 5 个导出 skill 版本均保持候选，不能宣称技能产生了任务增益。40 次原生 Responses 请求、32 次 Curator 请求、26 个工具事件与 2 次 memory 写入有原回执。5 次终止响应缺少 reported model 字段，原样保留缺失；其余明确报告 `gpt-6.1-sol`。无 judge 请求、无参数更新、无独立下游评测。

本机运行位于 `.runtime/pastbench/sm01-v3/`，导出位于 `.runtime/pastbench/sm01-v3-export/`：8 条完整原生轨迹、其中 5 条原评分通过轨迹、5 个候选技能版本。数据只覆盖一个开发任务族；原始轨迹不随 Git 分发。服务已退出。首两次配置/依赖诊断保留在独立旧目录，不混入这批结果。

在 `xuyifan` 分支复用现有 Pipeline、Curator、DeltaLibrary 和 Reviewer，新增原生 Codex CLI 执行器及 PAST-Bench notes 环境。模型参数不更新。本接线是 Codex 的 benchmark 适配，不是 Hermes/Hermes+ 复现。

## 输入与角色

- 公开任务来自固定版本 PAST-Bench 的 `task.yaml`。模型不接收 `expectations.json`、grader、后续题目或私有评分。
- Codex executor 使用 `codex exec --json` 和 Responses API；MCP 将原任务工具转发到原 mock service。关闭 shell 和多 agent 工具，使用独立 CODEX_HOME、只读 sandbox；这是可信任务的本机运行方式，不是容器级宿主读取隔离。
- Memory 保存明确长期偏好/约束；继承的完整 memory 在新会话输入中明确列出。`memory` 工具提供真实持久化的 read/add/replace/remove，替换必须匹配确切原条目。Skills 是可选目录，模型通过 MCP Skill 按需读全文。
- Curator 和 Reviewer 复用框架原有 OpenAI-compatible Chat Completions 工具循环，模型同为 `gpt-6.1-sol`；它们不是 Codex CLI 会话。Curator 只能看公开执行经验，不接收客观评分。
- 原 grader 在宿主独立调用，保留原维度与 `compute_task_score`、`is_pass`。Communication LLM 未调用；notes-memory 模式下它不进入 task_score。机制字段由实际 memory 工具事件、继承输入及持久化文件生成，不把 Skill read 当成 memory write。

## 范围

目前只支持无额外附件、sandbox files 或 Hermes seed 的 notes-service 任务。其他任务必须补上相应公开工作区、原服务和持久化适配；预检拒绝，不跳过输入后继续运行。不宣称覆盖 26 族或 204 题。

按原 `family.yaml/history_plan` 执行：continue 继承上一位置；from_anchor 的每个 episode 独立复制同一锚点；fresh 的每题从空 memory/library 开始。Memory 与 skill library 一起分支，task service 每题独立启动，不能继承业务状态。

## 启动

从仓库根目录执行，凭证通过已有环境变量提供，不写入配置或 argv。

```bash
git clone https://github.com/Gen-Verse/PAST-Bench.git upstream/PAST-Bench
python3 -m venv .venv
.venv/bin/python -m pip install -e 'upstream/PAST-Bench[mock]'

../codex/run_and_log.sh --artifact .runtime/pastbench/sm01/family-summary.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench \
  --source upstream/PAST-Bench \
  --config framework/examples/pastbench-codex.json \
  --family memory_ability/SM01_preference_adoption \
  --output .runtime/pastbench/sm01
```

默认模型与本次用户指定路由在示例配置中。运行前必须已安装 `codex`，并在父进程设置 `OPENAI_API_KEY`。原任务工具只作用于本地 mock service；配置对该 MCP server 自动批准，不改变用户日常 Codex 配置。

需要正式对照时使用新目录、新批次，固定模型、预算和上游 commit。已有 intent 但无完成回执的调用不自动重放。新增 driver 遇到不完整 episode 或 null 评分会停止传播；应先核对原始回执，不能删除 intent 盲目续跑。

## 产物与质量

每个 episode 是一个框架 run，包含：

```text
family-plan.json / family-progress.json / family-summary.json
anchors/<name>/{library/,memory.json}
episodes/<episode>/
  plan.json / report.json / library/ / memory.json
  episodes/<original_task_id>/
    snapshot.json / execution.json / exposure.json
    actor/{prompt.json,mcp.json,native-cli.jsonl,native-exit.json}
    actor/wire/<request>/{request.json,response.raw,result.json}
    actor/mcp-events.jsonl
    curator/{observation.json,messages.json,requests/,candidate-*.json,commit.json}
    environment-stop.json
  evaluation/<original_task_id>/{objective/,review/}
```

模型认证由宿主 Responses recorder 转发，密钥不交给执行器或 MCP，不保存 HTTP 认证头。完整 SSE 原文与实际请求逐次保存，费用统计来自原 usage；金额未知保持 null，不估算成实付费用。需完整 Skill 内容出现在已完成的实际后续模型输入中，才记录 body_proven；目录里有名称或模型声称使用，不足以证明曝光。

质量应分别核对执行完成、原任务评分、分支谱系、技能事务合法性、实际全文曝光与后续动作。产出技能并不等于技能有效。冷启动、对照和失败轨迹保留作诊断；任务未通过不得混入成功样本；新 skill 在后续任务消费前只能标为候选。API 轨迹没有训练所需的真实逐 token logprob，本接线不宣称已可直接用于 policy-gradient 更新。

完整任务族结束后，用全新目录导出；失败/unknown 保留在 all-trajectories，只有原评分通过的完整轨迹进入 successful-trajectories。技能按确切内容 SHA 保留版本和成功消费者证据，不把共现当作因果增益。

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/sm01-export/manifest.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.export_pastbench \
  --family-run .runtime/pastbench/sm01 --output .runtime/pastbench/sm01-export
```

导出文件为 `all-trajectories.jsonl`、`successful-trajectories.jsonl`、`skills.jsonl` 和带文件摘要的 `manifest.json`。保留原 Responses 请求和终止响应；网关未报告 model 字段时保持缺失，不用请求值补写；明确报告其他模型时拒绝发布该导出。

`upstream/`、`.runtime/` 和原始轨迹不提交。仓库只保存代码、配置、操作说明和经过检查的汇总结果；运行时源码、上游 commit、原输入摘要由回执记录。
