# Generator 训练与独立评测

## 已合并的下游任务交接

保留的 `training/` 提供采集准入、下游任务身份与 verl 交接接口；`evaluation/` 提供独立来源隔离与评测结果导入。接口与测试不代表实际 Generator rollout、GPU 参数更新或训练效果已经验收。

当前 Markdown 技能路径新增[异步训练与 GRPO](async_skill_training.md)：`training/markdown_launch.py`、`markdown_verl.py` 直接接固定原生 verl，不依赖旧 NeMo recipe；编排、完整组奖励和安装屏障已经诊断验收，真实 GPU 更新仍待训练资源与数据准入。旧 `verl_agent_loop.py` 和 `launch_verl.py` 保留为 declarative skill-v1 通道，不冒充 Markdown 适配。

当前 API Executor 保存的轨迹和原 evaluator 评分可作为经验来源；训练更新仍须来自 Generator 自身的真实 token/logprob 采样，并绑定固定 Executor 的技能执行、下游题面、轨迹和任务奖励。不得用 API usage 或事后重建 logprob 替代。

`training/rollout_handoff.py` 在转换前核对身份、顺序、策略步、episode 和重复 capture；`training/verl_agent_loop.py`、`training/launch_verl.py` 保留后训练接线。`resources_servers/`、`responses_api_agents/`、`integrations/nemo_*.py` 和 RSI 为相关执行服务提供接口。公共 TaskSet verifier 与 NeMo 的桥接位于 `integrations/verifiers/nemo.py`。

所有经验、Generator 采样、固定 Executor 运行与最终评测必须分开绑定版本；不同来源或不同参数更新阶段不能合并伪装为同一批次。外部声明式 skill-v1 接入方式仍见 [Skill RSI 手册](skill_rsi.md)。

[当前主流程](../../README.md) · [执行计划](../execution_plan.md)
