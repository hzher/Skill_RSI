# CL-Bench · Sol MAX Stateful

同一批 301 道题、同一模型 `gpt-6.1-sol` 和 `max` 推理强度，历史 Stateful 取得 254/301（84.39%），Fresh 为 139/301（46.18%）。两列均取 CL-Bench 原生 `success`。Stateful 按题序推进同一任务族的环境，并接续该族的 Codex 会话。频谱和扑克分别贡献了 74 和 21 个净增通过。

## 阅读原实验代码

每次启动都冻结了当时的 Python 源文件。三个目录按运行时间排列，每份 `manifest.json` 记录该次启动的文件及 SHA-256。

| 阶段 | 实际使用的代码 |
|---|---|
| 首次启动 | [题序、会话接续与逐题封存](historical/console-20261120-081944-657b5e/stateful_runner.py) |
| 第一次恢复 | [恢复时的收集器](historical/console-20261120-135354-31b813/stateful_runner.py) · [恢复逻辑](historical/console-20261120-135354-31b813/stateful_recovery.py) |
| 最后一次恢复 | [收集器](historical/console-20261120-142831-f9ef4b/stateful_runner.py) · [任务族环境及模型可见状态](historical/console-20261120-142831-f9ef4b/stateful_environment.py) · [Codex 会话接续](historical/console-20261120-142831-f9ef4b/stateful_actor.py) · [恢复逻辑](historical/console-20261120-142831-f9ef4b/stateful_recovery.py) |

历史启动器依赖当时的控制台目录、WSL 宿主及固定版本。框架提交为 `afd8d1c0165009d0a958463f71ec4a93afe7e64f`，CL-Bench 提交为 `5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26`。历史环境向模型提供了可能带有阶段或策略线索的实例 ID。扑克中需要决策的 104 手，Stateful 在首次行动前均可见对手策略标签，Fresh 均未见。这个差异会影响对会话延续收益的归因。

## 用当前框架运行

[共享收集入口](../../framework/skillrl/collect.py) 调用[任务族状态和会话管理](../../framework/skillrl/codex_stateful.py)。当前实现从 `status()` 和动作返回的 `current` 字段中移除了原生实例 ID。254/301 属于历史运行，当前实现的全量成功率仍待实跑。

在仓库根目录使用 Linux 或 WSL，先校验[配置模板](config.example.json)和[原题序](plan.json)。这一步只检查输入。

```bash
case_dir=CLbench-Codex/6.1Sol-MAX-stateful
PYTHONPATH=framework python -m skillrl.collect --mode stateful \
  --config "$case_dir/config.example.json" --plan "$case_dir/plan.json" --check
```

正式运行时，将模板复制到本地，填写已安装依赖的 CL-Bench checkout 绝对路径，在已登录 Codex 的环境使用该配置，将 `--check` 换成 `--output <本次运行的独立目录>`。
