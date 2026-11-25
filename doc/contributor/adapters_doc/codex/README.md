# Codex 接入：追到记忆进入哪一次决策

适配器首先要回答“正确版本的事实，在出错动作之前到底到了哪一层”。支持原生深度记录的 Codex 优先用 rollout trace 采集；已有的 CLI 历史、API 网关与 MCP 日志可以补录，也能在缺少深度记录时独立使用。API 或账号描述认证方式，每次运行另外登记实际可用的观察点。[诊断场景与指标](../../../../../github_Skill_plan/07_Codex上下文与记忆诊断.md) · [Codex 原生源码、版本与命令](../../../../../github_Skill_plan/08_Codex原生源码取证.md)

## 本机 CLI 与桌面端持续记录（0.4.2）

`skill_rsi/adapters/codex/recording.py` 是统一入口。工作台启动后台同步线程；无界面时由 `skill-rsi record --host codex` 持续同步。默认覆盖本机全部项目，新项目无需单独登记；`--scope project --project /path/to/project` 可显式限制范围。首次启用对现有文件建立 EOF 基线，只记录此后新增的完整行，不补录历史正文。工作台重启沿用游标，明确停用后再启用则跳过停用期。记录器读取登记的 Codex home，不改宿主文件，也不依赖桌面端重新启动。

需要复验某条已结束任务时，可在明确获准后使用 `--import-session THREAD_ID` 单独补录该主会话及已证实的后代。此入口不更改全局起录边界，不补录其他会话，并在元数据中保留单条补录的范围与时间。

每个用户可见的主会话对应一个稳定任务，续接追加到同一目录。已证明父链的子代理和后台线程归入主会话的 `capture/threads/<thread-id>/`，各自保留原文与索引；用户独立派生的会话仍单独记录。由 Skill RSI 启动的托管任务优先按原生 thread ID 关联，避免重复建任务。原文、来源索引和按摘要保存的原件存入任务的 `capture/`，详情与分析器通过公共解析器读取。宿主 hook 的额外事件仍写入 `journal/`。

新任务保存在 `~/SkillRSI/tasks/<宿主>/<来源>/<起录日期>_<主题>_<工作目录名>_<短标识>/`。稳定任务 ID 由私有索引定位，不随主题变化；目录名去除路径字符并限长。`task.json` 保留完整工作目录、原生创建时间、起录范围及来源信息，已有平铺任务可继续读取。

增量游标只推进到完整 JSONL 行。起录前开始的半行整行跳过，起录后的半行等待下一轮；归档移动和文件换代保留起录边界，错误记录保留具体位置。CLI JSON items、Responses 调用及桌面 `event_msg.item_completed` 都进入公共解析器；无法证明关联的工具结果保留未知关联，不按时间猜配。桌面的 reasoning 内容不进入 LLM 提炼投影。

普通 rollout 表明宿主保存过这些事件，不足以证明完整模型输入、物理请求次数或服务端最终输入；这些能力另由原生 trace 或显式网关提供。录制开关与扩展加载回执独立：暂停录制保留已有轨迹，采集成功也不等于扩展已执行。

回归入口：`python -m unittest discover -s tests/adapters/codex -p 'test_*.py'`。界面与 CLI 复验使用合成会话；本机真实会话只作本地验收，不加入仓库或发行包。

## 优先接原生 trace 与 reducer

