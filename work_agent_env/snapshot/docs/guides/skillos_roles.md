# 本地 SkillOS 三角色与 API Executor

2026-11-02 后续目标已调整：优先复用 SkillOS source204，角色模型部署配置继续使用本指南，主流程与训练算法以[上游接线指南](skillos_upstream.md)为准。本文的旧两位置训练与在线诊断描述是历史接线，不是新目标已完成状态。

2026-11-02 13:25 复查：用户提供的新凭证已更新到权限为 600 的宿主私有文件，确认客户端确实读取该值，但原端点 `cn.affordablerouter.com` 的推理、标准 `/v1/models` 及直连均返回 403 GROUP_DELETED。本次没有再次派发 20 题。需要对应的正确 Base URL 或服务商恢复分组后再做连通性与工具往返检查；[复查回执](../../evidence/mainline/skillos_api_recovery_20261102/receipt.json)不含密钥。

2026-11-02 起，正式配置不再用 Qwen2.5-1.5B。`configs/skillos_roles.json` 固定 SkillOS 来源、模型版本、独立服务、thinking/采样预算与奖励权重。

| 角色 | 模型 | 更新方式 |
| --- | --- | --- |
| Curator | Qwen3-8B，HF `b968826d9c46dd6066d109eabc6255188de91218` | 原生 verl GRPO 更新；诊断流保持初始权重 |
| SelfJudge | 同一初始 Qwen3-8B，独立实例 | 冻结，不随 Curator 更新 |
| ContentJudge | Qwen3.8-27B，HF `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` | 冻结 |
| Executor | 固定商业 API 模型 | 不参与本地更新 |

Qwen3.8 使用 `qwen3_5` 架构，ContentJudge 使用隔离 Transformers 5 环境；原生 verl 保留 torch 2.8 / vLLM 0.11 / Transformers 4.57。新环境通过 `.pth` 在自身依赖之后读取原环境 torch/CUDA，不升级训练环境。两个 tokenizer 已在原解释器中验证可加载，thinking 分隔符不会被默认 decode 丢弃。

## 部署与二十题联调

项目根目录执行，每次使用工作区记录器。下载核对全部权重的固定版本 LFS SHA256，完整成功才写 `deployment_source.json`；分块目录不算完整 checkpoint。`--resolved-direct` 每文件只解析一次公共 CDN URL，再直连范围下载，避免重复请求解析端点；签名链接不打印。

```bash
bash ../codex/run_and_log.sh --artifact .runtime/models/Qwen3-8B/deployment_source.json -- bash scripts/generator_python.sh scripts/download_generator.py --resolved-direct
bash ../codex/run_and_log.sh --artifact .runtime/models/Qwen3.8-27B/deployment_source.json -- bash scripts/generator_python.sh scripts/download_generator.py --repo Qwen/Qwen3.8-27B --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 --output .runtime/models/Qwen3.8-27B --resolved-direct
bash ../codex/run_and_log.sh -- bash scripts/setup_skillos_content.sh
bash ../codex/run_and_log.sh -- bash scripts/generator_python.sh scripts/skill_os_services.py plan
bash ../codex/run_and_log.sh --artifact .runtime/skillos-services -- bash scripts/generator_python.sh scripts/skill_os_services.py start
bash ../codex/run_and_log.sh -- bash scripts/generator_python.sh scripts/skill_os_services.py status
bash ../codex/run_and_log.sh --artifact .runtime/pipelines/<new_run_id> -- bash scripts/generator_python.sh -u scripts/run_skill_pipeline.py --run-id <new_run_id> --count 20 --execute
```

服务仅绑定 localhost，脚本只管理本账号记录的 PID/启动时间，不停止其他 GPU 工作负载。GPU 4/5 规划 Curator/SelfJudge 推理，6/7 规划 ContentJudge，尚需实际显存验收。训练前用 `skill_os_services.py stop --role curator` 停止本项目的诊断 Curator，为 actor/optimizer 准备资源；推理能加载不等于训练能装下。

不带 `--execute` 只列计划。实际运行先核对三角色完整模型及服务 checkpoint 路径；未就绪返回 `blocked_role_preflight`、派发 0 题，不伪造成绩。默认 `--streams 4` 把注册顺序连续分为四个独立技能分支，每流内 A 执行后保存经验和原评分，SelfJudge 判断执行，Curator 根据经验及反馈编辑；B 在独立环境使用本流的新技能，对 A 的候选形成未来奖励，ContentJudge 检查生成内容。流间 API 执行重叠，但三个角色分别共享有界许可，技能快照不串流。每流最后候选无后继奖励，20 题/四流最多形成 16 条奖励边；`--streams 1` 则最多 19 条。注册顺序仅作诊断，不代表正式训练划分。

