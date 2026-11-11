# 测试与发布验收

本次同时检查协议行为、完整链路和原生容器接线。机器可读结果位于reports。测试模型提供固定且可审查的响应，外部真实模型调用数为0。测试的任务成绩用于校验框架控制流。

## 十轮完整回归

```sh
cd framework
python3 tests/run_rounds.py --rounds 10
```

每轮使用新临时工作区和本地HTTP服务，随机打乱场景顺序。全部场景重新执行，检查跨轮状态泄漏及持久化幂等。结果逐轮保存到reports/local-ten-rounds.json。

|场景|主要检查|
|---|---|
|1|执行、add、下一题实际加载和两段Judge|
|2|自主跳过、全目录、没有正文时不采样评分|
|3|完整父Read、update稳定ID与新SHA|
|4|archive与历史正文保留|
|5|merge需要所有父项全文|
|6|split为两个独立条目|
|7|依赖环拒绝与库原子性|
|8|客观答案变化不改变管理观察和模型输入，失败任务正信用封顶|
|9|真实HTTP429原文与Retry-After、重启后不重复调用|
|10|已闭任务恢复零新请求、唯一writer和未解intent阻断|
|11|WorkBuddy镜像绑定、独立评分子进程stdin/stdout合同|
|12|无效Judge候选保留、null与不重采样|
|13|原生Messages到OAI再回SDK SSE的真实HTTP|
|14|确切Read正文、同工具ID、完整后续请求及缺字反例|
|15|WorkBuddy公开archive导出、私有grader隔离、恶意link拒绝|
|16|评分挂起时下一任务仍开始|
|17|观测token超预算的完整响应保留且工具不执行|
|18|CLI与技能两个只读挂载同时存在|
|19|同一评审夹具覆盖原生Bash动作|
|20|其他benchmark的业务动作名称可配置|
|21|兼容标准完整文本响应中的tool_calls=null|
|22|失败请求缺失usage保留null|
|23|无效完整响应已知usage仍计入分账|

## Linux原生Claude Code与Docker

提供tests/native_docker_rounds.py，需要实际Docker、固定镜像和Claude二进制。程序通过真实原生SDK读取技能、Bash读取不同公开数值并写文件；管理器提交真实事务；评审检查同一版本与后续动作。每题实际退出码、调用/返回数、正文证明和独立评分均写入NATIVE_RESULT.json。

```sh
python3 tests/native_docker_rounds.py \
  --root /your/dedicated/new-test-run \
  --image your-pinned-test-image \
  --network your-internal-network \
  --gateway YOUR_PRIVATE_GATEWAY \
  --cli /path/to/standalone/claude \
  --rounds 10
```

发布结果摘要保存于reports/native-ten-rounds.json。原始全量JSONL、模型请求/响应、每题事务和容器退役回执保留于专用服务器测试目录。它们是工程验证资产，未混入原100题科学数据。

## 独立拷贝与安装

发布前把整个目录复制到新临时位置，在移除原仓库PYTHONPATH的子进程中检查导入、CLI预检和一条完整链路。另在独立venv内验证pip安装和安装后的命令入口。测试报告保存当次源码SHA，交付清单保存目录中的文件SHA。

## 上线真实benchmark前

本次WorkBuddy检查覆盖原题导出、逐题镜像与原生评分命令接口。使用者仍需给出自己固定的官方数据、镜像与评分钩子，并用真实提供商做小规模接入验收。正式任务的权限、参考奖励与原生checker语义由对应benchmark合同定义。

其他benchmark可以配置Reviewer的action_tools，或实现自己的执行/环境适配器。原生数据里的其他工具名称应按真实动作语义登记，不能把读取目录或模型自述直接算采用。配置变化会记录到冻结plan。
