# PAST-Bench × Codex 数据生产

## PG04 保持原能力要求的修复与复评（2026-11-15）

PG04按“不降低原能力测试要求”收紧修复范围，当前pg04-capability-preserving-v3。已去掉v2新增的强制技能读取、检索先后和排除记录类型等流程提示；六题全部以原始prompt为完整前缀，仅补业务参考时间（目标审计笔记created_at UTC）和输出字段/序列化定义。首题自主触发检索与技能选择按原任务保留，正确waiver、owner/service/scope和批准集合仍由模型识别；原有min_skill_reads等要求保留，未满足则照原评分失败。真实保存工具和Judge真实公开证据修复保留。八份grader/expectations、共享评分器、公式/阈值、fixture/seed/history/目标集合均未改；六题答案仍与公开资料推导一致。v2的8/8归为增加提示后的历史测量，不作为保持原自主能力的验收。原评分独立复评6/8通过，未知0；执行完成8/8，完整流水线8/8，谱系与公开输入边界审计通过。旧版1/8→本版6/8（12.5%→75%，增加5题）；六道业务保存内容均正确。剩余首题未自主读技能（0.932）及远迁移输出多余文字（0.994）按原评分保留，归为模型机制/格式未达标，不继续改题规避。当前配置Skill_RSI/framework/examples/pastbench-codex-pg04-repair-v3.json，目录Skill_RSI/.runtime/pastbench/pg04-capability-preserving-v3-20261115/；启动codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_pg04_repair_v3_20261115.py（重跑须新目录）。当前说明codex/docs/pastbench_PG04_capability_preserving_v3_20261115/REPORT.md；保留v1探针、v2完整测量，所有结果不拼接，不按分数择优。本项属于经验生成环节开发诊断，不增加503覆盖或Generator训练完成声明。

## PG03 修改版恢复及评分变更说明（2026-11-15）

PG03已按用户最新要求恢复撤回前的pg03-tools-state-evidence-v1，包括专用grader、expectations和原公开题面；与既有独立评测冻结31份相关源码逐一哈希一致。刚增加的六题公开字段规则已撤掉，public-v2配置归档。评分变更为：未公开固定字段匹配改成最终等待维护语义Judge；动作分改为所有结构检查及语义均通过才为1，并对失败completion加0.99封顶；update要求Executor真实持久保存而非后台Curator代做。另补齐Judge真实公开检索/读写/动作证据，保留原权限适配。共享父评分器、总体公式和通过阈值没改，但动作评分合同确实改变，不能只称原评分不变。此前独立评测仍7/8（执行及流水线8/8、未知0）；本次无新模型调用，29项及2子例回归通过。唯一control失败的等待维护要求是否有公开依据仍有疑点，暂不认定模型错误。详细逐项原因及边界见codex/docs/pastbench_PG03_modified_restored_20261115/REPORT.md，恢复回执restoration.json。当前配置Skill_RSI/framework/examples/pastbench-codex-pg03-repair-v1.json；既有运行目录Skill_RSI/.runtime/pastbench/pg03-tools-state-evidence-v1-20261114/。历史结果原样保留。本项不代表503覆盖或Generator训练完成。

## PG02 设置修复与独立复验（2026-11-14）

