# Claude Code：在工具执行和恢复时接入策略

仓内参考驱动用压缩前后的 hook 留下快照。Claude Code 的公开工具回调还提供写点，分别适合承载 Part 2 交付的「工具结果按需展开」和「旧约束核验」。首版拟将二者绑定到同一宿主适配器，每个项目单独选择策略包和运行模式。[压缩 hook](../../../../../Skill_RSI_yangyang/framework/skillrl/context_hook.py) · [官方工具回调](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)

## 接入位置

| 位置 | 相邻项目已有能力 | 框架需开发的部分 |
| --- | --- | --- |
| 会话与 hook | `independent` 每题使用隔离会话，`grouped/shuffled` 在一条长链内复用 HOME、session 和 notes。长链的 `PreCompact` 与匹配 `compact` 的 `SessionStart` 将 transcript 和 notes 的字节及摘要写入快照。[采集入口](../../../../../Skill_RSI_yangyang/framework/skillrl/collect.py) · [快照](../../../../../Skill_RSI_yangyang/framework/skillrl/context_hook.py) | 让使用者在所选项目注册 hook，并能移除该组件。历史目录只读导入，监听从注册成功的边界开始。在压缩前基于允许读取的范围生成检查点，用 session、源版本及事件标识去重。 |
| 向恢复后的会话提供记忆 | 参考长链支持本地读写工具；独立模式限制为 benchmark MCP。现有压缩 hook 写快照后返回空对象。[长会话设置](../../../../../Skill_RSI_yangyang/framework/skillrl/continual.py) · [已有运行模式](../../../../../Skill_RSI_yangyang/framework/docs/ROLLOUT_COLLECTION.md) | 在支持并实测过的 CLI 配置上，让恢复 hook 提供检查点摘要，或通过项目启用的 MCP 提供读取工具与版本号。记录 hook 实际返回了什么、工具是否被调用。把文件安装为 Skill 时再验证正文是否实际被读取。 |
| 受配置的模型网关 | `RecordingBridge` 记录进入 Messages 边界的原始请求。配置 Chat 网关时，还保存实际向上游发送的请求、响应和完成状态。[Messages 桥](../../../../../Skill_RSI_yangyang/framework/skillrl/messages.py) · [Chat 网关](../../../../../Skill_RSI_yangyang/framework/skillrl/chat_gateway.py) | 对显式使用该网关的项目，可交付调用前注入检查点的中间件，记录变更前后摘要、版本及关联请求。同一检查点若已通过 hook 交付，这次跳过注入。原生直连模式报告可见的宿主与工具证据。 |

上述本地接入按会话和项目绑定。原 CL-Bench 的题序、Docker 环境、MCP 提交和评分由可选的 benchmark 适配器提供。用户当前项目可把“恢复后首个检查命令是否通过”作为带来源的结果。若没有任务结果，仍可提供可核对的检查点和执行回执。

## 从原型文件生成公共轨迹