请求、原回复、操作、快照、原评分和复合奖励分开保存。Qwen 思考文本仅在解析时分离；无效操作保留真实 function-call 分量。未知原评分不更新；缺失 judge/传输错误不冒充负面业务判断。

## verl 接口与适配差异

`training.markdown_launch` 读取同一 roles 配置，核对 Curator checkpoint、恒等采样（temperature=1、top_p=1、top_k=-1、thinking=true）。原生 verl 返回真实 token/logprob；`finish_sample()` 应用候选、调用 API Executor、取得冻结 judges 结果；完整组核对奖励、原评分和身份后才交给 optimizer。权重安装只更新 Curator。预算耗尽的原生 token/失败操作保留，不只训练格式正确的幸存样本。

复合奖励为 `SelfJudge task + function_call + 0.1*content + 0.05*compression`；原 evaluator 分数单列。缺失 ContentJudge 判断阻止封组。当前复用角色模型、解码配置、原提示/handlers/奖励原语，但不声称等价复现原实验：Executor 换为 API，SelfJudge 提示适配 Work 公共轨迹，训练采用两位置经验/下游任务和工程组大小 4；ContentJudge 使用 HF 固定版本，未断言与原 ModelScope 快照字节相同；completion endpoint 的格式解析/预算升级不声称复刻原 ContentJudge 结构约束采样器。配置保留这些差异。

## 任务对如何解决

任务对是当前两位置适配器的数据表达，不是模型额外要求的数据种类。可自动构造任务流，再将边展开为 `(经验 A, 下游任务 B)`，但仍须检查来源和测试隔离。

```bash
bash ../codex/run_and_log.sh --artifact .runtime/training_pairs/<new_proposals>.json -- .runtime/api-executor-venv/bin/python scripts/propose_task_stream.py --execution .runtime/executors/<existing_run>/receipt.json --output .runtime/training_pairs/<new_proposals>.json
bash ../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/prepare_skill_training.py --pairs <reviewed_input.jsonl> --output .runtime/training_pairs/reviewed.jsonl
bash ../codex/run_and_log.sh -- bash scripts/generator_python.sh -m training.markdown_launch --config configs/generator_h20_smoke.json --run-id <new_training_run>
```

候选器按公共业务工具/题面相似度选择非自身、非同题面目标，排除已知留出任务确切身份，绑定经验/目标摘要；可用 `--verified-tree <pinned_metadata.json>` 核对固定 Git tree，并按 prompt blob 排除改名后题面完全相同的测试任务。只读取树/对象身份，不读取测试题面或答案。新在线流的真实 SelfJudge 反馈会一并附到候选。相似度和 blob 不重叠仍不证明家族、别名、派生关系独立；候选保持 `training_admitted=false`，不自动写准入表或 reviewed.jsonl。首批产生 19 条候选，1 条原评分未知经验被排除。

审查后，同一 GRPO 组固定相同经验、父技能和目标任务，各候选用独立复位环境，避免任务难度差异混入优势估计。最终测试任务不参与检索、奖励或选优。

2026-11-02 实际结果：三个模型的完整权重与真实调用探针已通过。历史经验探针生成 3 条合法技能，但首条内容被 ContentJudge 判无效；这不代表整个候选的质量验收。批次 `skillos20_20261102_full_01` 尝试 20 题，全部在首次 Executor 请求收到 403 `GROUP_DELETED`（API Key 所属分组已删除），有效评分、当轮候选、下游奖励均为 0，任务容器全部关闭。三个本地模型服务保留运行。完整回归 1,154 项中 1,094 通过、60 跳过，另有开启 Docker 的 43 项相关检查全部通过；原生 GRPO 更新仍为 0。

已追加 401/403 时停止流间新增任务派发，已开始任务正常收尾，未开始项单列；此修复属于原试跑之后的控制版本，旧试跑回执不改写。继续前须在宿主 `.runtime/private/gpt-api.env` 更新可用凭证，必要时更新端点，不在聊天或终端输出密钥；随后用新 run ID 复跑。19 条任务对候选仍需来源与测试隔离审查。新三角色真实任务奖励链路和 optimizer 更新尚未验收；11 月 1 日小模型结果仅作历史诊断。[本轮正式回执](../../evidence/mainline/skillos_roles_20261102/receipt.json)同时保存试跑源码与后续控制修复的独立身份。