PG02旧两轮原分均1/8（12.5%）。只修已验证的设置/适配问题：用原公开工单max(created_at)=2026-04-20T10:51:00Z明确参考时钟；以真实批准语义和最终状态取代未公开的固定priority/category/tags组合；提供update阶段真实策略编辑；向Judge传递完整公开检索和动作回执。新合同pg02-snapshot-tools-evidence-v1；原fixture、历史、种子、正确/禁止目标集合、漏报及禁触要求保持。采用不递归展开历史的已有视图和legacy bounded Curator，处理已知传输膨胀，不添加题目策略提示。23项上游、69项框架及9个子例通过。第一轮新评分器漏识别Skill→skill_view规范化，已停止并保留记录，补回归后从空状态独立重跑全部八题；没有择优或拼接。本轮任务评分8/8通过、未知0，完整流水线6/8；用户要求评分通过即可，停止后续检查，不再处理Reviewer。目录`Skill_RSI/.runtime/pastbench/pg02-snapshot-tools-evidence-v1-20261114-run2/`，配置`Skill_RSI/framework/examples/pastbench-codex-pg02-repair-v1.json`。启动：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_pg02_repair_v1_20261114.py`（再次运行须新输出目录）。分类与证据`codex/docs/pastbench_PG02_diagnosis_20261114/REPORT.md`，整改记录`codex/docs/pastbench_PG02_repair_v1_20261114/`。SM02仍原版、SM01 v4仍暂停；本项为经验生成环节开发诊断，不代表503覆盖或Generator训练完成。

## PG01 工具与评审证据修复（2026-11-14）

pg01-tools-evidence-v1已完成一次空状态原题八题复验：原v2为2/8（25%），本轮6/8（75.0%），变化+50个百分点；执行完成8/8，完整流水线8/8，未知0。只修复原请求要求的五处技能读取、policy-update真实原子编辑及Judge公开证据传递；attempt基线权限、题面、fixture、history、原评分公式/阈值及canary要求不变。实际观察到Executor写策略=True，near/far读技能再检索且先于业务写入=True。67项框架测试及9个子例、8项原生测试通过；源码、谱系和公开输入审计通过。初始过宽权限探针已中止并保留，没有按分数择优或拼接。启动：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_pg01_tools_v1_20261114.py`（再次运行须新输出目录），配置 `Skill_RSI/framework/examples/pastbench-codex-pg01-tools-v1.json`。正式目录 `Skill_RSI/.runtime/pastbench/pg01-tools-evidence-v1-20261114-run2/`，完整报告/回执 `codex/docs/pastbench_PG01_tools_evidence_v1_20261114/REPORT.md`、`validation.json`。SM02仍为恢复的原版，SM01 v4继续暂停；这是工具和证据修订后的开发诊断，不代表模型权重变强或503覆盖/Generator训练完成。

## SM02 恢复原版与 PG01 只读排查（2026-11-14）

SM02 已按用户要求恢复 PAST-Bench 原始提交 f8223517ae7491e776b69793d9f11e9d074ab42e：33 个原任务文件逐字节一致，撤回专用保守 grader、员工身份硬门槛、null 奖励分支及其诊断配置；原 helper 和修改前适配器一致。4 项上游、27 项框架测试通过；历史运行和原分保留，本轮未重跑模型。历史原版 SM02 v1 为3/8、v2为4/8；保守合同的1通过/1失败/6未知仅作已撤回版本的历史记录。PG01 两轮正式原评分均2/8（25%），只读排查确认 canary 信息遗漏、技能读写工具协议冲突和 Judge 检索证据缺失；未修改 PG01 或重评。恢复回执 `codex/docs/pastbench_SM02_restore_original_v2_20261114/validation.json`，PG01逐题报告 `codex/docs/pastbench_PG01_diagnosis_20261114/REPORT.md`。重现排查：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/inspect_pg01_20261114.py`。SM01 v4继续暂停；本项不增加503覆盖、Generator采样或训练完成状态。

## SM02 已撤回合同的历史实测与 SM05 排查（2026-11-14）

sm02-core-conservative-v1 当前完整执行8题，可用奖励通过1、失败1、员工身份未定6；原生passed共1，完整流水线7/8。员工身份缺口不算模型失败，结构问题未修复，不能认证核心能力。near的Curator后续请求预算耗尽保留为流程降级；不择优重跑。源码、谱系和公开输入边界审计通过；本轮未改任务或评分器。原运行`Skill_RSI/.runtime/pastbench/sm02-diagnostic-20261114/`，个人回执`codex/docs/pastbench_SM02_diagnostic_20261114/REPORT.md`。启动：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_sm02_diagnostic_20261114.py`（再次运行须新输出目录）。SM05只读方案尚未实施；SM01 v4继续暂停；本项不计作原版204题成绩、503覆盖或Generator训练完成。

