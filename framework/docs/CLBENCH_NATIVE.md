# Continual Learning Bench 原生Claude Code接线

本入口用于每题全新环境、空技能库的基线。一次Claude Code进程完成该题的原生工具循环，任务动作通过MCP进入官方`task.step`，结束后调用官方`evaluate`。完整请求、响应、工具和原生session均落盘。

## 修正的必要性

此前CL-Bench运行脚本在Git框架之外维护，使用bare模式、外层文本JSON动作解析和同题多次CLI续接。原生文件工具与官方动作处于两个容器，代码题还出现`/workspace`与`/testbed`冲突。恢复期间切换工具集使旧会话继续请求已关闭工具。进程超时时，原先内存缓冲的stdout没有完整落盘。

新入口集中替换这条执行链。旧100题及其恢复分支保留原协议和成绩。修正版本使用独立目录，其验证成本与历史实验分开。

## 执行边界

```text
官方reset及原始题目
  → Claude Code完整默认system、独立HOME
  → 原生MCP benchmark.status / benchmark.act
  → 当前官方Pydantic动作schema校验
  → 官方task.step、实际环境动作及官方预算
  → 原生工具结果进入同一session
  → 题目结束、官方evaluate、原生终态与工作区归档
```

工具能力在所有题中固定为两个MCP工具。`status`提供当前任务和schema，`act`应用一个官方动作。代码题和销售题的CLI位于该题官方容器及其真实工作目录，其他题使用独立干净CLI容器。任务中的shell、数据库等能力仍由官方动作接口提供和计数。CLI内置Bash、Read等关闭，避免产生绕开官方动作预算的第二入口。这是明确的benchmark工具绑定条件。

官方任务的创建、状态读取、动作、评分和关闭在同一专用线程串行执行，HTTP线程只负责传输。真实数据库接线暴露过SQLite跨线程调用错误，原失败和5份完整请求保留。单线程所有权修正保留官方SQLite线程检查，也适用于其他具有线程亲和性的交互环境。

启动没有bare、自写system或resume。保留该CLI版本生成的默认system，同时禁用skills、slash commands、自动记忆、插件配置继承。2.1.160的print模式仍可能出现Claude Agent SDK身份语句，验收同时检查默认Harness、Environment、Context management段和实际工具定义。身份语句本身无法判定bare状态。

原生Stop钩子只核官方完成状态。任务仍未结束时，它让同一CLI继续执行；官方完成或工具层明确中断时允许结束。原64轮与900秒总界限继续生效。该钩子不选择具体动作，不生成答案，也不更改成绩。新增此钩子的实测原因是首个真实接线题在3个官方动作后以thinking-only响应提前结束，旧记录仍保存为未完成。

官方代码/销售容器原先使用`--network=none --rm`。适配器仅在本题创建时将网络改为显式配置的Docker internal网络，并移除自动删除。该网络让容器访问宿主录制桥，外部模型请求由宿主发出。原镜像、仓库、cwd、测试与动作执行函数保留。归档后由官方cleanup清理该题容器。镜像中已有的官方测试补丁也会进入原生默认git status，这是该绑定的可观察条件。

## 配置和启动

安装官方benchmark对应版本及其依赖，固定源码提交、题目索引、任务镜像和Claude二进制。框架主体仍可独立使用标准库，CL-Bench入口使用其官方Python依赖。

```json
{
  "repository": "/data/continual-learning-bench/repository",
  "model": "your-claude-model",
  "base_url": "https://your-provider.example/v1",
  "api_key_env": "ANTHROPIC_API_KEY",
  "cli": "/tools/claude",
  "cli_image": "sha256:your-clean-image-digest",
  "network": "your-internal-control-network",
  "bridge_bind": "172.21.0.1",
  "bridge_advertise": "172.21.0.1",
  "max_turns": 64,
  "timeout": 900
}
```

也可使用`api_key_command` argv从本机凭据助手读取密钥。配置文件只保存助手命令或变量名。上游凭据留在宿主内存，CLI收到本地桥占位凭据。

```sh
python3 -m skillrl.clbench --config /config/clbench.json \
  --family codebase_adaptation --index 15 --output /runs/new-independent-case
```

CLI入口在官方repository下执行，以解析其相对数据路径。输出目录在切换目录前转为绝对路径。每个进程负责一个题目，索引必须与官方canonical映射一致。数据库漂移阶段的独立选题需要同步官方漂移数据库状态，适配器显式保存该原有接线条件。

## 留档与中断

| 文件 | 内容 |
| --- | --- |
| intent.json / initial-task.json | 固定配置、任务与初始schema |
| container-adaptations.json / environment.json | 实际容器、cwd、网络适配参数 |
| native/ | 启动argv条件、原生JSONL、stderr、退出、工具调用/返回、缺口 |
| native-home/ | 本题独立Claude配置目录及session文件 |
| tools/ | 每个MCP操作的持久意图、真实参数、官方反馈 |
| requests/ | 所有Messages入口原始body、请求SHA、原始响应、chunk/event时间、usage、错误及接收状态 |
| workspace-final/ / workspace-capture.json | 官方Docker工作目录及逐文件SHA，包括超时停止后的可复制文件 |
| result.json / failure.json | 实际闭合状态、官方成绩或缺失原因 |

请求在路由、模型和协议门之前留档。原始Messages body直接转发，接收的JSON/SSE字节原样保存。完整finish、usage及message_stop缺失时保留unknown。已知拒绝可按同请求最多三次传输，未决相同请求被拦截并要求效果协调。工具操作在副作用前写intent，相同操作ID只复用已闭结果，未决操作不再次执行。

原生stdout/stderr持续写文件，超时后停止本题容器并保留已收到部分。没有返回的工具调用明确列为缺口。官方未结束时客观结果为null，进程退出0也需要原生终态、工具返回和请求闭合才能接受。SDK在同一进程中的内部恢复仍以完整原始日志为准，框架不生成格式修复提示或改变工具政策。

token/s采用提供商报告的output_tokens与实际交付时间。单chunk、缺usage或过短窗口不生成流式速率。缓冲、隐藏推理和网关行为会影响速率，模型身份仍需提供商审计证据确认。

## 验证

`test_interactive.py`覆盖原生启动、单操作副作用、pending保护、输入验证、真实stdio/HTTP MCP和超时日志。
`test_messages.py`覆盖默认system验收、入口拒绝落盘、逐字节转发和部分响应。
`test_wire.py`覆盖finish/usage闭合、用量合并、混合SSE分隔与单chunk速率缺失。

使用`python3 tests/run_rounds.py --rounds 10`运行完整回归。真实模型与官方环境接线验收单独记录，CPU回归与任务成绩分开汇报。
