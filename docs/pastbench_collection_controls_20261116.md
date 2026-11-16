# PAST-Bench 经验采集：control 持久化恢复

本轮按用户确定的采集协议运行：cold、learn/reflection、evaluation 保留当前修订版的持久化能力；control 按固定上游版本 `f8223517ae7491e776b69793d9f11e9d074ab42e` 选择权限。不同机制仍通过对应 memory、skill 或 session history 获取经验；不把这些载体全部打开。cold 从空状态或任务规定的初始状态开始，不引入未来学习信息。

## 修改范围

|任务族|需要恢复的 control|修改|
|---|---|---|
|memory_ability/EP01_prior_case_recall|EP01_I06_control_history_recovery|关闭全部持久化工具，恢复 fresh 历史分支|
|proactive_information_gathering/PG01_release_decision_followup|PG01_CONTROL_PRELOAD_001、PG01_CONTROL_BOUNDARY_001|撤回额外 skills 权限，保留原 session_search|
|proactive_information_gathering/PG03_oncall_handoff_lookup|PG03_CONTROL_PRELOAD_001、PG03_CONTROL_BOUNDARY_001|撤回额外 skills 权限，保留原 session_search|
|proactive_information_gathering/PG05_change_freeze_followup|PG05_CONTROL_PRELOAD_001、PG05_CONTROL_BOUNDARY_001|撤回额外 skills 权限，保留原 session_search|

只修改持久化工具权限和 EP01 I06 对应的历史继承条件。题面、task_id、私有答案、grader、fixture、业务工具、既有执行指引、模型和预算均保持原修订版。EP01 I06 的 history_recovery 题面与评分保留，因此关闭经验后可能无法通过；如实记录，不为提高分数修改题面或评分。

配置入口为 `persistence_restoration`，协议 `experience-collection-control-switches-v1`。每族配置完整列出所有位置的四项权限与历史分支，防止仅靠任务名推断。`pastbench_persistence.py` 校验整族清单，最后应用权限；`pg01_tools.py` 不再为 control 增加 skills，但保留 evaluation 的既有额外 skills 权限、真实策略编辑和所有业务逻辑。缺少该配置时，EP01 的历史恢复设置仍是旧版本；必须使用本轮冻结配置重现本轮结果。

## 实测结果

|任务族|旧通过|本轮通过|有效评分|
|---|---:|---:|---:|
|EP01_prior_case_recall|8|6|8/8|
|PG01_release_decision_followup|6|8|8/8|
|PG03_oncall_handoff_lookup|8|6|8/8|
|PG05_change_freeze_followup|8|8|8/8|
|新跑合计|30|28|32/32|

整族重跑存在模型采样差异，不把所有分数变化归因于 control 开关。与其余旧族整体合并后：138/203 通过（67.98%）、65 未通过、1 未评分；严格成功轨迹 127，成功原生模型交互轮次 811。203 份已完成执行均匹配本次采集开关协议；原始评分绑定与 32 题公开输入/历史谱系核验通过，实际凭证匹配 0。

## 执行与合并

- 4 个受影响族从原初始状态整族独立重跑，共 32 题；不拼接同族不同轮次的高分。
- 22 个未受影响族完整复用此前修订版，共 172 个位置，其中原有 1 题未取得有效评分。
- 输出至 [trace/pastbench-collection-20261116](../trace/pastbench-collection-20261116/README.md)。每族新跑/复用来源、模型、配置、框架与题库哈希均保留。
- 旧尝试、两次被用户中止的不同配置探针与原始评分保留在本地，未混入选定结果。
- 这是版本化经验采集，不是单轮独立 204 题测量，也不是论文的 shared-cold / persistence on-off 配对复现。

## 查看数据

交付目录 `README.md` 给出实际计数；`tasks.csv` / `trajectories-index.jsonl` 可按任务族、bucket、来源及评分筛选。可读轨迹在 `families/<族名>/traces/`；压缩 JSONL 包含模型交互与技能版本；原始 wire 字节另归档。`task-passed-index.jsonl` 与要求完整原生响应、流水线的严格成功轨迹索引分开。

```python
import gzip, json
with gzip.open('trace/pastbench-collection-20261116/families/memory_ability__EP01_prior_case_recall/all-trajectories.jsonl.gz', 'rt') as stream:
    for line in stream:
        task = json.loads(line)
        print(task['task_id'], (task.get('objective') or {}).get('score'))
```

没有 Generator 真实采样 token/logprob 或参数更新；工具轨迹不能冒充策略梯度训练样本。控制题低分单独解释，接口/评分异常保持未知。

## 复现

冻结的题库、框架和逐族配置位于交付目录 `provenance/` 与 `families/<族名>/run-config.json`。先克隆上游固定基线，再将 `repaired-benchmark.tar.gz` 解压覆盖对应三个目录，以保留此前所有修订。使用本仓库框架后，可按族独立执行：

```bash
PYTHONPATH=framework .venv/bin/python -m skillrl.pastbench_campaign \
  --source upstream/PAST-Bench \
  --config trace/pastbench-collection-20261116/families/memory_ability__EP01_prior_case_recall/run-config.json \
  --output .runtime/pastbench/NEW_RUN --workers 1 --families memory_ability/EP01_prior_case_recall
```

使用环境变量提供模型凭证，不能将凭证写入仓库。工作区内实际命令均通过 `codex/run_and_log.sh` 记录。89 项框架回归通过；最终开关、原始评分绑定、清单、来源与凭证扫描以交付目录验收回执为准。