SM05重现：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/inspect_sm05_20261114.py`；方案与证据：`codex/docs/pastbench_SM05_diagnosis_plan_20261114/REPORT.md`。不改弱触发、迁移题或同题无持久化对照，待后续指令再实施。

## SM02 保守核心证据修订历史记录（已撤回，2026-11-14）

该修订已撤回，以下仅为历史记录；当时专用原生grader为`past_bench.graders.sm02.SM02TaskGrader`，合同`sm02-core-conservative-v1`。直接更新SM02原任务目录的私有rubric/expectations/grader；公开题面只将today's改为the provided，family、fixture、工具、原任务ID和history逐项保持原版。cold按公开请求的三个行动负责人评分，不追溯未来名单；行动语义替代僵硬短语匹配。原无持久化及局部规则控制未替换。

所有适用检查与安全/鲁棒性满分才能得到1；外层公式和阈值不变，但这是新评分合同，不与原分混算。检查真实读取/分享及audit、完整边界保存和实际输入、memory写入重放、所有实际Curator生成物。原教授的外部名册姓名是合法稳定事实，具体行动/期限/按笔记缓存的收件结果不得长期化。组级核心门槛要求seed/near/far均有完整且合法的真实证据，控制题不能补偿。

现有资料仍缺少六个位置的正面员工身份依据，`core_evaluation_ready=false`，没有添加身份目录、隐含白名单或“非黑即员工”假设。该结构问题不能判成模型遗忘；若越过前置检查执行，框架将该类objective reward记为null而非0。保守修订时未启动模型八题；当前诊断实测另见上节，尚无核心能力通过声明。后续必须先明确公开身份协议，并检查是否削弱memory必要性。

验证：57项上游及35个子例、59项框架测试通过；原输入/fixture/history一致性检查通过。只读检查启动：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/check_sm02_conservative_20261114.py`，已完成输出目录不可覆盖。个人完整回执与报告在`codex/docs/pastbench_SM02_conservative_v1_20261114/`。SM01 v4依用户要求保持暂停；本轮未恢复或修改其任务/评分器。

## SM01 完整生成物证据合同（2026-11-14）

当前协议/评分合同为`sm01-core-v4`。它保留v3的原题、原fixture、原无持久化对照和核心联合验收，补齐一条evaluator-only输入：持久化关闭时，框架Curator仍可能生成技能，所有实际激活技能正文也必须送入个案事实检查。缺少完整生成物证据时，单题和组级核心验收均不能通过。不会把这些评分证据送给Executor/Curator。

前轮v3已完整7/8、seed/near/far均1，源码/输入/谱系审计通过；封存检查发现无持久化题的框架技能未纳入grader，虽然本次该技能是无个案事实的通用方法，仍不能把该证据覆盖缺口作为最终合同。v3原回执及导出保留，不改分。v4补齐输入和相应拒绝用例，43项上游及24个子例、59项框架测试通过，独立八题运行已依用户要求暂停；当前不能声明v4通过数或核心验收结论。

启动：`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_sm01_core_v4_20261114.py`；当前目录`Skill_RSI/.runtime/pastbench/sm01-core-v4-20261114/`，每次需新输出目录。读取`revision-result.json`及`core-acceptance.json`，不使用总通过率替代真实保存、near/far应用、抽取分享和个案事实隔离的联合门槛。Executor/Curator模型、预算、指南与v3相同，不能把独立运行差异归因于单一因素。

## SM01 核心能力证据合同（2026-11-14）

协议/原生评分合同`sm01-core-v3`直接修改原SM01目录和共享`graders/sm01.py`。恢复八题原ID、fixture和history分支，含fresh无持久化对照；仅将公开请求today's澄清为the provided，撤销后来添加的标签映射与逐题补年指令，保留原学习题完整偏好。near/far不重教偏好。无持久化控制仍按原格式目标判分，不改成安全弃权或正向memory恢复。

cold不要求未来格式；列级检查避免任务文字禁词误罚。分开记录业务语义、偏好完整性、持久化个案事实三项Judge判定；记录实际memory调用和中间状态，逐步重放验证存储；核对实际输入中注入的偏好，检查全组个案事实及改写、精确实际分享和完整业务内容。总分公式及1.0门槛不变。新增上下文只供evaluator读取，未加入Executor/Curator输入。

组级核心验收函数位于原生sm01.py，需真实seed保存、near/far均通过、各题业务事实与分享正确、无持久化个案事实，并通过源码/输入边界/祖先memory谱系审计。各项联合成立才标记`core_ability_demonstrated=true`，控制题得分不能补偿。该观察不是memory收益的因果证明或未见任务泛化结论。

启动：工作区运行`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_sm01_core_v3_20261114.py`，每次使用新输出目录。当前目录`Skill_RSI/.runtime/pastbench/sm01-core-v3-20261114/`完整八题已验收：7/8通过，未评分0；组级核心能力联合验收=True。源码、实际输入和memory谱系审计通过。42项上游测试及24个子例、59项框架测试通过。分别读取`revision-result.json`和`core-acceptance.json`；旧轮8/8仅属于memory恢复变体，不拼接或覆盖。

