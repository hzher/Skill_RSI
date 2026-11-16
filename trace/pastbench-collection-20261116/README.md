# PAST-Bench 经验采集轨迹（2026-11-16）

204 个任务位置；有效评分 203，通过 138，未通过 65，未评分 1。已评分通过率 67.98%；全题通过任务族 3/26。

## 采集配置与来源

cold、learn/reflection、evaluation 保留此前允许持久化的配置与合法状态继承；control 恢复固定上游基线的工具开闭，包含 EP01 I06 的 fresh/关闭条件。其余题面、评分器、fixture、业务工具、模型及预算没有改变。不是论文 shared-cold 复现，也没有运行配对关闭组。

4 个受影响任务族整族新跑 32 题，另外 22 族完整复用 2026-11-15 修订版的 172 个位置。不按奖励挑选任务或跨轮拼接同族。旧未知评分保持 null。此前被用户中止的两次 wider-scope 探针未选入本数据集。

EP01 I06 保留既有 history_recovery 题面/评分/ID，仅关闭持久化并恢复 fresh 分支；因此该位置不是原版题目语义，低分照实保留。

## 阅读与提炼技能

- `trajectories-index.jsonl` / `tasks.csv`：204 题索引、阶段、分数、来源及可读轨迹路径。
- `task-passed-index.jsonl`：原 evaluator 判定通过；`successful-trajectories-index.jsonl` 另要求执行、流水线与原生响应完整，门槛更严格。
- `families/<组名>/traces/`：实际初始提示、工具调用、最终回答及评分回执。
- `families/<组名>/*.jsonl.gz`：完整轨迹、成功原生轮次与技能版本。用 Python gzip 或 zcat 读取；原生模型轮次独立于评价元数据，避免将私有答案混入技能提炼输入。
- `families/<组名>/native-wire-receipts.tar.gz`：原始 actor 请求/响应字节及失败回执。
- `source-map.json`：每个任务族的新跑/复用来源和源码身份；`provenance/`：冻结题库、修复前后框架、仅持久化修改补丁及开关核验。

成功轨迹可用于整理技能候选；存在成功消费者不代表因果增益已验证。control 的低分与普通业务失败分开解读；接口或评分异常不当作模型负例。没有 Generator 自身训练采样身份，不可作为直接策略梯度样本。

## 逐族结果

旧通过数只作同族历史参照；整族独立重跑会有采样差异，分数变化不能全部归因于 control 开关。

|任务族|来源|有效评分/计划|旧通过|当前通过|当前通过率|
|---|---|---:|---:|---:|---:|
|memory_ability/EP01_prior_case_recall|rerun|8/8|8|6|75.00%|
|memory_ability/EP02_exception_list_recall|reused|8/8|7|7|87.50%|
|memory_ability/SM01_preference_adoption|reused|8/8|7|7|87.50%|
|memory_ability/SM02_constraint_retention|reused|8/8|4|4|50.00%|
|memory_ability/SM05_weak_trigger_preference_adoption|reused|9/9|6|6|66.67%|
|proactive_information_gathering/PG01_release_decision_followup|rerun|8/8|6|8|100.00%|
|proactive_information_gathering/PG02_ops_exception_desk|reused|8/8|7|7|87.50%|
|proactive_information_gathering/PG03_oncall_handoff_lookup|rerun|8/8|8|6|75.00%|
|proactive_information_gathering/PG04_temporary_waiver_audit|reused|8/8|7|7|87.50%|
|proactive_information_gathering/PG05_change_freeze_followup|rerun|8/8|8|8|100.00%|
|proactive_information_gathering/PG06_kappa_integration_review|reused|8/8|8|8|100.00%|
|procedural_ability/PC01_sop_bootstrap_01|reused|8/8|5|5|62.50%|
|procedural_ability/PC01_sop_bootstrap_02|reused|8/8|5|5|62.50%|
|procedural_ability/PC01_sop_bootstrap_03|reused|8/8|5|5|62.50%|
|procedural_ability/PC01_sop_bootstrap_04|reused|8/8|2|2|25.00%|
|procedural_ability/PC01_sop_bootstrap_05|reused|8/8|6|6|75.00%|
|procedural_ability/PC01_sop_bootstrap_06|reused|8/8|5|5|62.50%|
|procedural_ability/PC03_latent_rule_induction_01|reused|6/7|5|5|83.33%|
|procedural_ability/PC04_failure_to_rule_01|reused|9/9|2|2|22.22%|
|update_ability/EP03_recall_then_modify|reused|7/7|5|5|71.43%|
|update_ability/PC02_sop_patch_01|reused|7/7|1|1|14.29%|
|update_ability/PC02_sop_patch_02|reused|7/7|4|4|57.14%|
|update_ability/SM03_fact_correction|reused|7/7|5|5|71.43%|
|update_ability/SM04_rule_migration|reused|7/7|5|5|71.43%|
|update_ability/SM06_temporary_exception_pollution|reused|8/8|3|3|37.50%|
|update_ability/SM07_scoped_rule_migration|reused|8/8|6|6|75.00%|