下表是主仓适配器的拟议映射。读一段跨压缩会话时，先定位同一会话的快照，再查看后续调用留下的请求和回执。输出路径均相对于公共归档的 `runs/<run_id>/`。[公共轨迹文件树](../../02_轨迹数据契约.md#标准归档的文件树)

| 参考驱动留下的原件 | 对应的公共记录 | 验收时核对 |
|---|---|---|
| `context-events/<时间戳>-<回调名>/event.json`，同目录可有 `transcript.jsonl` 和 `notes/` | `events.jsonl` 记压缩或启动回调。`artifacts.json` 保留快照引用。能确认同一会话的前后边界时，写入 `context_episodes.jsonl`。 | 核对会话、原件字节数和摘要。只有压缩前快照时，恢复与投递状态均记为未知。 |
| Messages 桥的 `requests/<序号>/request.raw`。使用 Chat 网关时，同次网关调用的 `upstream/<序号>/request.messages.raw`、`request.raw`、`forward.json` 和 `receipt.json` 按阶段产生 | `requests/index.jsonl` 分别引用 Messages 入站、Chat 出站与完成回执，并在 `quality.json` 标记缺失层。 | 用入站字节摘要和调用范围对齐桥与网关各自的记录，再核对出站摘要和回执。单有已准备的请求正文时保留“发送未知”。若传输已确认，还需匹配目标主请求和检查点内容，才能认定此版记忆进入了该请求。 |

## 首版如何验证两项宿主能力

- **成功工具结果投影。** 在目标 CLI 上先探测同步 `PostToolUse`。通过后由 `tool-result-view` 保存获准的原件，按该工具原有的输出结构返回 `updatedToolOutput`，其中标明可展开的引用。记录原件标识、返回形状、工具 ID 与展开回执。失败调用、形状不匹配及仅有旧版 MCP 专用字段各有独立探针；保留原生错误信息。该回调发生在工具执行后，修改范围限于交给模型的结果。[官方结果改写协议](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)
- **动作前核验。** `constraint-check` 可在同步 `PreToolUse` 从实际入参识别目标对象和来源；需要提醒时按包模式提供依据，若该版本明示允许参数改写且已验证，再用 `updatedInput` 交付完整的新入参，保留未改字段。核验测试采集实际工具入参及后续结果。来源变化、未变、不可见各跑一次，旧包或旧权限无法执行的动作列为能力缺口。[官方动作前协议](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)
- **单独停用。** 先在 `shadow` 核对提案和原生结果，再以 `apply` 核对实际改写。在同一会话停用展开包时，以适配器确认的生效边界为准，检查下一次新结果恢复原生，核验包继续运行。另一个项目重复相同操作，按其自身绑定检查回执。若该宿主版本需要新会话加载，报告这一边界。[绑定与启停](../../02_轨迹数据契约.md#从发布清单到宿主回执)

## 补充场景：跨压缩恢复

1. 在 `PreCompact` 时为此次压缩记录源 transcript 位置、旧 notes 版本和允许进入检查点的事实，例如“表中耗时字段以毫秒计”。将生成的版本、写入结果和失败原因交给轨迹记录。
2. 在同一项目、同一会话的压缩后启动事件里定位该版本。若只有历史 transcript 或 CLI 未提供这个回调，标明缺口；若版本与会话不匹配，跳过并保留原因。正常场景记录摘要在 hook 返回或 MCP 工具回执中的交付位置。要声称已进入模型请求，需进一步找到关联的出站请求。
3. 追到下一项实际操作。比如工具输入是否仍使用毫秒，是否重复了已做过的字段探查。连接原生 `tool_use.id`、`tool_result.tool_use_id` 与工具 operation ID。多个同参工具调用无法唯一匹配时保留歧义。
4. 开关各跑一个隔离会话，使用相同初始工作目录、notes 和预算。验收包含一次检查点成功交付、一次连续收到重复 hook、一次新会话拒用旧检查点。Part 2 读取完整事件窗口和反例，再据独立验证决定能否向相似任务推荐这一组件。

## 现在可复跑

在 `Skill_RSI_yangyang/framework/` 的 Python 3.11+ 环境执行。前两个命令覆盖会话续接、快照和本地模拟网关；最后一个读取仓内的题单示例。[长链测试](../../../../../Skill_RSI_yangyang/framework/tests/test_continual.py) · [网关测试](../../../../../Skill_RSI_yangyang/framework/tests/test_chat_gateway.py)

```sh
python -m unittest discover -s tests -p 'test_continual.py'
python -m unittest discover -s tests -p 'test_chat_gateway.py'
python -m skillrl.collect --mode independent \
  --config examples/clbench/config.example.json \
  --plan examples/clbench/independent-301.json --check
```

本次复验两组各通过 4 项，题单检查覆盖 301 题，计划 301 个会话，模型调用为 0。真实本地会话加载、检查点内容的可见性及对恢复质量的影响，按前述场景另行验收。