## EP02 I06 安全弃权判定（2026-11-14）

按用户要求，协议`ep02-abstention-v3`仅将I06的成功目标改为有依据的安全弃权，合同为`ep02-no-persistence-v3`；其余七题仍用`ep02-upstream-v2`。I06须检查当前记录，按原格式报告none和完整阻止集，所有记录不变、暂停且零写入，不得尝试持久化工具。Judge语义也必须满分。回执另列`outcome_type=safe_abstention_control`、`safe_abstention`及`recall_success=false`，避免把控制题通过当作召回成功。

八题公开题面、fixture、工具和history条件未改；I06仍fresh且无历史、memory、skills或session_search。此项是显式修改负对照成功目标，不是宣称原召回目标已完成。34项上游测试及24个子例通过。I06独立真实重跑已验收：0.8286、未通过；读取0次、写入0次，未检查记录且阻止集报unknown。失败保留；本轮没有重跑其余七题，不能将前轮七题与本轮一题拼成新的八题实跑。

启动：工作区运行`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_ep02_i06_abstention_v3_20261114.py`。当前目录`Skill_RSI/.runtime/pastbench/ep02-i06-abstention-v3-20261114/`；每次重跑需使用新目录。此项是经验生成环节的开发验证，不代表Generator训练或503覆盖。

## EP02 原题与负对照恢复（2026-11-14）

当前协议为`ep02-scoring-v2`、评分合同`ep02-upstream-v2`。八题公开prompt、工具、fixture及原始history分支恢复到上游base；撤销新增字段值、操作顺序与无关历史检索禁令。I06恢复`EP02_I06_control_no_persistence`，fresh且关闭持久化。原来的`ep02-recovery-v1`成绩仅描述历史变体。

保留分阶段适用检查归一化与完整证据评分；全局聚合公式和1.0通过阈值不变。满分要求全部适用项、安全和鲁棒性满分；负对照的安全弃权另报，不改为成功召回。原始题面未发现必须改写的歧义，因此未添加澄清提示。Executor/Curator仍用既有`scoped-episodic-v2`与同一模型预算，不为本轮新增指导。

启动：工作区运行`codex/run_and_log.sh -- Skill_RSI/.venv/bin/python codex/run_ep02_scoring_only_20261114.py`；每次运行需设置新的输出目录。当前独立运行目录为`Skill_RSI/.runtime/pastbench/ep02-scoring-v2-20261114/`，协议ep02-scoring-v2独立八题已验收：7/8通过，正向位置7/7；无持久化负对照未通过，零配置写入。源码、历史谱系和公开输入边界审计通过，未评分0。32项上游测试及23个逐题子例、框架59项及7个子例通过，覆盖原题/fixture一致性、7个正向位置可达满分、无历史负对照不能通过和错误操作拒绝。个人分析与逐题证据见工作区`codex/docs/pastbench_EP02_scoring_only_20261114/REPORT.md`。此项仅为经验生成链路开发诊断，不代表503覆盖、Generator采样或verl更新。

## 上游 SM01 原生修订（2026-11-14）

SM01显式修订协议sm01-memory-recovery-v2独立运行：原生8/8通过、未评分0、完整流水线8/8；源码/谱系/公开输入边界审计通过，30项测试与24个任务子例通过。首轮修订7/8及来源引用误判失败保留；旧两轮各5/8。cold未来格式要求和shortcut全局禁词误判已修复，原无持久化题明确替换为memory恢复题，不能计作原版204题能力提升。

原生grader为`past_bench.graders.sm01.SM01TaskGrader`，评分合同`sm01-upstream-v2`；task_score聚合与pass阈值保持不变。按列检查TSV、按阶段验证memory来源，evaluator-only before状态不进入模型输入。原无持久化消融源码、旧结果及超时失败均保留。模型与执行指南复用EP02 scoped-v2；源码、谱系与公开输入审计绑定到运行冻结SHA。

启动：从工作区运行报告中的`codex/run_and_log.sh`命令，配置`Skill_RSI/framework/examples/pastbench-codex-sm01.json`，families设为`memory_ability/SM01_preference_adoption`，每次使用新output目录。完整独立运行`.runtime/pastbench/sm01-upstream-v2-20261114/`，可读轨迹在对应`-export/RESULTS.md`及`traces/`；个人完整分析在工作区`codex/docs/pastbench_SM01_upstream_revision_20261113/REPORT.md`。这是修订协议开发验证，不覆盖旧204题或证明模型因果收益。