1. **启动时配置。** 对用户选定的项目和诊断区间，先运行 `codex --version` 和 `codex debug trace-reduce --help`。通过启动器把 `CODEX_ROLLOUT_TRACE_ROOT` 显式传到真正承载 Codex 会话的进程，其值为单独的本地私有目录。普通 shell 中设置变量只影响随后继承它的进程。两个旧启动器均过滤 `CODEX_*` 或含 `CODEX` 的环境键，迁移时要专门传递所选目录，再用实际 bundle 验证。已有运行可以从它先前启用的观察点继续采集。[账号启动器](../../../../../Skill_RSI_chengze/framework/skillrl/codex_host_actor.py) · [API 启动器](../../../../../Skill_RSI_xuyifan/framework/skillrl/codex.py)
2. **导入原件。** 选取目标 `manifest.json` 的 `trace_id / rollout_id / root_thread_id`，保留相邻的 `trace.jsonl` 和它引用的 `payloads/`。使用匹配版本的 `codex debug trace-reduce <bundle>` 生成 `state.json`。原始 payload 可能含提示、工具输出和路径，仅从本地允许的字段生成提炼投影。将原件摘要与 reducer 版本随索引保存。
3. **定位一个决定。** 从 `codex_turns` 找到发生错误的主线程、turn 和后续动作。逐个查看此前 `inference_calls` 的有序 `request_item_ids`，再沿 `raw_request_payload_id` 查工具声明、指令、输入项及顺序。通过 `tool_calls`、`interaction_edges` 区分原始工具返回、子线程收到的结果和父线程实际接到的消息。WS 增量请求需找到可配对的 `previous_response_id` 前缀，再判断事实是否出现。登记这里可核实的逻辑输入及传输状态，保留服务端处理边界。
4. **处理不完整数据。** 导入游标使用源标识、原生 `seq` 与已读前缀摘要。重复导入同一事件保持一个记录，真实的第二次推理尝试或工具操作保留独立身份。合法但未闭合的前缀可得到 `recording_complete=false`；缺失 payload、乱序或损坏应留下具体位置及错误。实时尾部不完整时保存上一个完整游标，等待下一批；文件换代或前缀变更时开启新来源。`recording_complete` 与任务成功各自取证。[原生实现与回归索引](../../../../../github_Skill_plan/08_Codex原生源码取证.md)

### 原生数据怎样进入公共轨迹

