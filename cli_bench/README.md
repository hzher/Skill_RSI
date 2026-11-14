# CL-Bench 原生 Codex + API 实验数据

本目录整理 `2026.11.11` 项目在 11 月 12—13 日完成的实际 benchmark 采集。执行链路为 **CL-Bench → 原生 Codex CLI 0.167.0 → Responses API**，请求模型 `gpt-6.1-sol`，`use_oauth=false`。这些结果采集于独立原生执行链路，导入此仓库不表示曾在共享 RSI 平台运行。

源版本固定为 `a6f5ede32b4c6c13ccc4c793b3e9b9d7aebe5617`，属于含 PR25 的未合并 PR26 候选，不能写作官方 master 结果。完整来源锁、实际配置和资源摘要随包保留。模型名称来自请求参数，上游实际模型身份未独立核验。

## 最终评分

| 场景 | 阶段 | 原生成功 / 已评分 | 原生成功率 |
| --- | --- | ---: | ---: |
| Codebase | fresh（baseline） | 12 / 19 | 63.16% |
| Codebase | stateful（run） | 12 / 19 | 63.16% |
| Database | run | 35 / 40 | 87.50% |
| Sales | run | 12 / 12 | 100.00% |
| Cohort | run | 13 / 20 | 65.00% |
| BSM | run | 58 / 90 | 64.44% |
| Poker | run | 83 / 120 | 69.17% |

主采集覆盖 **301 个唯一实例、320 条评分记录，原生成功 225 条**。225/320 = 70.31% 只是这批记录的成功占比，包含 Codebase 两阶段，不能当作官方统一总分。另保留旧 Codebase 中断批次的 2 条已评分辅助记录，不混入上述分母。

全部索引共 **322 条**：306 条含模型交互，16 条是 Poker 对手在模型行动前弃牌的环境自动结束，保留真实手牌记录。共有 991 轮新增动作/反馈，另有 2 轮明确标记的恢复重放；这些轮数不等于 API 请求次数。原生 reward 与 raw metric 全量保留，不同场景的连续 reward 不混作准确率。

## 直接查看

| 内容 | 位置 |
| --- | --- |
| 机器可读主结果、执行版本和统计口径 | [results.json](results.json) |
| 分场景最终报告与原生评分 | [reports/RESULTS.md](reports/RESULTS.md)、[accuracy-by-task.csv](reports/accuracy-by-task.csv) |
| 全部 322 条记录、主/辅助角色、评分、轨迹位置和公开文件摘要 | [reports/all-episodes.csv](reports/all-episodes.csv) |
| 逐题详细统计、原始指标、重放、耗时、估算成本、失败状态 | [reports/episode-metrics.csv](reports/episode-metrics.csv)、[detailed-statistics.json](reports/detailed-statistics.json) |
| 可直接阅读的完整逐题过程 | `runs/collections/<run_id>/audit/episodes/*.json` |
| 完整原生执行、会话、事件、逐次 CLI 输入输出、工具反馈、最终提交与恢复现场 | `raw/*.zip`，按批次保存原相对路径 |
| 最终覆盖、会话连续性、中断与恢复、原始证据清单 | `reports/` 中对应原始报告 |
| 实际执行配置、依赖/镜像与源码文件锁 | `config/` 及各原始批次 `control/` |
| 采集、桥接、审计、恢复与封存的实际过程代码 | `process/`；批次当时的脚本快照还在 `raw/` 的 `control/` 中 |
| 原文件与公开副本逐文件对应、双份摘要、脱敏类型 | [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json) |
| 本次同步的全量离线验收 | [VERIFICATION.json](VERIFICATION.json)、[PACKAGE_CHECKSUMS.json](PACKAGE_CHECKSUMS.json) |

逐题 JSON 保留原题身份、阶段、题序、可见输入、模型动作、环境返回、原生评分和提交关联。若要核对 Codex 实际会话消息、事件、调用 ID、调用顺序和原始 CLI 请求/输出，请同时查看相应原始压缩包；逐题索引不能替代原生证据。

Codebase 的 `control/submissions/*.patch` 保存完整补丁，原生评分中可能截断的片段只作为关联前缀。Cohort 旧批次保留前 6 题，新恢复批次保留后 14 题及重放核验，不能把重放另算新题。失败、中断、未评分部分提交、资源退出和诊断日志保留在对应原始包中，不提前计为成功或新增覆盖。

## 离线核查与解压

从仓库根目录执行，Python 3.11+，仅使用标准库，无模型请求：

```bash
python cli_bench/tools/verify.py
python cli_bench/tools/summarize.py
python cli_bench/tools/test_export.py
```

重建完整原始相对目录时，可选择一个空目录：

```bash
python cli_bench/tools/verify.py --extract cli_bench/extracted
```

解压后在 `extracted/runs/collections/<run_id>/` 下查看 `native/`、`control/native-calls/`、`control/submissions/`、日志及其他执行现场。解压器核对摘要并拒绝覆盖内容不同的现有文件。Windows 较深路径使用脚本的长路径支持；普通解压器可能受路径长度限制。

导出工具的 7 项检查及实际长路径、CLI 请求和补丁的解压抽查见 [export-tool-tests.json](reports/export-tool-tests.json)。重新生成统计或修改公开文件后，再运行 `verify.py` 更新全量校验回执与包摘要。持有原始项目时，`tools/export.py --source /path/to/2026.11.11` 可重新导出同一范围；脚本会先核对原始封存摘要，不运行采集。

`process/` 为实验时使用的实际代码记录，不会自动补齐原 benchmark 源码、任务数据、Docker 镜像或私有凭证。需要重新执行时，应按 `config/sources.lock.json` 获取固定来源，再按批次 `control/effective-clbench.json` 和镜像锁复建。重新采集会调用 API，和上述离线核查分开。

## 内容与公开边界

只纳入实际采集、评分、恢复和验收证据；没有纳入组会讲稿、PDF、Word、个人说明、施工准备文件、第三方完整 checkout、独立题库副本或未执行的采集方案。历史压缩包只提取执行证据，环境安装和准备记录排除。

为去除个人机器信息，公开副本仅规范化操作者主目录和本地工作区路径；不删减模型消息、工具参数/结果、错误、原生评分和任务顺序。嵌套 gzip 内容同样检查。每个原文件的 SHA256 与公开副本 SHA256 分别记录，原始目录保持不动。

原批次 `checksums.json`、补丁摘要、初始题面摘要等是原始版本的历史回执；发生路径规范化时，不应拿原摘要直接检查公开副本。公开副本完整性以 `SOURCE_MANIFEST.json` 的 `exported_sha256` 为准；`source_sha256` 可在原始项目复核。可恢复的业务上下文和原有原生会话保存在原始包中，但本次没有恢复活会话、重跑 benchmark 或重新评分。

导出前重新核对原封存摘要和固定资源，导出后扫描所有公开文件、压缩包成员及嵌套 gzip，拒绝凭证形状、私钥块和未审查二进制。私有凭证及认证文件不随包分发。扫描不能替代上游使用许可，来源许可见 [NOTICE.md](NOTICE.md)。

费用字段均为框架估算，不能当作网关账单；缺失的失败原因、原生状态和令牌统计保持原样，不补写推测。
