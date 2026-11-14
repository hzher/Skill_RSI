# 全部批次轨迹总索引

更新时间：2026-11-13T03:50:02.899228+08:00。统计只包括已有原生评分并建立索引的实例；运行中的未完成题目不提前计入。

共覆盖 **301 / 301 个唯一实例**，索引 **322 条实例记录**。
其中 306 条含模型交互，16 条为原生环境自动结束；重放部分不重复计数。

一份 episode 文件表示一道题的过程，可能含多轮交互；文件数不是 API 请求次数。旧中断批次与新批次分别保留，跨批次同题重复尝试不增加唯一实例数。

当前执行：原生 Codex CLI + 组内 AffordableRouter Responses API，配置模型 gpt-6.1-sol，use_oauth=false。按工作指导跑的是 CL-Bench（Continual Learning Bench），不是另一个 CLI-Bench 项目。

逐条文件位置、评分、交互轮数和类型见 [all-episodes.csv](all-episodes.csv)。

| 场景 | 已评分唯一实例 | 目标 | 索引记录数 |
| --- | ---: | ---: | ---: |
| Codebase | 19 | 19 | 40 |
| Database | 40 | 40 | 40 |
| Sales | 12 | 12 | 12 |
| Cohort | 20 | 20 | 20 |
| BSM | 90 | 90 | 90 |
| Poker | 120 | 120 | 120 |

| 场景 | 批次索引 | 本批记录数 |
| --- | --- | ---: |
| Codebase | [collection_20261112T173337+0800_b06d3bd1](../runs/collections/collection_20261112T173337+0800_b06d3bd1/audit/collection-index.json) | 2 |
| Sales | [collection_20261112T181914+0800_e88f2057](../runs/collections/collection_20261112T181914+0800_e88f2057/audit/collection-index.json) | 12 |
| Cohort | [collection_20261112T184222+0800_8624dcbc](../runs/collections/collection_20261112T184222+0800_8624dcbc/audit/collection-index.json) | 6 |
| Database | [collection_20261113T004031+0800_3b8356a2](../runs/collections/collection_20261113T004031+0800_3b8356a2/audit/collection-index.json) | 40 |
| Poker | [collection_20261113T005523+0800_e910a9cf](../runs/collections/collection_20261113T005523+0800_e910a9cf/audit/collection-index.json) | 120 |
| Cohort | [collection_20261113T010809+0800_21578a53](../runs/collections/collection_20261113T010809+0800_21578a53/audit/collection-index.json) | 14 |
| BSM | [collection_20261113T014205+0800_420b0733](../runs/collections/collection_20261113T014205+0800_420b0733/audit/collection-index.json) | 90 |
| Codebase | [collection_20261113T021152+0800_af16b595](../runs/collections/collection_20261113T021152+0800_af16b595/audit/collection-index.json) | 38 |

全部批次已结束，原生 CLI 事件、会话、完整提交及配置已封存在对应批次的 native/ 和 control/。最终覆盖与完整性结论见 collection-completion.json。
包含未评分中断、恢复关系、验证样本和压缩历史记录的位置见 [原始证据总索引](raw-evidence-inventory.json)。
评分数量不等于通过数量；目标模型名称不代表上游实际模型身份已被独立验证。