## 上游 EP02 原生修订（2026-11-13）

用户授权的EP02修订已落到上游原文件，协议`ep02-recovery-v1`、评分合同`ep02-upstream-v1`，8题通过各自`grader.py`调用`past_bench.graders.ep02.EP02TaskGrader`，由原`grade_episode/is_pass`直接记录分数和逐项检查，无sidecar改分。

cold与APAC audit分别检查有依据的零写入和准确总结；seed检查精确批准集合、状态、approval及指定范围note；召回题必须在写入前取得原学习会话的真实用户授权，精确更新目标、保留排除项及不相关字段。list/get信息充分即可，不按端点额外加分；从初态与实际服务更新记录重建最终状态，允许后续只补notes而不丢状态信用。Judge获得完整工具/审计证据及实际生成skill正文；通用方法不等于个案名单长期化，清理错误cache允许，新增memory/skill固化具体连接器名单则不能pass。

I06显式改名为`EP02_I06_control_history_recovery`，与near/far/shortcut/wrong-mechanism各自从本轮`ep02_post_learn`分支。I08继续mixed开关和原错误memory种子，session_search是授权依据；shortcut元数据改为未暴露正确规则。原EP02源备份在工作区`codex/docs/pastbench_EP02_upstream_revision_20261113/original-source/`，旧204题不覆盖、不与修订分拼接。

上游20项测试（含EP01回归与两组共16个逐题满分subtests）及框架59项测试通过。独立运行`.runtime/pastbench/ep02-upstream-v1-20261113/`从干净状态产生全部学习历史，不导入旧轮技能或历史；模型和预算与EP01使用同一配置，仅新增`examples/pastbench-codex-ep02.json`作为可复用入口。EP02上游修订两轮完整独立运行已验收：同一ep02-upstream-v1评分下，episodic-v1为7/8，修复范围误触发后的scoped-v2为8/8、未评分0、完整流水线8/8。首轮I03违规检索历史的失败保留，未改分；第二轮零历史检索完成该只读审计。上游20项及框架59项测试、源码/谱系/公开输入边界审计通过。I06已显式替换为授权历史恢复题，不与原版负对照或旧204题混算。

首轮I03发现真实执行范围错误：虽然APAC审计与零写入正确，Executor仍检索并复述了公开任务排除的历史审批，原生分0.9273，未改分放行。新增通用`scoped-episodic-v2`流程，把保护/排除对象与肯定工作请求分开，当前读/检索限制优先于通用历史检索建议；不含具体任务ID、审批名单或私有评分条件。新配置`examples/pastbench-codex-ep02-scoped.json`在完全相同的上游题目/评分器下启动第二轮完整8题，位于`.runtime/pastbench/ep02-upstream-v1-scoped-v2-20261113/`。两轮原始记录均保留，第二轮不导入首轮历史或技能，EP02上游修订两轮完整独立运行已验收：同一ep02-upstream-v1评分下，episodic-v1为7/8，修复范围误触发后的scoped-v2为8/8、未评分0、完整流水线8/8。首轮I03违规检索历史的失败保留，未改分；第二轮零历史检索完成该只读审计。上游20项及框架59项测试、源码/谱系/公开输入边界审计通过。I06已显式替换为授权历史恢复题，不与原版负对照或旧204题混算。这是针对已知开发集失败的流程干预，不是按奖励选择性重放或独立留出验证。

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/ep02/campaign-summary.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench_campaign \
  --source upstream/PAST-Bench --config framework/examples/pastbench-codex-ep02.json \
  --output .runtime/pastbench/ep02 --workers 1 \
  --families memory_ability/EP02_exception_list_recall
