# 经验、Skill Generator 与技能库

这三个模块接在统一任务包 → API Executor → 原 evaluator 之后。每次任务结束时自动保存公开 TaskSpec、完整轨迹、实际工具调用（含原始大输出、异常与耗时）、workspace 常规文件内容、原始评分回执、API usage、请求次数、执行耗时及压缩视图。产物按 SHA256 去重持久化，运行目录删除后仍可读取；符号链接不作为产物复制。经验存储在 `.runtime/experience/experience.sqlite3`，含任务数据，仅供私有运行使用。

`money=null` 表示 provider 未报告货币费用，不当成免费；usage 逐响应保留，usage_complete 明确表示覆盖情况。经验不是训练样本，`training_admitted=false`。历史轨迹只有预览时不恢复不存在的原输出；新 Executor 的 `tool_calls.json` 记录预览前原输出。只有原轨迹里存在的 token/usage 才保留。

`SkillEditor.prepare` 输出确切 system/user 消息、三项 SkillOS 工具定义、相关技能版本和请求摘要。轨迹使用 `{step, action, observation}`；默认保留最近 100 步，每个 observation 最多 12000 字符，删减记录在 truncation，原证据完整保留。原评分为未知时保留 reward=null；已声明修订评分和原评分完整保存，生成输入默认只使用原分摘要，不包含 evaluator 的检查详情、stdout 或参考答案。当前不实施完整 prompt token cap，超出 provider 上下文的请求应失败，不能声称已通过 token 预算检查。

技能是带 name/description YAML frontmatter 的 Markdown。严格支持 `new_skill_insert`、`skill_update`、`skill_delete`，包括重命名；重复 JSON/YAML key、名称不符、缺少技能、重命名冲突或批次中任一错误都会拒绝整个批次。`[]` 是有效空操作。

真实 API 试跑发现部分模型会在文本 JSON 操作名中加 `functions.` 前缀。`transport.py` 仅将三个已知 `functions.<合法操作>` 映射到原 SkillOS 名称，再交给原严格 handlers；其他命名空间和未知操作仍拒绝。候选同时保存原回复、规范化回复、逐项别名映射和两份摘要，Markdown 内容不改写。默认提示保持已实际使用的原模板；额外操作名说明曾连续两次遇到上游 400，同一任务内容恢复原模板后成功，但单组对照不能证明全部 400 的根因。该兼容属于本地传输适配，不宣称原始回复已经符合上游严格协议。

`passed=true, reward=1` 是前述成功样例的原评分，不是 Generator 的输入准入条件。当前 editor 对有效未通过经验也生成请求：`status=measured, passed=false, reward=0`，提示词要求从失败轨迹识别问题、提炼可能修复问题的技能；也可更新、删除已有技能或返回 `[]`。这只是模型任务指令，尚未证明失败根因已识别或生成技能有效。未知结果保持 `status=unknown, passed=null, reward=null`；当前接口允许准备未知经验的请求，不会自动筛选或把未知改为失败。

建议采集层保存全部经验，正式生成阶段按证据质量筛选。原分通过且轨迹完整、没有已知任务/工具/评分缺陷的经验可用于成功流程提炼；有效未通过但能从公开轨迹明确定位可改进步骤的经验，可用于失败教训与恢复流程提炼。API/初始化/工具服务故障、前提数据缺失、评分争议和结果未知的经验应先诊断，暂不进入正式技能生成。原分未过但修订评分通过须单独标识，不与原版成功混合。这个选择原则是当前建议，尚未实现自动准入过滤；仅保留通过样例也不能替代下游技能效果验证或训练数据 split/firewall。

技能库在 `.runtime/skill_library/skills.sqlite3` 中保存内容寻址快照、候选与发布 head。候选记录绑定经验、Generator policy、确切请求、回复和 provider usage。候选可直接供诊断 Executor 执行，但只有发布 head 是默认检索对象；发布必须比较预期 head，冲突需要从新 head 重新生成候选。BM25 检索返回技能内容、分数和快照版本。

以下命令均在项目根执行，输出文件必须不存在。接口验收不需要 API key；generate 和任务 run --execute 会真实调用配置的模型。

