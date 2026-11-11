# 0.1.0发布验证

最终源码通过以下检查，源码逐文件SHA与本机、服务器验证回执一致。

|验证|实际结果|证据|
|---|---|---|
|macOS完整回归|10轮×23场景，230项通过|[逐轮日志与源码SHA](local-ten-rounds.json)|
|Linux同套场景|23项通过|[Linux记录](linux-suite.json)|
|真实Claude Code和Docker完整流程|10题，原生退出全部0，20个工具调用/20个返回，10份正文证明，1项add/9项no-op，客观与两段评审均闭合|[原生十轮](native-ten-rounds.json)|
|独立验收|10题快照、工具ID、完整产物SHA、按序事务回放及最终库一致；owner退出0且实际gone；测试网络/镜像清理0|[独立验收](native-independent-acceptance.json)|
|独立目录和安装|复制到新路径可运行完整链路；新venv pip安装、控制台入口、源码字节与依赖检查通过|[交付验证](distribution-validation.json)|
|原仓库回归|318项全部通过|[原仓库结果](legacy-regression.json)|

模型回应由测试服务提供，真实外部提供商调用0。原生Claude程序、Docker环境及其文件动作均实际运行。夹具的token值只用于核对分账字段，货币保持null。这些结果验证实现和接线，不能推断DeepSeek/Qwen真实模型能力或WorkBuddy官方任务成功率。

WorkBuddy测试覆盖公开原题导出布局、逐题原镜像映射和原生verifier命令协议。使用者接入固定的官方数据、镜像和对应版本评分钩子后，才能得到官方任务结果。

首检挂载覆盖、评审夹具遗漏Bash与null工具字段问题均有明确失败及修复记录，见[工程发现](ENGINEERING_NOTES.md)。原生全量日志和原候选保留在H20任务专用测试目录，不混入原100题实验。

原仓库全量检查首次从stdin入口运行时，multiprocessing spawn无法重新定位主脚本，1项入口失败。随后使用仓库已有`python3 -m scripts.run_cpu_checks`入口，318项全部通过。该失败保存在[初次入口结果](legacy-initial-stdin-entry.json)。
