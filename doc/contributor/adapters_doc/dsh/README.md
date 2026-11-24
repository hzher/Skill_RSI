# DSH：试跑工具视图与旧约束核验

DSH（DeepSeek Harness）是首版两种策略的首个实验宿主。现有 Node 插件可以为特定 query 建立可逆的上下文视图，Python 驱动也有 query 前策略入口。Part 1 从这两处记录机会和动作；Part 2 拟把从历史中发现的「工具结果按需展开」和「旧约束核验」各发成一个可独立启停的包。[会话驱动](../../../../../Skill_RSI_hzh/dsh_system.py) · [原生插件](../../../../../Skill_RSI_hzh/plugins/clbench-submit/index.mjs)

## 接入位置和实现状态

| 位置 | 相邻项目已有能力 | 框架需开发的部分 |
| --- | --- | --- |
| 用户已有的 DSH 记录 | `evidence.dsh` 能从支持的批次读回会话、调用、记忆快照和原生评分。带来源的 `SkillRSI-CTX-v1` 可作兼容基线。[DSH reader](../../../../../Skill_RSI_hzh/evidence/dsh.py) | 允许指定合法的本地会话目录、格式版本与任务标识。缺少 benchmark 时保留宿主事件和用户给出的完成信号，注明评分来源。旧版本缺少的字段保留为未知。 |
| 正在执行的任务 | `DSHSystem.respond` 在首次发送本次 query 之前调用策略。现有 `MemoryPolicy` 消费公开题面、此前反馈与当前记忆，能写入、取回、压缩或跳过。策略服务超时会按原任务继续。插件订阅 `llm/stream`、`tools/execute` 和 `tools/result`。[策略回执](../../../../../Skill_RSI_hzh/memory_policy.py) · [调用与工具记录](../../../../../Skill_RSI_hzh/plugins/clbench-submit/index.mjs) | 拆出可在用户项目启用的插件入口，按能力登记是否有调用前回调、是否能改变本次输入。将 CL-Bench 的 `clbench_submit`、checkpoint 和 scorer 留在 benchmark 接口。未装载插件的会话可先从历史记录导入，新事件的实时介入从宿主实际完成注册后开始。 |
| 装载冻结组件 | 两个特定任务已有 `SkillGate.decide/submitted/observed` 规则。驱动将启用状态交给插件，在当前 query 的主调用上加载固定 Markdown，并检查辅助调用无意外暴露。[触发规则](../../../../../Skill_RSI_hzh/skill_gate.py) · [加载验证](../../../../../Skill_RSI_hzh/skill_loading_pilot.py) | 将规则和文本打包发布，给出版本、适用事件、开关及卸载方式。绑定用户工作区与会话，记录每次命中、跳过和回退，支持换回已知版本。 |

迁移时固定所选源文件的摘要。参考仓的 `abbf143` 提交之外还有本地修改。现有 `MemoryPolicy` 会从相邻训练项目导入动作定义，框架需要把运行时所需的动作与校验独立出来，使基础插件可在没有训练环境的机器上工作。[来源记录](../../../../../Skill_RSI_hzh/SOURCE_PROVENANCE.json) · [策略实现](../../../../../Skill_RSI_hzh/memory_policy.py)

## 从一次请求查回 DSH 原件

