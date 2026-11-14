# 优先复用 SkillOS source204 的 Toolathlon 接线

2026-11-02 后续：原训练生命周期的 H20 部署迁移代码已新增，见[H20 指南](skillos_h20.md)。当前仅编译与简单 CPU 协议检查，不代表实际 GPU 验收；下文“尚待迁移”的内容描述此前阶段。ContentJudge 结构约束与权重安装已有实现，但仍待后续启动新服务实测。

2026-11-02：按用户最新要求，后续优先使用 SkillOS 固定提交 `3c039efeafd89400115426a9a7af19b58601f0be` 的最终 `source204` 源码与配置。旧两位置 A/B、K=4、标准差归一化训练器不再是目标实现；历史产物、回执和源码快照不覆盖。当前完成源码复用与 CPU 接口诊断，真实角色/任务联调、原 actor 生命周期迁移与参数更新尚未验收。

## 直接使用的上游代码

`integrations/skill_generator/vendor/skillos_upstream/` 保存 209 个固定版本文件，含完整 source204、基础 implementation、角色默认配置与训练规格。每个文件逐字节比对 Git 对象，manifest 自身与所有文件 SHA256 在加载前复核；无需运行依赖 `.upstream/` 或工作区 codex。

`integrations/skill_generator/skillos/upstream.py` 选择并编译原始定义，避免执行旧服务器的 CONTROL、绝对路径和进程生命周期。它不是另写一套近似算法：

- 从 source204 的 `collect()` 提取 `rollout()`，只替换 ALF/Web 环境与 Executor 块，并修改该块的原评分说明；后续 SelfJudge、Curator 渲染/采样、编辑、ContentJudge、压缩、位置保存逻辑来自原函数。
- `finalize_group()` 原样编译：8 条 rollout × 10 位置；任务分排除位置 1，取其余有效 SelfJudge 信号均值；辅助奖励在 10 个位置平均；优势为整条奖励减组均值，不除标准差；同一 rollout 所有位置共享优势。
- 使用原 `curator_render.py` 和最终 `curator_protocol.py`，保留原 prompt token cap、删减规则、最终观察与工具封装说明。
- 使用最终 `yaml_reward_toolchain.py`、BM25、`group_window.py`、actor 的 `validate_batch()` / `verify_reward_sources()` 和 `four_rank_math.py`。完整 rollout 等权、rollout 内响应 token 均值的权重规则保持原代码。
- 提示文件来自固定副本 `sources/audit/skillos/curator/prompts.py`；其 SHA256 `a3fd0c8a…` 与上游服务器快照清单中 `runtime/reward-toolchain-v1/source-snapshots/prompts.py` 完全相同，因此这次可追溯到最终 collector 引用的字节身份。

旧服务器的 LOCAL WebShop 特批复核与进程/许可证不可直接迁入新环境。本地使用原基础 content 提示/解析器；不将 WebShop 专用质量适配器或历史许可冒充 Toolathlon 接线。

## 四段接口转换

| 边界 | 转换及约束 |
|---|---|
| TaskSpec → 任务组 | 显式 10 个不同任务 UID、有序 TaskSpec 摘要、8 个采样 seed；正式 32 组，工程诊断 1 组。不会把旧 19 个 A/B 候选自动当成已审查任务组 |
| SkillRepo → API Executor | 原 BM25 检索 top 5；仅检索到的技能进入固定快照，再交给现有商业 API 执行器。每个任务独立环境、独立评分，固定 Executor 身份持续校验 |
| Executor → SkillOS | 工具名称/参数成为 action，实际工具返回成为 observation；完整公开轨迹先不按字符截断，交原 Curator tokenizer 预算逻辑处理。原 evaluator 分数单列；未知分数、空轨迹、基础设施错误阻止组输出。SelfJudge 使用 Work 公共轨迹提示，继续使用上游布尔解析器和预算阶梯 |
| 模型 HTTP → 训练片段 | 原始 completion request/response 单独留存；实际 token IDs、逐 token raw logprob 转换为上游 chat envelope。没有响应 token 重新编码或虚构概率；缺失/不匹配即拒绝，保留 thinking/失败/终止 token 的全 1 mask |

现有 Transformers ContentJudge 服务不支持上游结构约束解码；适配回执明确记录 requested_schema 与 schema_enforced，不假称执行了约束。该部署差异仍需解决或单独验证。当前模型 revision、独立冻结 judges 和角色配置继续来自 `configs/skillos_roles.json`；只启动入口或 HTTP 成功不能证明模型或训练安装身份。

## CPU 启动与诊断

项目根目录运行。默认入口只读文件、核对源码、生成计划，不加载模型、不请求角色服务、不启动 GPU/Docker，也不调用商业 API：

```bash
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/run_skillos_upstream.py \
  --task-set .runtime/pipelines/skill20_20261031_fresh_01/tasks.json \
  --engineering --plan-first-group --run-id skillos_upstream_cpu_plan_20261102
```

`--plan-first-group` 只取已有任务清单前 10 题，明确为诊断，非正式任务课程。1 个完整组意味着计划 80 次独立任务环境尝试，不能解释成只执行 10 题或沿用旧 20 份轨迹。

也可用 `--groups <groups.json>` 提供数组，每组形如：

```json
{
  "group_id": "group-001",
  "ordered_task_ids": ["toolathon-gym:task-1", "其余九个不同的完整UID"],
  "rollout_seeds": [42, 43, 44, 45, 46, 47, 48, 49]
}
```

上例是字段示意，运行时必须恰好 10 个 UID、8 个 seed。`--execute` 仅连接已运行的角色服务并执行工程组，不自行启动服务或 optimizer；正式 32 组仍等待任务来源/测试隔离准入迁移。结果保存在 `.runtime/skillos-upstream/<run_id>/`，完整片段称为 `collected_batch.json`，不冒称已准入的训练封存批。

旧 `scripts/run_skill_pipeline.py --execute` 必须显式带 `--legacy-diagnostic` 才能运行历史在线诊断；旧 `training.markdown_launch --execute` 已阻止启动两位置 optimizer。配置文件保留以便解释历史源码和回归测试，不据此继续旧训练目标。

## 验证和下一步

新增 CPU 检查覆盖真正编译的上游 8×10 循环、技能从前一位置传递至下一位置、首位置任务分排除、失败操作保留、全 token mask、奖励重算、拒绝标准差缩放的优势、完整 rollout 权重、缺失 token 拒绝、未知评分阻止输出和请求不可隐式重放。全部模型输出为明确标记的替身，不能当实际采样、业务成绩或技能收益。

新旧相关回归与只读计划的实际回执见 [source204 接线记录](../../evidence/mainline/skillos_upstream_20261102/receipt.json)。这轮没有 GPU 启动、商业 API 调用、真实任务环境尝试或参数更新。

本轮完整 CPU 回归共 1,162 项，1,102 通过、60 跳过；默认离线计划和项目文档检查均通过。跳过项不作验收通过统计。该结果认证本轮代码/接口检查，不认证原训练生命周期或模型表现。

下一步处理 source204 原 actor/checkpoint/validate/export/install 的旧绝对路径与状态绑定，并将新的 Toolathlon 分组和模型身份接入其校验，保持原 loss/优势/完整状态生命周期；在本地完整接线前不直接调用旧 actor 命令、不绕过旧路径断言。随后再做真实角色协议、完整任务组与连续更新验收。当前不能把“文件已迁入”和“CPU actor 数据校验通过”说成 optimizer 迁移完成。
