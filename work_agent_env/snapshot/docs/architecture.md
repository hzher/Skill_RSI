# 模块与数据流

2026-11-02 H20 代码接线：新增 `training/skillos_h20*.py` 与 `scripts/train_skillos_h20.py`。采集使用四张 H20（Curator 1、SelfJudge 1、ContentJudge 2）；采集排空并停止本项目服务后，原 actor/validation 使用四卡 FSDP/NCCL；原 export 使用四进程 CPU/gloo，安装核对独立 HF 与实际 worker 指纹后才开放下一批。storage/control/run/service 根分开配置，正式批次通过 TaskSpec/课程组/真实采样/原分和 judge/技能 repo/native lineage 逐段绑定。ContentJudge grammar 已有代码，Curator 不加 grammar。当前只通过静态/CPU 协议检查，未启动任何上述 GPU 生命周期；[H20 指南](guides/skillos_h20.md)列明真实运行待验收边界。

2026-11-02 最新架构目标采用固定 SkillOS source204：`TaskSpec → 有序10任务组 → 原8条独立rollout → 原Curator/技能库/复合奖励 → 原中心化优势与actor合同`。`skillos/upstream.py` 从逐字节固定副本加载原定义；`skillos/bridge.py` 负责 Toolathlon/API Executor、公开轨迹、HTTP 采样 envelope 和本地存储转换。配置为 `configs/skillos_upstream.json`，入口 `scripts/run_skillos_upstream.py` 默认只做离线计划。已通过 CPU 替身接口诊断；原 actor/checkpoint/validate/export/install 生命周期尚待本地路径与完整状态迁移，不能直接运行旧服务器 actor。旧两位置原生 verl optimizer 入口已停止执行，下文描述保留为历史接线。详见[上游接线指南](guides/skillos_upstream.md)。

2026-11-02：正式角色配置统一在 `configs/skillos_roles.json`。API Executor 执行任务；独立冻结 Qwen3-8B SelfJudge 判断执行，Qwen3-8B Curator 编辑技能，冻结 Qwen3.8-27B ContentJudge 评价生成内容。`skillos_stream.py` 连接相互隔离、流内顺序的任务流，原生 `markdown_verl.py` 连接 Curator 真采样、独立环境、复合奖励和参数更新；原 evaluator 分数与 SkillOS 复合奖励分别存储，旧确定性 attributor 仅作身份核对。ContentJudge 使用隔离 Transformers 5 环境，训练保留原解释器；Curator 和 SelfJudge 不共享会被更新的实例。三角色权重与真实调用探针已验证；20 题新配置尝试全部被 Executor API 403 GROUP_DELETED 阻断，没有有效业务评分和当轮技能奖励，真实 GRPO 更新仍为 0。流间共享角色限流和 Executor 鉴权失败停止派发信号；已开始的任务正常关闭，未开始任务单列，不伪造失败分。下文小模型部署为历史诊断，接口和适配差异见[三角色指南](guides/skillos_roles.md)。

当前执行链路由任务声明驱动，具体数据集的转换、初始化及评分适配只放在其任务包内。

| 路径 | 当前职责 |
| --- | --- |
| integrations/benchmark_tasks/ | TaskSpec/TaskSet 校验、目录发现、版本快照与注册 |
| integrations/benchmarks/ | 标准任务包、原始题面、工具/初始化与原评分薄适配 |
| integrations/executors/ | 固定模型 API 循环、并发、轨迹和执行回执 |
| integrations/environments/ | 每任务 Docker、独立网络/服务、MCP/命令工具发现调用与关闭 |
| integrations/verifiers/ | 原 evaluator 分发、有效性与统一评分；NeMo 分发辅助接口 |
| integrations/experience/ | SQLite 不可变经验、工具原输出、产物内容、完整评分、usage 与有损视图 |
| integrations/skill_generator/ | 固定 SkillOS 原语、editor 请求与候选、严格 handlers、下游 attributor |
| integrations/skill_library/ | Markdown 内容快照、BM25 检索、候选分支与 CAS 发布 |
| integrations/orchestration/ | 有界异步采样/执行队列、批次 journal、固定 Executor 下游适配 |
| training/ | Markdown 捕获/奖励/完整组封存、算法注册、原生 verl AgentLoop 和权重安装屏障；保留原 skill-v1/NeMo 通道 |

注册将任务包固定到 `.runtime/tasksets/packages/<摘要>/`，TaskSet 存到 `sets/<摘要>/tasks.json`。执行不直接消费可变源目录。批次保存在 `.runtime/executors/<run_id>/`，包含计划、源码快照、逐任务 trajectory.json、receipt.json 与 workspace。模型密钥由宿主 API 客户端使用。

通用 Executor 在开始任务前自动保存源码快照；API 异常保存脱敏正文、请求结构摘要和实际尝试，对连接/超时、429、5xx 最多尝试 3 次同一请求，400 不假定为瞬时故障。OpenAI 函数声明显式 strict=false：当前中转的两组同时对照均为省略时失败、显式 false 时成功；这不是对所有服务或全部 400 根因的推断。逻辑模型轮次与实际 HTTP 尝试分别计数，turn 预算耗尽仍评分已产出的交付物并保留停止原因。数据集专有兼容修复留在任务包：初始化脚本在每题容器的可写层修复已安装工具的格式/方言，并将修改前后 SHA 写入 workspace/.benchmark/runtime_compat.json；包摘要同时绑定修复代码。任务时区由包内环境变量声明，不在通用 Executor 中写具体数据集逻辑。

