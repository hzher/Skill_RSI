# 两套原生CLI可编辑上下文框架

源码来自聊天“Claude code CLM”和“Codex CLM”的独立仓库交付。各自在自己的目录运行，避免两个同名`clm`模块互相覆盖。逐文件来源SHA与提交见[UPSTREAM_SNAPSHOTS.json](../UPSTREAM_SNAPSHOTS.json)。

| 目录 | 原生执行器与协议 | 源版本 | 历史验证 |
| --- | --- | --- | --- |
| [claude-code-clm](claude-code-clm/README.md) | Claude Code 2.1.160，Anthropic Messages | `96e6cde` | Read/Bash归档、删除旧工具交换、随后回读的实际接线证据 |
| [codex-clm](codex-clm/README.md) | Codex CLI 0.169.0，stateless Responses | `806b573` | 7次完整Responses、6次成功shell调用、编辑生效及回读 |

Claude版让模型通过`clm-context status/show/edit`提交编辑，网关按工具ID校验完整旧工具交换，投影到随后Messages。Codex版按`call_id`处理完整工具组，投影到随后Responses input。原生本地记录保留原历史，模型实际看见的出站输入另行记录。

模型决定要删除哪些允许编辑的内容，最新工具组、任务与高优先级指令受保护。接受门要求版本匹配、调用返回配对及JSON字节严格缩短。字节缩短与任务收益分别评价。

## 本地测试

```sh
cd framework/companions/claude-code-clm
python3 -m unittest discover -s tests -v
```

```sh
cd framework/companions/codex-clm
python3 -m unittest discover -s tests -v
```

## 部署入口

Claude版为`bin/claude-clm`，Codex版为`bin/codex-clm`。原H20的Docker代理、固定镜像、UID和二进制布局保留在各自`clm/run.py`与README。迁移环境时按实际部署调整这些参数，并核验原生工具、权限和网络。Claude版从宿主环境读取`ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY`，Codex版读取`OPENAI_BASE_URL/OPENAI_API_KEY`，两者均把真实凭据留在宿主。

Linux CLI程序按原版本单独准备，二进制来源与SHA见各README。`--probe`会创建实际隔离容器，`tests/smoke-h20`会调用真实API，运行前需配置对应环境和预算。

同名聊天后续100题实验使用了额外的benchmark桥接、恢复或容量维护策略，其结果、会话谱系与完整轨迹在[Claude code CLM数据归档](https://github.com/sheepyedu/Skil-RSI-Repository/tree/main/Claude%20code%20CLM)与[Codex CLM数据归档](https://github.com/sheepyedu/Skil-RSI-Repository/tree/main/Codex%20CLM)。这些实验变体及其成绩与这里字节保持的独立CLI源码版本分开记录。
