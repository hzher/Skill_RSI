# 轨迹与经验采集结果

## 当前数据集

[PAST-Bench 经验采集与 control 开关修复（2026-11-16）](pastbench-collection-20261116/README.md)

|指标|结果|
|---|---:|
|任务族 / 任务位置|26 / 204|
|新跑 / 复用位置|32 / 172|
|有效评分 / 未评分|203 / 1|
|通过 / 未通过|138 / 65|
|已评分通过率|67.98%|
|严格成功轨迹|127|
|成功原生模型交互轮次|811|
|全部任务通过的族|3 / 26|

cold、learn/reflection、evaluation 保留持久化能力；control 按固定上游权限。7 道 control 恢复，4 个受影响族从初始状态整族重跑，其余族整体沿用旧修订版。此前被中止的探针没有混入。

完整任务索引、评分、可读轨迹、原生 wire、技能版本和来源在上述目录；未评分不转成失败。`task-passed-index.jsonl` 与 `successful-trajectories-index.jsonl` 分开：后者还要求执行、原生响应及经验流水线完整。

[验收回执](pastbench-collection-20261116/validation.json)：204 个唯一任务，203 份执行开关核验，71 个 control 核验（23 个关闭持久化），7 个 control 变更、非 control 变更 0；冻结题库未改变；已扫描内容未匹配实际凭证。没有 Generator 参数更新或技能因果收益声明。