原生 reducer 产出 `state.json`。下表是主仓 Codex 读取器的开发约定。每条事实应保留原始事件或 payload 的位置与摘要，便于从归档查回原件。[公共文件树与字段](../../02_轨迹数据契约.md#标准归档的文件树)

| 从哪里取证 | 写到哪一层 | 核对什么 |
|---|---|---|
| `trace.jsonl` 的事件、`tool_calls` 和工具结果 payload | `events.jsonl` 记调用与回执，`artifacts.json` 记原件 | 用线程、原生调用 ID 和有序事件区分并发调用以及重复投递。 |
| 主线程目标 turn 的 `inference_calls`、`request_item_ids` 和请求 payload | `requests/index.jsonl` 记逻辑调用及输入引用 | 能把工具结果连到有序输入项时，标记宿主侧输入已确认。实际发送和服务端最终输入另按相应回执填写，缺证时记录原因。 |
| 切换线程、续接、压缩边界及收尾事件 | `trajectory.ctx.json` 记分支或上下文变化，`context_episodes.jsonl` 关联前后调用，`quality.json` 记覆盖缺口 | 只有压缩标记时，只能定位变化发生的位置。用后续主请求核对目标事实是否保留。 |

例如，离线夹具里 `memory_lookup` 返回 `unit=ms`。只有在同一线程第二次推理的输入项找到相同调用 ID 和正文，才能再记录“工具结果进入了第二次逻辑请求”。[可复跑的原生轨迹夹具](原生轨迹离线验收.md)

## 如何补上原生图之外的事实

| 证据来源 | 接入时应保留什么 | 缺口与开发工作 |
| --- | --- | --- |
| 普通 CLI 与已保存的 session | `thread.started`、`turn.completed/failed`，以及命令、文件修改、MCP 的 item 起止。对指定旧版 rollout，核对 `session_meta.payload.id`、工作区、`event_msg.task_started/task_complete` 中的 turn ID；`response_item` 的调用与返回按 `call_id` 配对。导出格式随版本声明。[格式分类](../../../../../Skill_RSI_hzh/adapters/native_events.py) · [历史续接核验](../../../../../Skill_RSI_chengze/CLbench-Codex/6.1Sol-MAX-stateful/historical/console-20261120-142831-f9ef4b/stateful_recovery.py) | `--ephemeral` 会话可能只有导出的 stdout。只导入用户选定的会话原件，按内容身份核验；文件名、时间接近和同一工作区都不足以配对。记录源不含下一请求时，交付状态保持未知。 |
| 续接、派生与压缩 | 续接记录前后 thread 和 attempt、已核对的边界。fork 记录新 thread、可证实的父 thread 与分叉前缀。子代理 spawn 消息单独记录。普通 rollout 中 `compacted` 或 `event_msg.context_compacted` 可标出压缩边界。[续接实现](../../../../../Skill_RSI_chengze/framework/skillrl/codex_stateful.py) · [事件识别](../../../../../Skill_RSI_hzh/adapters/native_events.py) | 有深度 trace 时再区分 `compaction_requests` 的尝试/返回、`compactions` 中已安装的检查点和下一主请求的输入；普通 marker 只支持“发生边界”。没有分叉点时记录未知，避免重复计算继承前缀或推测哪些事实被保留。 |
| 受配置的 Responses 网关与账号 CLI | API 参考执行器将请求、响应字节及完成/失败回执另存，可追加处理前后摘要。既有账号采集器保存 CLI item、benchmark MCP、broker 操作和任务 outcome，现有协议只开放 `status/act`。[API 记录器](../../../../../Skill_RSI_xuyifan/framework/skillrl/codex.py) · [账号回执](../../../../../Skill_RSI_chengze/framework/skillrl/codex_chatgpt.py) | 对得到的网关请求逐笔核对结果；空目录、孤立请求、未知重试必须单列。在账号模式的旧归档中物理请求正文不可见；新版本能否记录逻辑输入由实际 trace 验证。 |
| 自有 MCP、Skills 与工作区 | 记录生效版本、可用目录、候选排序、选中/跳过原因、操作 ID、写后版本、读回结果。只在受控检索器记录确实公开的候选。记忆服务支持读、写及替换，批准的静态 Skill 也可能已在启动 prompt 中。[MCP 服务](../../../../../Skill_RSI_xuyifan/framework/skillrl/codex_mcp.py) · [Skill 读取](../../../../../Skill_RSI_xuyifan/framework/skillrl/pg01_tools.py) | 新框架需给选定路径的修改补前后摘要或差异、操作关联与冲突回执。原 benchmark 已保留终态文件清单和摘要；缺少前态时只能证明末态。菜单出现、读取成功、来源变更、实际进入请求与后续采用分别举证。[参考快照](../../../../../Skill_RSI_chengze/framework/skillrl/clbench.py) |
| 成本与任务反馈 | 深度 trace 有用量时保留逐调用 input/cached/output 等原始字段、执行时间和作用域；账号 stateful 的 thread 累计量只在同一线程、口径相同且单调时求差。任务结局取原生 outcome，纯本地任务可用指定测试回执、用户纠正或重做。[用量与逐题差分](../../../../../Skill_RSI_chengze/framework/skillrl/codex_stateful.py) | 余量、价格或字段不可见时标缺失。相同工具结果在 CLI、MCP 与 broker 各出现一次，按操作键核对一次副作用，对成本分别记录观测口径。 |

## 两个首版包在 Codex 的交付范围

| 独立策略 | 拟选入口 | 成为可启用版本前的现场检查 |
|---|---|---|
| `tool-result-view` | 先通过自有工具或 MCP 将获准的大结果存原件，向宿主返回短字段和可展开引用。若要把其他原生工具的历史条目投影为短视图，另接能力已验证的主请求组装器或支持完整可重建输入的网关。 | 测一次长结果、一次关键字段位于末尾、一次工具失败。检查原件可按版本取回、短视图进入哪次主请求、停用后新结果恢复原生；只读 trace 配置可参加诊断和回放。 |
| `constraint-check` | 自有工具可核验项目、对象和来源版本。自管 App Server 若允许该连接直接输入，则能用 `thread/inject_items` 向已加载线程追加带来源的更正，不启动用户轮次。 | 在目标决策前检查作用域与写入回执，并追到下一主输入和实际动作。追加不能删除旧项；若版本或入口只提供工具结果，就按工具调用时核验的范围发布。[固定版本与原生用例](../../../../../github_Skill_plan/08_Codex原生源码取证.md) · [官方 App Server](https://developers.openai.com/codex/app-server) |

Part 1 可在两个包均停用时独立采集。试跑只读决定之前的历史前缀，`shadow` 产生的提案留在诊断记录，宿主继续执行其他已启用绑定。在支持写入的隔离项目切到 `apply` 后才比较干预效果。只停用其中一个包时，另一个包的绑定及其他项目的会话各自独立。已追加的历史条目保留原身份，下次生效边界以后的事件不再触发已停用包。[组件发现与验证](../../03_Skill提炼与对照实验.md)

## 合成验收

| 合成场景 | 预期断言 |
| --- | --- |
| v3 中单位为毫秒，v4 修订为秒。续接后提交了旧值 | 自有检索返回 v3 时定位选错版本。若 v4 已在下一主输入而工具仍使用毫秒，定位为输入已交付后的动作错误。去掉请求证据，结论降为“已返回，后续输入未知”。 |
| 在压缩前取到 v4；压缩成功、失败或没有安装回执 | 检查检索返回、压缩结果、安装的检查点、下一主请求逐层首次差异。仅子线程或仅辅助请求可见时，不计入父线程主请求的交付。 |
| fork 继承一段历史，同时有并发同名读取和重复通知 | 绑定分支、call ID 和版本，公共前缀一次、不同分支上的操作各一次。缺少唯一映射的两个结果保持歧义。重导和中断恢复得到相同索引。 |
| 关闭组件的会话在启动时已预载 v4 | 即使未触发 MCP，也能在输入中找到 v4。对照启用后是否减少错误、反复探查及恢复耗时，并报告额外调用。 |

## 现在可复跑

原生合成验收使用本机 `codex-cli 0.169.0-alpha.11.1` 的 `debug trace-reduce` 和 schema v2 的单线程 fixture。带有一条已知事实的输入通过 `request_item_ids` 定位到正文；移去收尾记录后可回放为不完整前缀；再去掉请求 payload 时退出码为 1，报错指向缺失的 payload。三个变体均在临时目录执行，未连接模型。[原生测试入口](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/cli/tests/rollout_trace.rs)

| 参考模块 | 在对应目录执行 | 本次离线结果 |
| --- | --- | --- |
| [CL-Bench Codex 宿主](../../../../../continual-learning-bench/tests/test_codex_system.py) | 在 `continual-learning-bench/` 使用该项目环境运行 `python -m unittest discover -s tests -p 'test_codex_system.py'` | 72 项通过，覆盖选定 session 导出、逐 turn 用量、记忆文件快照、Skill 加载与修复。 |
| [Codex CLM 上下文编辑](../../../../../Skill_RSI_yangyang/framework/companions/codex-clm/tests/test_context.py) | 在 `Skill_RSI_yangyang/framework/companions/codex-clm/` 运行 `python -m unittest discover -s tests -p 'test_context.py'` | 6 项通过，覆盖完整工具组、并发返回及过期编辑。此实现限定完整的无状态 Responses 输入，使用服务端历史的路径需另验。[网关条件](../../../../../Skill_RSI_yangyang/framework/companions/codex-clm/clm/gateway.py) |

先在 `Skill_RSI_xuyifan/framework/` 用 Python 3.11+ 执行下面的最小 MCP 探针。只使用临时目录和合成数据，两次调用应返回同一条记录，`events.jsonl` 中应有两行带不同 ID 的回执。

```sh
probe=$(mktemp -d)
printf '{"tools":[],"events":"%s/events.jsonl","memory":"%s/memory.json"}\n' "$probe" "$probe" > "$probe/mcp.json"
python -m skillrl.codex_mcp "$probe/mcp.json" <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory","arguments":{"action":"add","text":"Synthetic unit: ms"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory","arguments":{"action":"read"}}}
JSON
```

在 `Skill_RSI_chengze/framework/` 执行以下回归。先激活 Python 3.11+ 环境，使测试启动的脚本也用这个版本的 `python3`。该账号测试使用脚本模拟 CLI 登录和动作。[脚本夹具](../../../../../Skill_RSI_chengze/framework/tests/scripted_codex_cli.py) · [原生回执断言](../../../../../Skill_RSI_chengze/framework/tests/test_codex_chatgpt.py) · [stateful 断言](../../../../../Skill_RSI_chengze/framework/tests/test_codex_stateful.py)

```sh
python -m unittest discover -s tests -p 'test_codex_stateful.py'
python -m unittest discover -s tests -p 'test_codex_chatgpt.py'
python -m skillrl.collect --mode stateful \
  --config ../CLbench-Codex/6.1Sol-MAX-stateful/config.example.json \
  --plan ../CLbench-Codex/6.1Sol-MAX-stateful/plan.json --check
```

本次复验 MCP 的临时写入和读取成功，两组账号回归分别通过 6 项和 7 项，题单检查覆盖 301 题、6 个计划会话、0 次模型调用。用户日常项目上的加载、跨会话触发与实际收益属于新组件验收。
