# Work Agent Environment 基建交接

这是给团队评估复用的独立源码交接目录，来源是 `work_agent_env` 的固定版本 `main@aa07d24e308e3adfa843ca70e3faf1ccfa8185eb`。目标为本仓库当前个人分支，共享基线为 `master@d068b79deb0e71f2f55e88487efff051dadd0f0a`。

`snapshot/` 包含 **505 个原文件、约 3.5 MB，其中 445 个 Python 文件**。导出从固定 Git 对象读取，保留原命名空间和规范字节；不复制工作区里的未提交内容、stash 或运行缓存。逐文件 Git blob、SHA256 和最后改动提交见 [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json)。交接说明与来源清单只记录项目和版本信息。

本交接没有修改团队 `framework/`，也没有带入 `2026.11.13` 的 CL-Bench 实验封装、采集轨迹或之前个人工作树的共享适配提交。源码保持上述固定版本，团队可逐模块选择复用，不会因放入此目录而自动进入共享 Pipeline。

## 从哪里开始看

| 内容 | 入口 | 本包中的定位 |
| --- | --- | --- |
| 通用任务、任务集和包快照 | `snapshot/integrations/benchmark_tasks/` | 任务身份、原始题面、版本摘要、注册与包检查 |
| API Harness | `snapshot/integrations/executors/` | 固定 API 模型、多轮工具循环、并发、调用与错误记录 |
| 环境与原评分接口 | `snapshot/integrations/environments/`、`snapshot/integrations/verifiers/` | Docker、工具分发、原 verifier、资源回收 |
| 办公资源后端 | `snapshot/resources_servers/`、`snapshot/adapters/`、`snapshot/integrations/isolated_gateway.py` | 环境后端源码；业务数据与部署配置需另行提供 |
| 经验、技能库和编辑器 | `snapshot/integrations/experience/`、`skill_library/`、`skill_generator/` | 第一方源码；SkillOS vendor 未分发，相关路径需补齐固定依赖 |
| 异步与训练接口 | `snapshot/integrations/orchestration/`、`snapshot/training/` | rollout、奖励、版本及 H20/verl 接线；本次不声称 GPU 训练通过 |
| 原项目辅助工具 | `snapshot/workenv/`、`snapshot/scripts/`、`snapshot/tests/` | 保留第一方工具和测试，完整原测试集另有依赖 |
| 可公开任务样例 | `snapshot/integrations/benchmarks/file_report/` | 项目自建文件协议样例；不冒充官方办公题或 CL-Bench |

共享实现的复用建议与边界见 [MODULE_MAP.md](MODULE_MAP.md)。排除项见 [EXCLUDED.md](EXCLUDED.md)，来源许可见 [NOTICE.md](NOTICE.md)。

## 零模型检查

在本仓库根目录运行，Python 3.11+：

```bash
python baselines/work_agent_env/verify_handoff.py
```

检查导出文件摘要、Python 语法、纯任务合同和凭证形状；不启动模型、Docker 或 GPU。实际回执见 [verification.json](verification.json)。

安装下述最小依赖后，在支持符号链接的 Linux 环境、从仓库根目录执行 `python tools/check_work_agent_env_handoff.py`，可复跑正常 CLI 与选定原测试。脚本使用重新核过摘要的临时副本，移除子进程模型凭证，并禁用容器测试，不会污染快照或访问真实模型。Windows 的原测试有符号链接权限前提；本次完整离线回归以 Linux 回执为准。

正常 CLI 的静态任务检查需要原依赖中的 `jsonschema==4.25.1`。最小离线入口（无模型请求，不代表无安装网络）：

```bash
cd baselines/work_agent_env/snapshot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install jsonschema==4.25.1
python scripts/benchmark_tasks.py list
python scripts/benchmark_tasks.py test --benchmark file_report
python scripts/benchmark_tasks.py run --benchmark file_report --provider openai --model your-model-id
```

最后一条默认只生成计划，只有显式 `--execute` 才进入模型执行。注册和检查产生的文件位于本目录 `.runtime/`，由 Git 忽略。

## 真实环境的启动准备

完整 API 执行依赖以 `snapshot/configs/api_executor.requirements.lock` 为准，应装入独立虚拟环境。其他业务服务的 Python/Node 锁也保留在 `snapshot/configs/` 和对应服务目录。本次没有重装整套依赖，不能把源码导出当作所有后端均已部署。

模型凭证只从运行者私有环境注入 `MODEL_API_KEY`、`MODEL_API_URL`；`snapshot/configs/.env.example` 仅列变量名，不自动加载。不把实际 `.env`、服务账户或 SSH 私钥提交到仓库。

`file_report` 的 Docker 运行时可由 `snapshot/docker/runtime/Dockerfile` 准备为 `bench-runtime:python`。准备完环境后，`python scripts/benchmark_tasks.py test --benchmark file_report --runtime` 是原项目的零模型容器诊断；本次没有运行该路径。真实模型执行需使用实际 provider/model 并显式 `--execute`，会产生 API 请求。原任务与环境合同说明保留在 `snapshot/docs/guides/generic_benchmarks.md`。

Toolathlon、办公数据库、NeMo、SkillOS 和 verl 路径属于可选后端：需要按原锁取得相应上游、授权任务、数据库种子、模型及私有配置。它们未随此包分发。原指南中的历史 evidence 路径和机器路径是源版本背景，本包不附带这些记录，也不把历史成绩转记为本次验收。

## 验收与状态

- 本次实测：导出完整性、语法、纯任务合同；正常 CLI 和选定单元回归的结果记录在 [CHECKS.json](CHECKS.json)。
- 仅源码交接：办公后端、技能编辑/检索、异步调度与训练接口；其依赖条件见 [MODULE_MAP.md](MODULE_MAP.md)。
- 未重新验收：完整办公 benchmark、多 Harness 一致性、Skill 后续收益、GPU/GRPO 更新、CL-Bench 全量。
- 没有搬运或恢复被暂停的 `2026.11.13` 采集，没有对服务器项目做部署操作。

完整测试中的 Docker、vendor 和授权数据测试有额外前置条件。选定离线回归中的容器测试会明确跳过，不能称为容器实跑通过。第一方文件已通过凭证形状扫描；实际推送仍应查看暂存文件清单，不能把此检查当作原私有目录可直接整体上传的许可。

## 后续推送

本次只整理和本地提交。用户检查后，在目标仓库执行：

```bash
git status --short --branch
git log --oneline origin/sunchengze..HEAD
git diff --stat origin/sunchengze...HEAD
git push origin sunchengze
```

不会强推、改写已有个人分支历史或合并团队 master。
