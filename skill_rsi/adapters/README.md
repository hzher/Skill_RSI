# CLI 宿主接入

宿主注册落在用户指定项目与 Skill RSI 状态目录。注册后，`doctor` 根据实际回调报告加载状态并列出已到达的节点。未确认的工具改写或主请求可见性保留为未知。

| 宿主 | 注册位置 | 管理式启动 | 离线导入 |
| --- | --- | --- | --- |
| DSH | 状态目录内的 Cordis patch。可用 `setup` 返回的 `command_prefix` 启动同一 Profile | `dsh --profile sdk`，使用 SDK JSON-RPC 并按显式 session ID 续接 | CL-Bench 批次、SDK stdout、原生 session |
| Claude Code | 项目的 `.claude/settings.local.json` 与 `.mcp.json` | Claude CLI 的 stream-json 和 `--resume` | CLI 事件、原生 transcript、受控 Messages 网关 |
| Codex | 项目的 `.codex/hooks.json` 与 `.codex/config.toml` | Codex CLI 的 `exec --json` 与 `exec resume` | CLI 事件、rollout、schema 2 trace bundle、受控 Responses 网关 |

Codex 由宿主判断项目配置和 hook 信任。Claude Code 由宿主确认项目 MCP。启动后的首个原生回调是加载凭证。对已交付结果的模型输入可见性，使用实际请求记录复核。

`skill-rsi setup --config` 接收以下可选作用域字段。

- `mcp_paths` 列出允许自有 MCP 工具提供的 UTF-8 文件。
- `checkpoint_paths` 列出 Claude Code 压缩前应登记摘要的文件。
- `constraints` 列出待核验的结构化来源。每项包含 `source_path`、`object_id` 和 `field`，可提供 `pointer` 指定 JSON Pointer，省略时使用完整 UTF-8 正文。可选 `target_paths` 把动作目标关联到其独立约束源。
- `profile` 选择 DSH 的 CLI Profile。`command` 允许使用已安装 CLI 的指定路径。

这些路径相对于注册项目。实时探测和工具读取检查项目范围。启用 `attach --mode observe` 后可从 `setup` 或 `doctor` 返回的 `journal` 目录归档实时证据。采集关闭时，运行所需的回执仍在本地私有状态目录保存。

DSH 受控工具视图在 SDK 已标记为主调用的请求副本中生效。原生持久化历史保持原件。工具调用边界提出的约束提示在随后一次主调用交付。托管启动还可指定 `query_source`，在发送本次查询前按已注册的来源核对旧约束，并记录 SDK 接收查询的回执。DSH 托管实验可在独立工作区传 `baseline_tools: true`，开启原件目录、行段读取、聚焦和恢复工具。

Codex 可用 `collect --config` 设置 `trace: true`，显式为该进程配置私有原生 trace 路径。离线读取器使用兼容 CLI 在临时目录运行 trace reducer，并保留原始文件的引用、逻辑请求边界及传输观测缺口。

HTTP 网关按需使用 `skill-rsi gateway --host claude_code --upstream <base-url> --output <private-dir>` 启动。明确改用返回的本地地址后才会收到请求。上游地址需要包含实际 API base，例如尾部的 `/v1`。将已记录的目录按 `messages` 或 `responses` 格式导入。该网关记录 HTTP 正文和发送回执，保留服务端处理范围为未知。

## 多节点 Wasm 策略

使用 `form: middleware` 和 `engine: wasm` 的扩展经过相同的绑定、能力和动作校验。在 PATH 中提供支持资源限制与 WASI 开关的 Wasmtime CLI。打包的 `policy.wasm` 以 WASI 命令形式实现 `decide`。框架通过 stdin 传入一项 JSON 记录，字段为 `abi`、`method`、`event`、`state` 和 `options`。当前 ABI 为 `skill-rsi.middleware-stdio.v1`，方法为 `decide`。stdout 返回一项符合策略接口的 JSON 对象，包含可选的 `reason`、`action` 和 `state`。

运行配置限制指令数、线性内存、输出大小和决策时间。访客只获得 stdio。宿主桥以自己的回执确认动作提交与交付。[Wasmtime CLI](https://docs.wasmtime.dev/cli-options.html) · [Wasmtime 参数实现](https://docs.rs/wasmtime-cli-flags/latest/src/wasmtime_cli_flags/lib.rs.html)
