# 在 Claude Code（CC）中使用 Skill RSI

Skill RSI 可以在 Claude 调用工具后找出长日志里的错误行，也能在压缩会话时记录指定文件的版本。下面先试用日志功能。

## 安装并接入项目

在 macOS 或 Linux 上，先确认本机已能正常使用 `claude`。激活 Python 3.11+ 环境，在目标项目根目录运行。下面假设收到的 wheel 放在 `~/Downloads`。

```sh
python -m pip install --upgrade ~/Downloads/skill_rsi-0.2.0-py3-none-any.whl
skill-rsi --version
skill-rsi init --host claude_code --extension builtin:tool-result-view
skill-rsi setup
claude
```

`init` 仅在首次创建 `skill-rsi.json` 时使用。`setup` 在 `.claude/settings.local.json` 登记工具与会话回调，在 `.mcp.json` 登记可供 Claude 调用的工具。启动新会话时按 Claude 的提示确认项目权限和 MCP 服务。

## 试一次长日志

在 Claude 会话中输入“请用 Bash 执行 `seq 1 80 && echo 'ERROR build timeout'`，告诉我最后的错误”。这条命令成功返回 81 行文本。日志包默认处理至少 60 行的结果，找到第一个错误词后选出附近几行。默认 `shadow` 模式只记录会选中哪段。

在同一项目另开终端查看结果。

```sh
skill-rsi doctor --host claude_code
skill-rsi project receipts --host claude_code
```

`loaded: true` 表示已收到宿主回调。回执中的 `long_text_focused` 表示日志包选中了片段。要让回调向 Claude 提交带原文引用的短视图，把配置里的扩展条目换成下面的对象，再运行 `skill-rsi setup`。如果想先在会话里看到建议，可将模式设为 `suggest`。

```json
{"source": "builtin:tool-result-view", "mode": "apply"}
```

## 其他常用操作

- **压缩前留档。** 在配置顶层加入 `"checkpoint_paths": ["README.md"]` 并运行 `setup`。在 Claude 会话使用 `/compact` 时，压缩前的回调记录该文件的路径、大小和内容摘要。同一会话发出压缩后启动回调时，框架可取回这份记录。
- **核对旧约束。** `builtin:constraint-check` 可在动手前重新核对已记录的配置值，例如接口超时单位。用项目配置的 `constraints` 指定允许核对的文件和字段。
- **导入或停用。** 将收到的 ZIP 路径（例如 `./extensions/team.zip`）加到 `extensions` 数组，或删除要停用的条目，再运行 `skill-rsi setup`。切换在宿主确认的下一次回调生效。