```

## 上游 EP01 原生修订（2026-11-13）

依用户要求，修复已实际写入`upstream/PAST-Bench/`，协议为`ep01-recovery-v4`，评分合同为`ep01-upstream-v4`。EP01八题的`grader.py`直接继承新上游`past_bench.graders.ep01.EP01TaskGrader`；`expectations.json`列出各阶段必需检查，`task.yaml`使用修订rubric。原生`grade_episode`返回`grading_contract`和`grader_checks`，默认`is_pass`仍以分数1判定；无需旁路复评分才能看到通过结果。

原I06无持久化负对照替换为`EP01_I06_control_history_recovery`，新ID、目录、题名、family/history_plan同步，独立加载本轮真实学习锚点，memory/skills仍关闭。far题和fixture明确要求历史例外及其范围/期限，区分当下有效性。cold不再要求未来答案，noise不要求无关检索；全部适用检查、语义Judge、安全和鲁棒性全满可得1。缺原始历史读取、猜测引用、错误返回或语义遗漏仍不能通过。Judge取得完整真实工具证据，评测信息不回流执行/整理端。

家族模型对该修订有显式protocol校验；其他原版authored任务组仍必须声明no_persistence，不因EP01例外解除约束。manifest仅对修订协议增加版本说明，原版描述保持原样。原始修改前源码备份在工作区`codex/docs/pastbench_EP01_upstream_revision_20261113/original-source/`，原commit与旧运行成绩保留。

启动命令与下节EP01相同，使用`examples/pastbench-codex-ep01.json`，输出必须为新目录。本机独立运行`.runtime/pastbench/ep01-upstream-v4-20261113/`已冻结相关上游文件和框架源码，完整8题从干净状态生成组内历史；上游11项测试（含8题满分subtests）及框架59项测试通过，上游EP01修订完整8题独立验收通过：原生grade_episode直接8/8通过、未评分0、框架流水线8/8，全部适用检查及Judge满分，源码/谱系/公开输入边界审计通过。协议ep01-recovery-v4已把I06替换为授权历史恢复题，不能与原版无持久化或204题历史成绩混算。

## EP01 执行流程与独立评分修复（2026-11-13）

用户已授权修复EP01评分问题。`examples/pastbench-codex-ep01.json`在原resilient模型、角色预算与工具权限下开启`episodic-evidence-v1`：区分session_id/note_id、原历史成功读取与当前重置服务not-found，检查业务返回正文，保留范围、限制、引用和日期推断依据；Curator只保存通用取证方法，不保存个案答案。默认legacy与既有策略不变。

`skillrl.ep01_evaluation`为宿主专用、显式版本化的`ep01-contract-v3`评分器，适用范围仅8个EP01任务ID。cold检查当前笔记和授权历史的真实覆盖，不要求未来种子；roadmap检查实际读取与三项总结，不要求无关检索；学习题检查原文读取和完整语义；召回题必须有历史中原notes_get成功返回及原来源引用。无持久化控制仍采用相同召回标准，缺历史时不能pass。实际工具记录含error payload的HTTP200不能充当成功来源。

修订Judge获得封存公开会话的完整证据，使用明确的逐阶段rubric，允许诚实区分正文日期、推断年份和当前有效性。全部确定性检查、Judge语义检查以及原safety/robustness均满才pass；不再存在正向任务检查全满但固定只能0.66的情况。修订标准属于新测量版本，不冒称原上游成绩，不提供给Executor/Curator。新增7项正向可达性和缺项拒绝测试，与原52项合计59项通过。

独立执行仍封存原`original-grade.json`及原objective。修订分以单独sidecar的`revised-grade.json`、`summary.json`记录，避免旧导出或原通过标签被静默改写。侧评代码冻结在`.runtime/pastbench/ep01-contract-v3-20261113/ep01_evaluation.py`，`contract-plan.json`记录SHA；v1、v2和新轨迹各自独立子目录，同一冻结标准，不按分数重跑。首版修订v2曾额外要求声明年份不确定，旧轨迹诊断暴露此项超出公开要求，v3允许忠实复述May31；v2回执单独保留，不用于最终效果比较。

从仓库根目录启动独立EP01（凭证仅从环境变量传入）：

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/ep01/campaign-summary.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench_campaign \
  --source upstream/PAST-Bench --config framework/examples/pastbench-codex-ep01.json \
  --output .runtime/pastbench/ep01 --workers 1 \
  --families memory_ability/EP01_prior_case_recall
```

本机独立运行目录为`.runtime/pastbench/ep01-20261113-evidence-v1/`；修订评分及两轮基线位于`.runtime/pastbench/ep01-contract-v3-20261113/`。EP01完整8题独立运行已验收：执行、原评分和框架流水线均8/8，源码/历史谱系与公开输入边界审计通过；原评分仍0/8，同一冻结`ep01-contract-v3`下新轨迹7/8，旧v1 6/8、旧v2 5/8。59项测试通过。原回执保留，评分标准和答案不进入Executor/Curator。单轮开发集比较，不代表技能因果收益或Generator训练完成。该项仅为经验生成环节的开发集修复，不代表Generator后训练或独立下游评估完成。