部分原 benchmark 存在已复现的评分矛盾。任务包的 evaluation_errata.py 为明确列出的题目生成单独标识的修订评分程序；原 evaluator 源文件不修改，结果双报 original_evaluation 与 reviewed_evaluation，附修订说明及两份源码摘要。修订成绩属于新任务包版本，不作为原版 benchmark 成绩。未知的输入/评分矛盾仍应保留待归因，不能仅凭 valid=true、reward=0 判成模型能力失败。

Agent 只挂载公开题面/工具/初始资源；原 evaluator 与参考答案仅挂入评分容器。每题的服务网络与工作区独立，评分读取该题工作区；资源删除后核对关闭结果。异常/超时/评分无法解析保留 reward=null，不能冒充失败分 0。

`toolathon-gym` 是符合相同合同的一个例子，共 503 题。包内 build_package.py 直接转换固定上游，不再依赖已删除的 legacy 或原生 Agent。`docker/toolathlon-api/` 与基础 Dockerfile 仍是当前工具镜像的构建依赖，名称不意味着旧批次调度还在运行。

Executor 结束时将 TaskSpec、receipt、完整 trajectory、tool_calls 原输出和 workspace 产物写入 `.runtime/experience/experience.sqlite3`，经验摘要及默认压缩视图 ID 回填逐题 receipt。历史批次可用 `scripts/skills.py ingest-run` 验证源码快照后导入。完整评分与参考检查仅保留在私有证据，editor 输入只接收原评分摘要；修订成绩不替换原分，未知保持 null。

editor 将公开任务、压缩轨迹和相关技能快照渲染为 SkillOS curator 输入，handlers 在隔离副本执行严格 Markdown 编辑，成功后生成候选。`.runtime/skill_library/skills.sqlite3` 分别记录不可变技能快照、候选来源与发布 head，发布以父 head 比较交换。Executor plan 固定技能全文，`--skill-version` 将指定快照注入固定模型提示；执行身份绑定源码、镜像、模型及预算，与技能版本单独记录。attributor 校验下游任务、评分输入、Executor 和技能摘要，再回传可追溯奖励；该回执不授予训练资格。详见[经验与技能指南](guides/experience_skills.md)。

SkillOS 完整上游克隆在项目 `.upstream/SkillOS-Replication/`，仅作固定来源参考；部署使用 `integrations/skill_generator/vendor/` 内的固定副本。工作区 `codex/` 不参与运行依赖。editor 当前不按成功/失败自动过滤经验，失败结果以原分传递，未知结果保持 null；正式生成前的质量筛选尚未自动化。

真实批次入口 `scripts/run_skill_pipeline.py` 将任务执行、经验和串行技能编辑连接起来；每轮从独立空分支开始，保存确切生成请求与 provider 回复。`transport.py` 对三项已知操作提供显式 `functions.` 别名映射，记录原始与规范化摘要后再严格校验。已完成采样可由 `recover_skill_responses.py` 在原父版本上重验，并将无冲突编辑合并为单独标识的诊断池；该池不作为某次 Generator 的原始输出，不进入训练、不发布 main。

任务网络分配仍使用独占锁和明确子网；外部工作负载不受该锁约束。Docker 网络列出后被外部删除时，保留 inspect 返回的存活网络清单，仅忽略明确的网络已删除错误；权限等其他错误仍退出，空的错误响应则重新枚举。子网碰撞仍由 Docker 创建结果判定并有界重试。

`integrations/orchestration/` 已实现 AsyncPipeline、Journal 和 DownstreamExecutor，分别管理有界采样/执行队列、不可隐式重放的状态和独立环境执行。`training/markdown_verl.py` 接固定 verl 原生采样 token/logprob，将候选执行的原评分作为 reward_score 回传；Manager 完整组封存后交给上游 GRPO/actor 优化器。CheckpointManager 在原生更新及保存后记录权重安装，UpdateGate 确认成功才允许下一步策略采样。GRPO 组内并行异步，更新之间保持零策略滞后屏障。详情见[异步技能训练与 GRPO](guides/async_skill_training.md)。

当前已经验收诊断调度、真实 Docker 分组奖励与固定原生源码接口，尚未验收原生 GPU 运行时、真实 Generator 训练捕获或参数更新。现存 `skill_rsi_*.py`、NeMo resources/agents、`training/` 和 `evaluation/` 保留；Markdown 与旧 declarative skill-v1 没有隐式训练转换。所有更新仍须通过既有训练/评测隔离，并绑定 Generator 自身真实 token/logprob、独立下游执行和奖励。`adapters/toolathlon_native.py` 等仍被 NeMo 资源引用，与删除的原生 503 Agent 是不同组件。

可训练 Generator 部署使用 `.runtime/generator-venv/` 与 `.runtime/models/Qwen2.5-1.5B-Instruct/`，和 API Executor 环境独立；`scripts/generator_python.sh` 为进程设置项目 CUDA 兼容库，宿主驱动不变。训练配置 `configs/generator_h20_smoke.json` 固定 GPU、模型、并发、预算及允许的原生数值/内核调优参数，不能覆盖采样捕获、准入或安装接口。诊断入口 `scripts/check_generator_runtime.py` 可检查 HF 梯度和 vLLM 实际采样；其可选 JSON 结构约束明确只用于诊断，目前未进入原生训练采样，所有这些样本保持 training_admitted=false。详见[部署指南](guides/generator_deployment.md)。

[执行计划](execution_plan.md) · [任务包规范](guides/generic_benchmarks.md)