```bash
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python -m pip install -r configs/skill_generator.requirements.in
# 将已有真实批次导入经验；核对原任务集与源码快照，不覆盖原运行。
bash ../codex/run_and_log.sh --artifact .runtime/imports.json -- .runtime/api-executor-venv/bin/python scripts/skills.py --output .runtime/imports.json ingest-run --run-directory .runtime/executors/<run_id>
# 从 imports.json 选取经验 ID，生成可审阅请求。
bash ../codex/run_and_log.sh --artifact .runtime/editor_request.json -- .runtime/api-executor-venv/bin/python scripts/skills.py --output .runtime/editor_request.json prepare --experience <experience_id> --generator-policy-id <policy_revision>
# 接收实际生成的 raw JSON 操作数组；离线 apply 不声称经过模型采样。
bash ../codex/run_and_log.sh --artifact .runtime/candidate.json -- .runtime/api-executor-venv/bin/python scripts/skills.py --output .runtime/candidate.json apply --request .runtime/editor_request.json --response <operations.json>
# 或用 API 直接生成候选，宿主已安全加载 MODEL_API_KEY / MODEL_API_URL。
bash ../codex/run_and_log.sh --artifact .runtime/generated_candidate.json -- .runtime/api-executor-venv/bin/python scripts/skills.py --output .runtime/generated_candidate.json generate --experience <experience_id> --generator-policy-id <policy_revision> --provider openai --model <generator_model>
# 发布以空库为父的候选；更新已有通道时传入原 head 摘要。
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/skills.py publish --candidate <candidate_id> --expected-head empty
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/skills.py search --query 'verify deliverables'
# 固定 Executor 按明确技能快照执行独立下游任务；默认仅计划。
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py run --benchmark <benchmark> --task <downstream_task> --provider openai --model <fixed_executor_model> --skill-version <candidate_version>
# 加 --execute 实际执行后，由该次 experience 取得归因。
bash ../codex/run_and_log.sh --artifact .runtime/attribution.json -- .runtime/api-executor-venv/bin/python scripts/skills.py --output .runtime/attribution.json attribute --candidate <candidate_id> --experience <downstream_experience_id> --fixed-executor-id <executor_id>
```

Executor plan 保存确切技能快照；执行身份绑定源码、镜像、provider、模型、endpoint 摘要和预算。每次执行使用唯一 run ID，变更技能不会变更固定 Executor 身份。模型别名本身不提供权重不可变保证；研究训练仍须固定模型 revision。归因拒绝用种子任务充当独立下游任务，并核对执行身份及技能版本，未知结果保持 mask。当前把整个选定快照注入 Executor；需要只执行检索子集时应先建立对应快照，不暗中更改版本含义。

历史 `training.skill_contract` / `SkillRegistry` 和 RSI 服务保留自己的 declarative skill-v1 及签名 admission，Markdown 库不冒充该训练格式。真实 Generator token IDs/logprob、冻结策略版本、独立 split/firewall、下游奖励、verl 更新仍需另行采样验收。此接口验收只覆盖经验与技能模块连接，不证明技能提升效果。

SkillOS 完整克隆位于项目内 `.upstream/SkillOS-Replication/`，运行所需固定副本位于 `integrations/skill_generator/vendor/`；`codex/` 只用于检查、运行记录与个人进度分析。固定来源与本地差异见 [源码接线说明](../../integrations/skill_generator/skillos/README.md)。可复现验收命令：

```bash
bash ../codex/run_and_log.sh --artifact .runtime/experience_skills_check -- .runtime/api-executor-venv/bin/python scripts/verify_experience_skills.py --run-directory .runtime/executors/<run_id> --output .runtime/experience_skills_check
```

该验收运行真实 Docker/MCP/服务/原评分检查、带技能的容器工具执行与归因，以及历史真实 API 轨迹的请求适配；模型输出由测试 driver 提供，不新发收费请求。详细结果以 `receipt.json` 为准。

2026-11-02 起，`scripts/run_skill_pipeline.py` 默认使用本地 Qwen3-8B Curator/SelfJudge、Qwen3.8-27B ContentJudge，API 仅用于固定 Executor；旧 API Generator 批次代码已退出此入口。默认只列计划，`--execute` 先检查三角色就绪，再逐题执行和编辑，后一题用于检验前一候选。详见[三角色指南](skillos_roles.md)。

```bash
bash ../codex/run_and_log.sh --artifact .runtime/pipelines/<new_run_id> -- bash scripts/generator_python.sh -u scripts/run_skill_pipeline.py --run-id <new_run_id> --count 20 --model gpt-6.1-sol --execute
```

每个新批次从空技能快照开始，在独立的 `trial_<run_id>` 通道累积候选。生成请求、provider 原始回复、校验失败诊断、候选和技能 Markdown 清单保存在 `.runtime/pipelines/<run_id>/`；不会更新默认 main 通道。一次生成不符合严格协议时保留原始失败，不擅自把格式错误改成成功。真实 API 生成只证明模型产生了技能候选，尚不证明 Generator 已具备训练 token/logprob、技能能改善独立下游任务或 verl 更新完成。

针对初始化失败，可用同一入口的 `--task <task_id>` 指定单题，并使用新 run ID；原始失败和恢复版本分别记录。`scripts/recover_skill_responses.py --pipeline <original_pipeline> --output <new_directory>` 能对保留的真实回复执行明确的传输适配和重新校验，不调用模型；候选保持原请求及原父版本。该脚本另行三方合并无冲突编辑形成诊断池，同名冲突保留在候选分支，不覆盖已有内容。诊断池不是单次 Generator 采样，也不自动发布 main。

2026-10-31 已完成前 20 题的真实任务到技能试跑：主批 19 题有效评分，网络初始化失败单题恢复后覆盖 20 个不同任务；原版通过 6 题，另 3 题仅修订评分通过。20 条真实生成回复形成 20 个有效候选，经记录明确的传输适配与无冲突合并后得到 45 条诊断技能；原始一遍执行和恢复分开统计。结果见 `evidence/mainline/skill20_pipeline_20261031.json`，完整生成文件在 `.runtime/skill20_reporting_20261031/final/`。当前没有用这些真实生成技能验证独立下游效果，训练 token/logprob 与 verl 更新仍未验收。
