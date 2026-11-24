# 接入连续只读数据库任务

宿主在已有线程的每道新题启动前，用当前公开题面、此前已完成回合的真实 QUERY 参数与公开返回生成附录。工具仍由原执行器调度。原生变更提示开启一个新数据版本。接入方应给每个实验臂、会话和版本分别保存来源。

`scripts/gateway.py` 中的 `Notebook.observe(sql, observation, source)` 接受已完成、未缓存、非终端查询的可见文本。附录取自 `appendix(notebook.payload())`，数据源编号可回溯到原操作。换会话、连接或遇到迁移时新建 `Notebook`。允许复用的字段、目录范围和已分组业务类别由解析器限定。

`scripts/host.py` 对接现有 RSI 的 SQL 轨迹布局。使用其 `inject(actor, round_directory, panel, arm, record=True)` 上下文挂入宿主的提示构造器。每次交付都会产生一份 `map-notes` 收据，同时保留实际 Codex 输入。独立验收时使用 `record=False` 重新计算每题记忆并逐字核对；另外用既有 RSI 验证器检查模型、线程、原生工具调用、实际代价、评分和资源关闭。
