# 最终原生评分与过程统计

来源为本目录已导出的逐题原生评分。主采集按固定题序统计，旧中断批次单列。

| 范围 | 场景 | 阶段 | 评分记录 | 原生成功 | 原生成功率 | 平均 reward | 新动作/反馈轮数 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| auxiliary_interrupted | codebase_adaptation | baseline | 2 | 2 | 100.00% | 0.825000 | 16 |
| primary | blind_spectrum_monitoring | run | 90 | 58 | 64.44% | 0.571979 | 90 |
| primary | codebase_adaptation | baseline | 19 | 12 | 63.16% | 0.518421 | 152 |
| primary | codebase_adaptation | run | 19 | 12 | 63.16% | 0.584211 | 72 |
| primary | cohort_studies | run | 20 | 13 | 65.00% | 0.020140 | 230 |
| primary | database_exploration | run | 40 | 35 | 87.50% | 0.788320 | 98 |
| primary | exploitable_poker | run | 120 | 83 | 69.17% | 6.079167 | 281 |
| primary | sales_prediction | run | 12 | 12 | 100.00% | 0.908850 | 52 |

主采集共 320 条评分记录，含 Codebase 两个阶段；覆盖 301 个唯一实例。另有 2 条旧 Codebase 辅助记录，不能加入主采集分母。

逐题 reward、原始指标、成功判定、耗时、估算成本、重放、恢复来源、完整补丁数量及轨迹位置见 [episode-metrics.csv](episode-metrics.csv)。

各组 reward/耗时/估算成本的数量、总和、均值、中位数、最小值、最大值、失败题清单和原生状态字段见 [detailed-statistics.json](detailed-statistics.json)。缺失失败原因保持空值，不补写推测。

Poker 的 16 条对手先弃牌记录没有模型交互，保留原生手牌历史和评分。Cohort 的 2 轮重放前缀单列；共 991 轮新增动作/反馈不是 991 次 API 请求。

本结果来自一次覆盖性采集，源版本为未合并候选；不代表共享 RSI 平台的实跑结果，也不代表正式跨模型对照实验。
