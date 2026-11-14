# SkillOS H20 GPU 训练实现与离线检查

2026-11-02：按用户澄清，实现目标是实际调用 H20 GPU 的训练链路；本轮验证只允许编译和简单 CPU 协议检查，不启动 GPU 作业。`scripts/train_skillos_h20.py` 是新入口，`configs/skillos_h20.json` 固定四卡 FSDP/NCCL 及上游训练参数。本文描述代码能力与待验收条件，不代表真实 H20 更新已经成功。

## 已写入代码的 GPU 路径

1. `bootstrap` 核对固定 Qwen3-8B 文件与独立 HF 参数指纹，启动本项目登记的 Curator/SelfJudge/ContentJudge 服务并核对实际载入参数。Curator 与 SelfJudge 各一张 H20，ContentJudge 两张；商业 API 仅作为固定 Executor。
2. `collect` 按已审查的 32 组 × 8 条 rollout × 10 个位置，调用原 SkillOS 多位置循环，保存真实请求、采样、技能库与分量奖励。每次请求检查已绑定服务身份，复用原 request admission，失败后停止新增请求，已发出的请求收尾。
3. `prepare` 核对任务来源/课程组/留出隔离、原始 HTTP 与规范化采样的 IDs/logprob、策略和角色指纹，写入原 actor 所需 batch/application state 和本地不可重放的更新状态。
4. `actor` 通过 `torch.distributed.run --nproc-per-node=4` 调用原 source204 四进程 FSDP actor 主体。保留 FP32 参数与优化器、BF16 forward、AdamW、恒定 1e-6、原 old-logprob 冻结、中心化优势、原完整 rollout 权重、clipping 与一步更新。
5. `validate` 通过另一次四进程 H20/NCCL 调用原 fresh native validation，重新构造模型并加载模型/optimizer/scheduler/RNG，逐 rank 比较保存边界，验证成功才发布 checkpoint。
6. `export` 使用原四进程 CPU/gloo FSDP shard 合并程序导出 BF16 HF 权重，显式设置 `CUDA_VISIBLE_DEVICES=''`；actor/validate 没有 CPU 降级。
7. `install` 启动导出权重的 Curator 和原冻结 judges，比较实际 worker 参数与独立 HF 指纹。更新到 INSTALLED 后，下一批才可使用新策略。

采集和训练分阶段复用同一四卡池；采集结束后先停止属于本项目、PID/starttime 匹配的服务进程组，空闲检查通过才运行四卡 actor/validate。代码不抢占或停止其他工作负载。权重导出后才恢复推理服务。

## 原代码复用与部署适配

`training/skillos_h20_sources.py` 在固定原始字节上做可列出的部署转换：旧数据根/控制根/模型路径改为配置值；actor 的旧 WebShop 作业前置约束改接本地版本/完整批次/父状态检查；validation/export 的 argv 改接不可变 context。actor 从 rank 初始化到 optimizer、checkpoint 保存的主体未重写，校验/合并主体保留。

额外从同一固定上游源码对象归档还原 8 个原文件，位于 `integrations/skill_generator/vendor/skillos_h20_runtime/`，包括 SP62 完整存储守卫、预算核算及指纹程序。保留原 300 GB 活跃数据上限、200 GiB 空闲保留、最多两个 native full states，以及原每 shard 写入容量限制。固定副本不修改；每个编译产物记录原文件摘要与实际转换点。

原 vLLM 指纹扩展针对 0.19；本地 worker 适配器明确针对既有 0.11.0，沿用原 fused QKV/gate-up → HF 参数映射，调整 worker load_model 签名及版本绑定。需后续真实 GPU 验证兼容性，不能由静态编译推断已通过。

ContentJudge 的实际 GPU generate 路径已增加 xgrammar JSON schema masking，在原生 thinking 的 `</think>` 之后启用；Curator 不添加 grammar，保持原采样分布。旧服务进程不会因文件改动自动获得该能力，必须后续显式重启并实测。HTTP 转换会拒绝未实际确认 schema enforcement 的旧 ContentJudge 响应。依赖 `xgrammar==0.1.25` 已写入隔离 ContentJudge 的依赖声明，本轮没有安装/升级或启动服务。

## 现在可运行的离线检查

项目根目录执行，以下命令不启动训练或模型服务，不探测当前 GPU 空闲情况、不分配 CUDA 内存：