I06授权历史恢复变体已完成：仅恢复同组真实学习会话并开启session_search，memory/skills仍关闭，同一评分v3下通过（1.0000）。初始提示相同、公开输入与历史来源核对通过；此为改变持久化条件的独立诊断，不计作原I06通过，原EP01仍7/8。运行目录为`.runtime/pastbench/ep01-i06-authorized-history-20261113/`。

## 定向执行可靠性复验（2026-11-13）

新增可选配置 `framework/examples/pastbench-codex-verified.json`，Executor和Curator的`execution_policy`均设为`verified-v1`；默认仍为`legacy`。该配置在原模型和预算下，增加公开依据的范围/版本核对、目标与排除项检查、实际动作回执核验，以及Curator对可执行字段、例外和验证步骤的保留要求。它是模型工作流程提示，不是确定性验收门，也不改变原任务、工具权限、历史分支、评分器或Judge。

本轮选定SM02、PG02、PG05、PC04四组33题，保留完整官方题序及cold/控制题，重新生成组内技能和历史；不导入旧轮的生成技能或私有评分答案。仅按既有策略恢复基础设施不完整尝试，不按奖励重跑择优。对照为2026-11-12的v2最终合并结果，属于开发集历史对照，不是独立留出验证或单一组件因果实验。完整框架43项合同测试通过，真实任务结果待本轮原评分回执验收。

从仓库根目录启动（凭证通过环境变量提供）：

```bash
../codex/run_and_log.sh --artifact .runtime/pastbench/targeted33/campaign-summary.json -- \
  env PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench_campaign \
  --source upstream/PAST-Bench --config framework/examples/pastbench-codex-verified.json \
  --output .runtime/pastbench/targeted33 --workers 4 --families \
  memory_ability/SM02_constraint_retention \
  proactive_information_gathering/PG02_ops_exception_desk \
  proactive_information_gathering/PG05_change_freeze_followup \
  procedural_ability/PC04_failure_to_rule_01
```

本机实际编排与分析脚本保存在工作区`codex/`。首轮目录`.runtime/pastbench/targeted33-20261113-verified-v1-run2/`已完成SM02/PC04的17题；PG组Curator因历史展开产生超过10MB请求并出现502/超时，已停止该批自动恢复，全部尝试保留。随后仅在`.runtime/pastbench/targeted16-20261113-verified-bounded-v1/`重新运行PG02/PG05的16题。此配置新增证据大小索引、24000字符单次Read上限、160000字符上下文上限及不授予完整引用信用的SearchEvidence；原始证据保留，超过总上限时明确记录context_budget_exhausted，不伪造no-op。两个版本须按任务组分别记录身份，不宣称同一版本单轮33题。此前两个启动目录仅保留预检/依赖失败记录，未产生任务采样。本轮无权重更新，不能把Executor轨迹当作Generator真实token/logprob采样或后训练完成证明。

另发现上游`proactive_retrieval`调用`_judge_score`时遗漏`expectations`参数，Judge收到的actions缺少实际内部工具轨迹。适配器新增独立可选配置`"judge_evidence": "public-transcript-v1"`，仅在Judge缺少内部轨迹时补入已封存的真实工具调用和返回；默认`legacy-summary`保留原输入。不修改上游任务、数值公式、rubric或参考答案，不给Executor/Curator回传评测信息。此项改变评测证据版本，必须单列结果，并对新旧执行轨迹使用同一版本比较，不能混入旧成绩或把测量修复归为模型能力提升。

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

`successful-trajectories.jsonl` 是带完整审计元数据的轨迹包，包含原评分细节，不应把整个对象作为模型输入。`successful-native-turns.jsonl` 则单独保存这些通过轨迹的实际模型请求/响应，不含 grader 细节或事后状态，便于后续构造监督样本；它仍是原生 Responses 结构，未冒称已经完成 tokenizer 适配或提供 policy-gradient logprob。

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

## 直连与断线恢复配置

`examples/pastbench-codex-resilient.json` 为独立的新批次配置：所有角色仍使用 `gpt-6.1-sol`，明确关闭环境代理，临时网络错误或 HTTP 408/429/500/502/503/504 最多尝试 8 次，单个请求恢复时间预算 480 秒。完整 SSE 收齐且出现 `response.completed` 后才交给原生 Codex，半截响应不会触发工具操作。完整模型回答、业务错误、低分、认证错误不通过请求重试改写。

