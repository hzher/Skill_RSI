# 通用任务包注册、执行与评分

当前主入口是 `scripts/benchmark_tasks.py`，支持 `list/register/test/run`。通用模块不导入任何具体数据集适配器。旧原生调度与 legacy 兼容实现已删除。

## 目录和存储

```text
integrations/benchmarks/<名称>/                  # 放入标准化数据集，无需修改通用 Python 代码
  manifest.json
  smoke.json                       # 可选：运行测试的工具调用与预期评分
  tools/                           # 工具适配程序，可选
  assets/                          # 初始资源，可选
  tasks/<任务ID>/
    task_config.json
    docs/task.md
    docs/agent_system_prompt.md     # 可选
    evaluation/main.py             # 数据集原 evaluator 或薄适配器

.runtime/tasksets/
  registrations/<名称>.json         # 注册回执，指向当前版本
  packages/<包摘要>/                # 完整任务包快照
  sets/<任务集摘要>/tasks.json       # 标准 TaskSet
.runtime/executors/<run_id>/        # 执行计划、输入快照和批次回执
  <任务摘要>/
    workspace/                     # 每任务独立工作区
    trajectory.json                # 模型消息、调用、工具结果和 API usage
    receipt.json                   # 原 evaluator 统一评分、镜像身份、资源关闭结果
.runtime/package-tests/<ID>/        # 无模型运行测试，与正式任务结果分开
```

注册会拒绝包内符号链接，记录完整包文件摘要并复制资源。源码变动再次注册会形成新版本；旧快照保留。执行前重新校验快照与 TaskSpec，拒绝篡改。注册回执中的 task_set/package 就是数据实际保存位置。任务子集也保存独立清单。

## 标准任务包

可复制的完整例子：`integrations/benchmarks/file_report/`。目录名必须与 manifest 的 benchmark 一致；改成新名字即可成为另一个数据集。

```json
{
  "schema_version": "workagent-task-package-v1",
  "benchmark": "your_dataset",
  "revision": "v1",
  "environment": {
    "backend": "docker",
    "config": {
      "image": "your-prepared-image:version",
      "network_access": false,
      "mounts": [{"source": "tools", "target": "/assets/tools"}],
      "tools": [{
        "name": "write_file",
        "description": "Write a file in the task workspace",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        "argv": ["python3", "/assets/tools/write_file.py"]
      }]
    }
  },
  "evaluation": {
    "backend": "command_json",
    "config": {
      "argv": ["python3", "/assets/evaluation/main.py"],
      "mounts": [{"source": "tasks/task1/evaluation", "target": "/assets/evaluation"}],
      "timeout": 180
    }
  },
  "tasks": ["task1"]
}
```

每题 task_config.json：

```json
{"needed_mcp_servers": [], "needed_local_tools": ["write_file"], "meta": {}}
```

可在 task_config.json 中用 environment/evaluation 覆盖清单默认值，例如选择不同题目的评分目录。题面从 docs/task.md 原文进入模型用户消息；system prompt 可选，通用执行器补充 `/workspace` 工作目录说明。

环境镜像预先包含所需解释器和工具依赖。默认不构建、不拉取镜像、不在模型执行过程中安装依赖；批次启动前把全部已安装镜像标签解析为固定 ID。样例使用 `bench-runtime:python`，最小镜像可从 `docker/runtime/` 构建。当前本机测试复用了已安装镜像并登记实际 ID，不声称构建了新的最小镜像。

## 工具、初始化和资源服务

command 工具使用 argv 列表，stdin 接收模型生成的参数 JSON，stdout/stderr/exit_code 作为工具结果反馈。模型只收到工具的名称、描述和参数 schema，不收到执行命令或 evaluator 配置。调用前校验参数；不存在的工具或错误参数作为工具错误反馈。

MCP 服务在 environment.config.mcp_servers 中声明：

```json
{"name": "files", "argv": ["python3", "/assets/tools/server.py"]}
```

执行器通过 `docker exec -i` 连接容器内 stdio MCP，完成 initialize、分页 list_tools、call_tool 和关闭。模型可见名称为 `服务名__原工具名`。需要网络访问的工具显式设置 network_access=true；默认任务网络为 internal。

初始化可声明 initial_workspace（包内目录）和 preprocess（在 Agent 容器中执行的 argv）。工具/初始化所需资源通过 mounts 只读挂载到 `/assets/`；工作区独立挂载到 `/workspace`。包不会整体挂载给 Agent，evaluator 和参考答案只应列在 evaluation.config.mounts。

数据库等依赖在 services 中声明，每任务创建独立容器和网络，不发布宿主端口：

```json
{
  "name": "database",
  "image": "postgres:15",
  "env": {"POSTGRES_USER": "tester", "POSTGRES_DB": "testdb", "POSTGRES_PASSWORD": "test-only"},
  "ready": ["pg_isready", "-h", "127.0.0.1", "-U", "tester", "-d", "testdb"]
}
```

服务名同时是任务网络中的 DNS 别名。services.mounts 可挂载包内初始化资源。environment.config.env 为任务工具和 evaluator 设置公开配置；inherit_env 仅声明需继承的宿主变量名，值不写入计划。模型 API 密钥由宿主 Executor 使用，默认不传入任务容器。

