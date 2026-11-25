# 在 Codex CLI 中使用 Skill RSI

排查构建失败时，可以让 Codex 先读报错附近几行，需要细节再展开原文。这个功能通过 Skill RSI 提供给 Codex 的 MCP 工具工作。

## 1. 安装到本机

在 macOS 或 Linux 上，先确认 Codex CLI 能正常使用，激活 Python 3.11+ 环境。以下假设收到的 wheel 放在 `~/Downloads`，也可以换成实际路径。

```sh
python -m pip install --upgrade ~/Downloads/skill_rsi-0.2.0-py3-none-any.whl
skill-rsi --version
codex --version
```

## 2. 在项目里试一次长日志

进入目标项目根目录。首次运行 `skill-rsi init --host codex --extension builtin:tool-result-view` 创建配置。默认的 `shadow` 只记录拟采取的动作。本例用 `apply` 实际返回精简日志，在 `skill-rsi.json` 中配置如下。已有配置时合并所需字段。

```json
{
  "host": "codex",
  "mcp_paths": ["skill-rsi-demo.log"],
  "extensions": [{"source": "builtin:tool-result-view", "mode": "apply"}]
}
```

`mcp_paths` 列出允许该工具读取的项目文件。下面创建一份 81 行的演示日志，再启动新会话。

```sh
python - <<'PY'
with open("skill-rsi-demo.log", "x", encoding="utf-8") as log:
    log.write("build step OK\n" * 80 + "ERROR: missing settings.json\n")
PY
skill-rsi setup
codex
```

`setup` 登记项目的 `.codex/hooks.json` 和 `.codex/config.toml`。按 Codex 提示确认项目信任，再在会话中提出这个请求。

> 请用 skill-rsi 的 skill_rsi_read 读取 skill-rsi-demo.log，告诉我失败原因。再用 skill_rsi_expand 展开原文第 1 至 3 行。

日志包默认处理至少 60 行的文本。`skill_rsi_read` 会返回 `ERROR` 附近的行和原文引用，`skill_rsi_expand` 用这个引用取回前 3 行。如果看到了整份日志，先确认 Codex 实际调用了 `skill_rsi_read`。

## 3. 查看结果与更换扩展

在同一项目的终端查看加载状态和动作记录。

```sh
skill-rsi doctor --host codex
skill-rsi project receipts --host codex
```

`"loaded": true` 表示已收到宿主回调。回执中的 `long_text_focused` 表示日志包已选出片段，`delivered` 表示短视图已通过 MCP 返回。

- **换包或停用。** 把外部或框架生成的 ZIP 路径加入 `extensions`，例如 `"./extensions/team.zip"`。要停用某个包，从数组删除该项。改完执行 `skill-rsi setup`，重新打开 Codex。
- **核对旧配置。** 内置的 `constraint-check` 可在同一 MCP 会话再次读取已登记的配置字段时，核对其当前值。用 `constraints` 指定来源和字段，并把源文件加入 `mcp_paths`。
- **记录一次任务。** 支持原生 trace 的 Codex CLI 可用 `skill-rsi collect --host codex --trace --prompt '总结当前项目'`。返回的 `native_trace_status` 表明是否取得轨迹。