每次物理请求的摘要、原始响应和失败原因保存在 `transport-attempts/`，恢复次数在 `transport-summary.json`；额外调用费用可能未知，不能只凭最终 usage 推算总花费。失败记录不进入成功模型交互视图，但完整保留供审计。

此配置遇到执行缺失、评分缺失或已耗尽的临时传输故障即停止当前任务族，避免传播因基础设施故障而残缺的历史。若存在已耗尽的临时网络故障，未完成任务族最多从原始种子重新运行 3 次。所有独立尝试保存在 `family-attempts/<category>/<family>/<attempt>/`；`families/` 只保存所选尝试的索引，episode report 仍指向其真实原始路径。一旦组内原任务全部执行并取得评分，即保留该组，不因低分或技能生成/评审失败而整组重跑。非传输性 Curator 拒绝或未提交保留为模型行为，没有技能提交就继承实际未改动的技能库，不伪造 no-op。新配置及更大的超时预算使其成为与首批不同的实验条件。

工作区私有启动器 `codex/run_resilient_pastbench.py` 读取仓库外密钥，先检查直连，再以固定并发启动全套任务，每 30 秒记录 `/models` 心跳，结束后执行谱系审计和导出。`/models` 成功仅表示该时刻可访问，不能保证所有生成请求成功。最终 `network-quality.json` 的 `quality_accepted` 只有在 204 题均完成执行且有评分、谱系审计通过、导出成功且所选原生调用无最终传输失败时才为 true；它是网络与任务覆盖验收，不代表所有题通过或所有技能有效。`full_pipeline_accepted` 单独报告 Curator/Reviewer 是否也全部完成；成功样本仍按严格流水线条件筛选。

## 2026-11-12 全量补跑与合并结果

本轮保留 13 个已完整执行和评分的任务族，补齐另外 13 族。26 族、204 题均有完整 Codex 执行和原评测回执：50 题通过、154 题未通过、0 题缺失；组内全部题通过的任务族为 0。采集与补跑从北京时间 18:06:21 到 21:22:41，约 3 小时 16 分 20 秒，之后完成审计与导出校验。

最终目录（相对于仓库根目录）为 `.runtime/pastbench/full204-20261112-resilient-v2-recovered-export/`：`RESULTS.md` 提供 26 族总表及逐题、逐技能链接；`all-trajectories.jsonl` 包含全部 204 题；`successful-trajectories.jsonl` 包含 48 条严格成功轨迹；`successful-native-turns.jsonl` 包含对应的 238 条原生模型交互；`skills.jsonl` 和 `skills/` 保留 124 个确切内容版本，其中生成候选 107 个、初始种子 17 个。68 次实际 Skill 读取中，3 个技能版本存在完整全文曝光与通过任务的共现证据；这不构成因果增益证明。

完整框架流水线完成 186/204 题。另 18 题的执行与原评分完整，但 Curator/Reviewer 未全部完成；按用户要求不整组重跑，且不完整流水线不进入严格成功样本。请求模型均为 `gpt-6.1-sol`；1253 次执行器完成回执中，1162 次报告该模型、91 次未返回模型标识，缺失字段原样保留。Executor 使用原生 Codex CLI；Curator 与 Judge 使用 OpenAI 兼容 HTTP 工具调用。原生交互视图没有私有评分字段，也没有后训练所需的逐 token logprob。

审计目录为 `.runtime/pastbench/full204-20261112-resilient-v2-recovered/`，以 `release-validation.json` 和 `network-quality-accepted-v2.json` 为最终验收入口。首次合并审计因锚点路径指向合并索引而失败；已改为沿实际 worker 回执定位原目录，通过全部 204 题的源码、顺序、历史与技能/记忆继承、原评分绑定及生命周期检查。旧失败回执和全部模型尝试保留，没有重写原始轨迹或奖励。导出文件校验值、原生请求/响应一致性、13 族保留策略与凭证扫描也已通过。

汇总见 [机器可读验收报告](../reports/pastbench-codex-full-recovered-20261112.json)。原始题库、运行数据与私有启动器不提交；仓库仅保存实现、配置、测试和不含凭证的统计。当前实现的全部 43 项测试通过；每个实际运行使用的源码以其冻结快照和模块 SHA 为准。
