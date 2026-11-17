# 持续会话与上下文操作

`skillrl.continual`将一组官方题目连续交给同一Claude Code会话。执行器容器、HOME、session ID与`/workspace/notes`持续保留。每个用户回合使用原生`--resume`加载上次会话，原64轮预算逐题适用。

官方任务的数据库和代码环境按该题原生规则创建，工具调用路由到当前环境。执行器的本地Read/Write/Edit/Glob/Grep服务于持续笔记，正式任务操作通过benchmark MCP执行。两条链的会话与工作区独立。

配置沿用`skillrl.clbench`的API、CLI、网络和预算字段，并增加整数`context_window`。新实验统一200000 token，原生自动压缩开启。模型别名和小模型路由均固定为同一DeepSeek API模型。初始技能为空，后台自动记忆agent关闭，主模型自主选择是否写笔记。

```sh
python3 -m skillrl.continual --config config.json --plan PLAN.json --arm grouped --output runs/grouped
python3 -m skillrl.continual --config config.json --plan PLAN.json --arm shuffled --output runs/shuffled
```

PLAN的selected_tasks保存官方family/index，grouped_order与shuffled_order保存同一集合的两种排列。两条命令各由唯一owner派发，链内逐题执行。

## 留档

- requests和upstream保存完整Messages/Chat原字节、usage、thinking和逐块计时。
- cases保存每题任务、官方工具、工作区产物、原生CLI、官方结果和请求序号范围。
- workspace/.agent/home保留原生完整session，continuity.json记录会话ID与首请求中的历史任务标识。
- notes-before与notes-after保存逐题笔记版本及SHA。
- workspace/.agent/context-events保存PreCompact与compact后SessionStart钩子的原输入、完整transcript与当时笔记快照。native中的compact_boundary单独提取。

压缩次数按实际边界事件去重，钩子与boundary为同次操作的多种证据。摘要请求和回复属于真实API调用并计入用量。CLI自动触发、模型生成摘要、主模型主动笔记操作分别标记。上下文操作为0时，只报告任务执行完成，研究目标保持未达成。

首段须实际验证原生session保持、历史进入下一请求和官方工具闭合。CPU测试验证参数与快照逻辑，真实接线与压缩效果依据运行记录。

## 2026-11-16首段实测

两条100题链均通过前2题原生同session续接核验。打乱组第3题发生auto compact，原生边界记录166840→6525 tokens、29.665秒。摘要请求真实HTTP200，CLI选取输出中summary部分的9673字符进入下一请求，原始analysis部分仍保存在API记录。5份持久笔记压缩前后SHA完全一致，随后任务和工具继续。

分类组第32题也出现原生自动压缩，166676→11862 tokens。一次`messages/count_tokens`请求被记录入口返回404，CLI继续完成压缩并执行后续题目，该记录单列本地辅助路由失败。它没有转发成模型生成请求。主模型实际笔记读写、CLI触发压缩、模型生成压缩摘要分别计数。

首证与逐题原件位于实验根`clbench-continuous-context-100x2-20261116`。任务成绩和压缩后的信息保持效果独立统计。

## 完整200题次结果

2026-11-17两条原生长会话均已完成100题。分类43/100达标，打乱41/100，实际模型响应656/777份，原生自动压缩9/10次。本地文件工具尝试155/180次，两组各一个session通过全部题目，官方初始status数据100对完全一致。

科学源包39文件保持冻结，200题原生wait0、全部工具调用返回、请求转换与原SSE通过CPU独审。两份普通推理到达32000输出上限并由原会话继续。19份摘要中18份选取正文精确进入下一请求，另1份在原生CLI重组时将美元符号加反引号变成两个换行，其余字符一致。原响应、实际后续输入与JavaScript替换语义复现均保留，具体CLI调用点尚未定位。

报告见`experiments/clbench-continuous-context-100x2-20261116/REPORT.md`。原件在CPFS的`runs/clbench-continuous-context-100x2-20261116`和本机同名runtime目录。采集记录区分CLI自动压缩时机、模型生成的摘要和模型主动笔记操作，训练采纳由后续审核与筛选处理。