## 接入与测试

在项目目录运行：

```bash
../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py list
../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py register --benchmark file_report
../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py test --benchmark file_report
../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py test --benchmark file_report --runtime
```

test 的两个层次分别为静态合同/资源检查和真实容器运行。`--runtime` 初始化环境、发现工具、执行 smoke.json 中该题的可选 calls、调用 evaluator，并核对关闭回执；不调用模型。smoke.json 可以配置 passed 预期值。没有 smoke 时也可以测试初始化、发现和评分可用性，此时不要求题目通过。结果写在 package-tests，不能当作模型成绩。

## API 执行与并发

独立解释器 `.runtime/api-executor-venv` 已安装 API/MCP/schema 依赖，完整版本锁为 `configs/api_executor.requirements.lock`。其中 Pydantic 固定为已验证的 2.11.3，避免 MCP 1.9 导入不兼容。其他机器可用 uv venv 和 uv pip sync 按该锁重建；不要升级原业务验证解释器。

在外部安全配置好 MODEL_API_KEY，可选 MODEL_API_URL 后：

```bash
# 默认只生成计划；--execute 才真实调用模型并启动任务。
../codex/run_and_log.sh -- .runtime/api-executor-venv/bin/python scripts/benchmark_tasks.py run \
  --benchmark file_report --provider openai --model YOUR_FIXED_MODEL --concurrency 2
```

provider 支持 openai/anthropic；参数还包括 --max-turns、--max-output-tokens、--task-timeout、--request-timeout、--task、--run-id。也可以把 --benchmark 换成 --task-set 注册后的标准 JSON。不同已注册包的任务可合并为同一 TaskSet，交给同一 ExecutorService；并发上限作用于整个批次。API 请求与工具结果循环由通用 Executor 负责，不进入任何特定数据集的旧 runner。run ID 不可重放，取消/超时保留未知状态并尝试清理。

## evaluator 与统一结果

evaluator 使用与 Agent 相同的任务镜像，在单独容器中调用；共享该任务的资源网络，工作区只读挂载，额外挂载评价所需资源。stdin 是 `{"task": TaskSpec, "context": {"workspace": "/workspace", ...}}`，stdout 必须是一个 JSON 对象，日志写 stderr。原程序没有 JSON 接口时，在任务包内放一个薄适配器调用原 evaluator，保留原成功条件。

接受 `{"pass": true/false}` 或 `{"reward": 0.375, "mask_sample": false}`；统一保留 valid/reward/passed/raw、任务与 verifier 版本。异常、超时、无法解析保持 reward=null，不能当成任务失败的 0。任务未通过但评分有效，仍属于完成的执行。容器、网络删除后核对结果；清理不确定单独记录，不把进程退出当作资源已关闭。

valid=true 仅表示评分协议可用，不能证明数据前提、工具语义或评分规则正确，更不能自动把 reward=0 归因于模型。需要核对这些条件后再做失败归因。若原 Bench 评分存在已复现的矛盾，修订程序必须另标版本，双报原分与修订分，并保存规则说明和源码摘要；不能把修订分当作原版成绩。

NeMo 的部署注册与分发辅助层继续使用同一 VerifierService；真实 NeMo HTTP 部署不由这些本地测试认证。API 轨迹不自动成为 Generator 的训练 token/logprob，所有新回执 training_admitted=false。

## 当前验收边界

检查覆盖目录发现/自动注册、快照与篡改拒绝、两种 API 消息合同、跨数据集并发、真实 Docker 工具与独立评分容器、stdio MCP 和资源服务网络、关闭确认。API 消息合同使用替身，模型循环测试使用明确标记的 driver；未调用付费模型。新数据集还应先运行自己的 --runtime 测试，再进行真实模型执行。

清理验证见 `evidence/mainline/cleanup_20261031.json`；之后的模型复跑、接口兼容对照和任务工具修复见 `evidence/mainline/gpt20_repair_20261031.json`。不同源码检查点分别解读。

## 已接入数据集与 GPT20 结果

完整 503 项标准任务包位于 `integrations/benchmarks/toolathon-gym/`，每题题面与配置均随包保存。前 20 项按 task_id 升序冻结在 `.runtime/gpt20_20261031/registered_first20.json`。首批并发 20 已执行，取得 11 个有效评分，其中 2 通过；另外 5 初始化失败、4 API 错误。仅初始化失败的 5 题另作恢复批次，3 个有效评分、0 通过、2 API 错误；版本不同分别保留。

私有配置在 `.runtime/private/gpt-api.env`，使用 gpt-6.1-sol；模型 API 的历史 400 问题仍待排查。结果位置与验收边界见[项目状态](../current_status.md)。工具超过 80 个时通过通用搜索/调用接口选择，超长结果保存为任务工作区文件。

最新完整修复批次有效评分 18/20，原评分通过 6 题，另 3 题通过已声明修订评分，9 题未通过、2 题 API 拒绝；全部资源关闭。工具声明显式 strict=false，其效果由同时 A/B 控制和独立批次验证。新的 arXiv ID/商品排序修复目前只有无模型验证，不能写成模型已验收。