在 Poker 批次的 `native/<任务>/<运行时 ID>/` 中，`request-<调用 ID>.json` 保存 SDK 选项。转换后，在本次运行的 `requests/index.jsonl` 按 `logical_call_id` 找到该调用，沿 `request_ref` 核对原件，再用 `source_event_ids` 找到 `events.jsonl` 里的相关事件。此时证据层是 `native_options`。若 `actual_context_ref` 为空，对“这条记忆是否进入模型最终输入”应记为未知。批次和运行目录的完整布局见[标准归档的文件树](../../02_轨迹数据契约.md#标准归档的文件树)。

## 首版要接的两个写点

| 策略包 | 可复用的原语 | 接入后怎样验收 |
|---|---|---|
| `tool-result-view` | 参考插件按 query 匹配可公开的历史片段，在 SDK 请求选项上投影；`ctx_focus` 按原始行号聚焦，`ctx_restore` 可逐字恢复。原型已测试版本冲突及压缩时回到原生视图。[视图代码](../../../../../Skill_RSI_hzh/plugins/clbench-submit/context-view.mjs) · [编辑代码](../../../../../Skill_RSI_hzh/plugins/clbench-submit/context-edit.mjs) | 开发工具原件保存、项目绑定和策略触发，验证目标工具与主调用有相应读写能力。在 `shadow` 看到拟聚焦的行而保持原始输出；启用后引用可展开。停用时后续结果走原生路径，已交付且在保留期内的引用仍可读。 |
| `constraint-check` | query 前驱动和已获准的工具读取可供核对目标对象及来源修订。若要承诺每个工具动作前都生效，还需在对应工具边界接入并取得回执。[策略参考](../../../../../Skill_RSI_hzh/memory_policy.py) | 仅装此包也能核验修订后的相关约束。展开包同时开启时，核验优先用可访问原件；输入不变、无关字段更新和版本未知各自检查。 |

DSH 首批归档有 100 次执行、20 个独立实例、465 次主任务模型调用，观察到的压缩为 0。它可供选取重复读取样本；压缩和跨修订恢复的发布试验要补采确实遇到这些边界的任务。在新的项目上进行工具视图四组对照以及两包分别开关的组合实验。[归档汇总](../../../../../trajectory-lab/reports/clbench-dsh-100/summary.json) · [对照设计](../../03_Skill提炼与对照实验.md)

## 核验包的两步夹具

用两步合成任务验收。第一次工具反馈公开了字段单位，策略据此写入带来源的记忆。下一次查询需要核对这个字段时，策略取回对应版本，在调用前把笔记交给宿主。以下是新适配器的通过条件。

1. 以 session、query 和原生调用 ID 连接触发事件、组件摘要、选择的动作和执行回执。首次写入的版本应与下一次取回的前置版本一致。对重复的通知复用回执；已经发出但没有结果的写入，先核对现有版本。
2. 区分宿主拼出的提示、插件看到的 SDK options 和实际发出的请求。取回成功、进入本次可观测输入、后续操作使用了正确单位，三项分别举证。工具侧成功和任务原生得分也各自留源。
3. 用相同初态另跑关闭组件的会话。检验门控关闭和辅助调用的记录，再比较任务结果、额外读取及调用成本。补一例策略超时，确认任务继续且当次记忆未被改变。Part 2 可消费这组成功、未触发与回退的样例，发布范围取决于独立验证。

## 现在可复跑

在 `Skill_RSI_hzh/` 使用该项目支持的 Node 环境执行下列命令。14 个参考用例覆盖查找、聚焦、原文恢复、旧视图写入冲突和压缩边界回退。独立扩展包的安装、试跑和项目效果属于待实现的验收。

```sh
node --test plugins/clbench-submit/context-find.test.mjs plugins/clbench-submit/context-edit.test.mjs plugins/clbench-submit/context-view.test.mjs
```

在 `Skill_RSI_hzh/`、已装参考仓及 CL-Bench 依赖的 Python 环境执行。本机用 Python 3.13。这个批次使用来源引用，再用原批次核验字节。[归档入口](../../../../../Skill_RSI_hzh/evidence/__main__.py) · [历史 Poker 批次](../../../../../Skill_RSI_hzh/results/dsh-poker-q1)

```sh
archive_dir=$(mktemp -d)
python -m evidence build results/dsh-poker-q1 --harness dsh --output "$archive_dir"
python -m evidence verify "$archive_dir" --batch-dir results/dsh-poker-q1
python -m unittest discover -s tests -p 'test_memory_policy.py'
python -m unittest discover -s tests -p 'test_skill_loading_pilot.py'
```

本次复验 7 个 run 的引用完整，含 6 个完成、1 个出错。45 次调用均有请求载荷引用，已核实的最终模型输入为 0。两组回归分别通过 4 项和 5 项。首个新框架版本仍需在一个真实的用户长任务中验证安装、触发、关闭和效果。[策略用例](../../../../../Skill_RSI_hzh/tests/test_memory_policy.py) · [加载用例](../../../../../Skill_RSI_hzh/tests/test_skill_loading_pilot.py)
