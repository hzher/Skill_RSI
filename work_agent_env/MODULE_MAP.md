# 复用边界与源码状态

本表描述交接源码与团队框架的关系，不是“已经融合完成”的证明。共享任务与模型执行保持团队 `framework/` 为共同基线；本提交只增加独立目录。

| 旧模块 | 可复用能力 | 与共享框架的关系 | 当前验证边界 |
| --- | --- | --- | --- |
| `integrations/benchmark_tasks/` | TaskSpec/TaskSet、原始任务身份、包与输入摘要、插件注册 | 可在 `Benchmark.tasks()/prepare()` 处加薄适配，保留原题和题序 | 本次纯合同、静态注册及选定回归；授权任务数据未导出 |
| `integrations/executors/` | API Harness 的上下文、调用、错误、工具分发和轨迹 | 可作为可选 Harness；不应与共享 ToolExecutor 的循环重复叠加 | 本次无模型单元回归；没有真实商业 API 新调用 |
| `integrations/environments/`、`verifiers/` | Docker 资源边界、command/MCP 工具、原评分服务 | 将业务工具暴露给环境接口，保留原工具名、参数、返回和错误；评分留评测域 | 源码可审阅，Docker 实跑未在本次进行 |
| `isolated_gateway.py`、`task_routing.py`、`task_profiles.py` | 任务绑定、资源授权、隔离与配置摘要 | 可作为环境后端，不能只接工具发现而跳过 owner/session 校验 | 私有资源种子和部署配置未分发 |
| `resources_servers/`、`adapters/` | 办公资源启动、工具发现、真实调用、导出和回收 | 各后端分别接共享环境合同；统一环境启动与 Harness 入口仍需单独验收 | 不声称原办公题在共享环境中已经跑通 |
| `model_task_executor.py` | 工具 schema、参数检查、结果与用量记录 | 复用业务合同；模型驱动循环与共享 Executor 择一承担 | 原模块和测试保留；依赖特定服务的诊断需另配 |
| `integrations/experience/` | 经验存储与视图 | 共享 Curator 应消费公开轨迹/产物；不能直接传入旧含评分视图 | 历史经验库和完整轨迹未导出 |
| `skill_library/`、`skill_generator/` | Markdown 版本、检索、编辑和候选 | 共享 Curator/DeltaLibrary/Reviewer 优先复用；来源锁与旧候选仅作接口参考 | SkillOS vendor 不在包内，不能宣称这些路径开箱运行 |
| `integrations/orchestration/`、`training/` | 异步背压、完整组、奖励与版本身份、verl/H20 接口 | 训练侧消费经审阅的版本、轨迹和奖励，不能把普通 API 日志直接视为 GRPO 样本 | 本次仅编译，不启动模型或 GPU，不宣称权重更新成功 |
| `workenv/` | 早期文件/票据/办公诊断辅助接口 | 按具体组件选择复用，不替代当前共享主入口 | 原数据目录未导出；完整原测试集需相应 fixtures |

真实任务的通过判定仍须读取原 verifier：明确原生 pass 才映射到 0/1；没有有效判定时用 null。文件型可对归档副本评分；业务状态型需封存同尝试状态，活会话依赖须单独验证。不得将补充检查、退出码或历史成功率代替原评分。

`file_report` 只是可分发的工程诊断。CL-Bench 仍有自己的原题序、状态协议、Codex 会话和原生评分；本目录既不导入其私有题库，也不把别处运行记录当作本分支验收。