```bash
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/train_skillos_h20.py --run-id h20_plan --action plan
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/train_skillos_h20.py --run-id h20_compile --action compile
```

`compile` 编译实际部署变换后的原程序及 GPU worker 语法，但不导入 torch/vLLM/Transformers，不执行 actor、save/load、导出或服务启动。代码默认四卡物理 ID 为 `[4,5,6,7]`，仅为可配置计划，未声称这些卡空闲或已分配。实际 actor 与 validate 会检查 H20 型号、显存和占用。

## 后续真实调用顺序（本轮不执行）

所有可能启动模型/GPU/环境或更新状态的动作均需要显式 `--execute`；没有该参数只打印计划。后续在 H20 机器使用项目 GPU 解释器执行：

```bash
# 后续真实阶段示意；不是本轮已执行命令。
bash scripts/generator_python.sh scripts/train_skillos_h20.py --run-id <fresh_run> --action bootstrap --task-set <tasks.json> --execute
bash scripts/generator_python.sh scripts/train_skillos_h20.py --run-id <fresh_run> --action collect --admission <reviewed_groups.json> --execute
bash scripts/generator_python.sh scripts/train_skillos_h20.py --run-id <fresh_run> --action prepare --collection <collection_dir> --admission <reviewed_groups.json> --execute
bash scripts/generator_python.sh scripts/train_skillos_h20.py --run-id <fresh_run> --action update --context <context.json> --execute
```

上述真实命令也必须通过工作区 `codex/run_and_log.sh`，并用 `--artifact` 保存相应回执目录。`update` 顺序执行 actor → validate → export → install，也可用对应 action 分阶段执行。中断后只允许从已确认阶段推进；SENT/UNKNOWN 不自动重试 optimizer。代码不把 actor step、保存成功、服务在线分别冒充完整安装成功。

每一步都保存原始采样/来源、完整技能库、父 checkpoint、更新 UUID、角色身份及 context/source 摘要。第一步之后只允许从已安装的同 world-size=4 完整 native checkpoint 继续，不仅加载 HF 权重重置 optimizer。前两个 checkpoint 可保留；第三步之前需要显式 `retire --execute`，复用原 retention_plan，只退役同一 run 较旧的工作 checkpoint/配对 export，先保存元数据/哈希并检查活动读者，保留最新已安装完整状态；不触碰任务证据、其他 run、partial 或最终归档。

## 正式任务组输入

`scripts/prepare_skillos_groups.py` 可将明确的 32 组 TaskSpec/seed 清单转换成审查材料；不会编造家族/祖先信息，不写训练准入表。它也不会把旧 19 个 A/B 候选或“前十题”当作正式课程。

```bash
bash ../codex/run_and_log.sh --artifact <proposal.json> -- .runtime/api-executor-venv/bin/python scripts/prepare_skillos_groups.py \
  --task-set <tasks.json> --groups <groups.json> --run-id <fresh_run> --output <proposal.json>
```

正式 admission 必须绑定 TaskSet、32 个有序任务组、每组 8 seeds、evaluation manifest 和经过家族/别名/祖先审查的 seed registry。当前 `training/seed_registry.json` 仍无准入条目；这属于尚未完成的数据审查，不会由入口自动批准。

## 当前证据与仍缺的信息

本轮只做静态/CPU 协议检查。真实 GPU 作业、模型服务启动、商业 API 请求、真实任务环境尝试及 optimizer 更新均为 0。测试范围和源码身份见[H20 代码检查回执](../../evidence/mainline/skillos_h20_code_20261102/receipt.json)。真实四卡容量、FSDP save/load、vLLM worker 适配、ContentJudge grammar 与新权重安装效果均待后续 GPU 验收。

本轮 55 项相关检查中 54 通过、1 项默认容器检查跳过；API 解释器与实际 GPU 训练解释器均完成不导入模型运行时的源码编译检查，update 默认只打印计划，项目文档检查通过。没有用 CPU 替身结果冒充实际 GPU 训练或 checkpoint 恢复。

尚待确认：商业 API 的最终 Base URL/模型；同机四张完整 H20 的可用卡号；checkpoint 数据盘路径与空间。配置均可调整，这些信息不阻止本轮写 GPU 代码。私有密钥继续由宿主私有文件读取，不进入聊天、命令参数或回执。另需完成正式任务分组与留出隔离审查。
