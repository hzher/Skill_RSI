# Claude Code CLM

在阿里云 PAI H20 的独立容器中运行 Claude Code，将可编辑上下文接入原生 Anthropic Messages 请求。Claude 用 Bash、Read、Write 等原生工具保存信息和修改后续上下文。已在 Claude Code 2.1.160 和 Claude Sonnet 4.6 上验证。

## 启动

服务器目录为 `/mount-data/Claude-code-CLM`。用具备现有 PAI Docker 和 Claude API 凭据读取权限的账号启动。每次启动创建独立容器、会话目录和临时网关。

```sh
cd /mount-data/Claude-code-CLM
bin/claude-clm --probe
bin/claude-clm
bin/claude-clm -- -p --allowedTools Bash,Read,Write --max-turns 12 '你的任务'
```

默认每次启动使用新的可写工作目录。如需提供输入文件，在服务器上为该任务创建 `workspaces/<任务名称>`，为容器 UID/GID 1005 提供所需访问权限，然后加 `--workspace /mount-data/Claude-code-CLM/workspaces/<任务名称>`。同一工作目录若需跨会话复用，由操作者显式指定。一次启动包含一个 CLM 会话。

部署依赖同一台服务器现有的 PAI Docker 代理、Python 3.11、已固定摘要的 Python 容器镜像，以及从原生安装复制到 `runtime/claude` 的 Linux x86-64 可执行文件。原生二进制副本 SHA-256 为 `2b84cc2e04633619eab02b9f77ed00a56b64682b4fa7b3267149ee9eb1fecfc7`。源码仓库不分发 Claude Code 二进制或工作区内容。

## 上下文编辑

1. 运行时把当前可编辑的已完成工具交换投影到 `/context/LIVE_CTX_MAIN.json`。原始用户任务、系统提示、工具定义和最新工具交换受保护。
2. Claude 通过 `clm-context status` 查看版本和可编辑条目，用 `clm-context show ID` 读取指定条目。需要精确检索的信息可以先写入工作目录。
3. Claude 通过 `clm-context edit --drop ID [ID ...] [--note '短记忆']` 提交删除和可选的替换记忆。适配器校验当前版本、完整工具调用与结果配对，以及修改后消息 JSON 的字节数。下一次实际模型请求采用通过的改动。随后仍可使用 Read/Grep 从文件检索。
4. 宿主网关转发原生 SSE，记录各轮编辑结果和消息体字节数。模型 API 凭据留在宿主进程。容器仅有 loopback，通过期间专用 Unix socket 访问 Messages，使用只读系统和程序挂载。可写挂载限于当前任务的工作目录、上下文和 Claude 配置目录。

这是依据官方 CLM 的上下文文件机制所做的 Claude Messages 适配。当前允许编辑历史工具交换和简短记忆，接受规则使用序列化字节数。适配器状态随单次启动保存，跨启动恢复需要额外实现。ICL Skill 优化、RL、Suffix Cache Reuse、ContextBench 评分和长期 token/成本收益属于后续验证范围。机制参考 [Context Language Models 论文](https://arxiv.org/abs/2609.37725)。

## 核验

```sh
python3 -m unittest discover -s tests -v
bin/claude-clm --probe
```

服务器上每个会话的 `runs/<session>/messages.jsonl` 记录 HTTP 状态、编辑结果以及重写前后的消息字节数。快照、工作目录与原生会话记录留在该会话目录。真实模型验收使用合成文本，通过原生 Read/Bash 执行读入、落盘、移除旧工具交换和回读。

在 H20 上可运行 `tests/smoke-h20` 复现真实 API 验收。脚本生成独立合成数据，使用预算上限 1 美元和最多 12 轮，并保存实际结果及验证回执。
