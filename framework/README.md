# Rollout采集框架 · 协作者上手

使用Claude Code调用DeepSeek API采集任务轨迹，支持分类长会话、乱序长会话和逐题独立session。初始技能为空，采集模式关闭技能精炼。

## 1. 准备环境

需要Python 3.11以上、Docker使用权限、Claude Code可执行文件，以及已安装依赖和任务镜像的CL-Bench。H20上的题库目录为`/mount-data/Datasets/continual-learning-bench/repository`。

```bash
git clone --branch yangyang https://github.com/hzher/Skill_RSI.git
cd Skill_RSI/framework
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
cp examples/clbench/config.example.json config.local.json
```

编辑`config.local.json`，填写自己的API端点、Claude二进制路径、容器镜像摘要、Docker internal网络及桥地址。示例配置保留了这些字段。将API密钥放入环境变量`DEEPSEEK_API_KEY`。

先检查题单和顺序，该检查的范围为输入配置。

```bash
python3 -m skillrl.collect --mode grouped --config config.local.json --plan examples/clbench/paired-100.json --check
```

## 2. 选择采集模式

**分类顺序100题**，同一个session连续执行。

```bash
python3 -m skillrl.collect --mode grouped --config config.local.json --plan examples/clbench/paired-100.json --output runs/grouped
```

**同100题打乱顺序**，使用另一条持续session。

```bash
python3 -m skillrl.collect --mode shuffled --config config.local.json --plan examples/clbench/paired-100.json --output runs/shuffled
```

这两条命令可分别在两个终端运行，共200题次。每条链保留自己的上下文和笔记，官方任务沙箱逐题切换。

**300题逐题独立执行**，各题使用独立HOME和session。

```bash
python3 -m skillrl.collect --mode independent --config config.local.json --plan examples/clbench/independent-300.json --output runs/independent300
```

官方题库实际301题，完整覆盖时改用`independent-301.json`。初始化就已终态的题单独记录官方结果。各次新实验使用独立输出目录，恢复时沿用原目录并检查已有intent和结果。

## 3. 查看轨迹

输出目录包含逐题结果、原生会话、工具调用及返回、完整模型请求和SSE响应、API返回的thinking、token用量与计时。持续会话还保存笔记版本、压缩边界及前后快照。

- `cases/`保存各题结果与工具、原生日志。
- `requests/`、`upstream/`保存模型输入输出，独立模式的这些目录位于各题目录内。
- `result.json`或`collection-result.json`保存整轮结果，`PROGRESS.json`显示执行进度。

## 4. 使用两套CLI上下文编辑框架

| 框架 | 说明与配置 | 启动入口 |
| --- | --- | --- |
| Claude Code CLM | [使用说明](companions/claude-code-clm/README.md) | `bin/claude-clm` |
| Codex CLM | [使用说明](companions/codex-clm/README.md) | `bin/codex-clm` |

两套CLI各自在自己的目录运行。原H20部署路径、二进制、镜像与API配置见各自README。

## 测试与更多说明

```bash
python3 tests/run_rounds.py --rounds 10
```

[三模式详细说明](docs/ROLLOUT_COLLECTION.md) · [双CLI机制](companions/README.md) · [技能管理与评审架构](docs/ARCHITECTURE.md) · [接入其他Benchmark](docs/BENCHMARKS.md) · [验证结果](reports/ROLLOUT_RELEASE_20261117.md)
