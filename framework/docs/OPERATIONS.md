# 部署、恢复与结果读取

## 原生Claude Code部署

1. 固定任务镜像digest及原始公开工作区。镜像需要相容glibc、shell，以及任务本来要求的依赖。
2. 使用镜像自带Claude程序，或配置cli_host_path把已验证的独立二进制只读挂入`/opt/skillrl-claude`。
3. network设置为任务许可的Docker网络；读取该网络的实际gateway，填入bridge_bind及bridge_advertise。
4. 宿主启动的桥接监听该私有地址，任务容器须能到达它。bridge路径有随机会话令牌，上游API密钥只在宿主读取。
5. 使用`python3 -m skillrl config.json --check`核配置，再运行同一配置。

Docker CLI支持DOCKER_HOST标准环境变量。使用受控DinD时，根据平台实测选择security_profile。hardened使用cap-drop及no-new-privileges，platform使用平台本身的受控策略，需显式配置并在结果中记录。CPU2核、内存4GiB、pids256保持声明资源界限。

user应匹配任务工作区的UID/GID。宿主root可仅对新建的本题工作区分配该所有权，普通用户需使用自己的UID。技能快照及可选CLI通过只读bind提供。网络和路径由运行配置决定，没有内置Node01、H20公网地址、账号或密钥。

## 结果目录

```text
run/
  plan.json                         冻结题单、输入SHA与模型/协议配置
  OWNER.lock                        唯一活动writer
  library/                          版本blob、state与事务原件
  episodes/<id>/
    snapshot.json                   题首确切版本
    actor/                          每次模型输入输出及原生CLI日志
    execution.json                  封存可观测轨迹
    public-artifacts/               完整公开工作区归档
    artifact-manifest.json          归档SHA
    exposure.json                   逐版本正文证明
    curation-observation.json       管理器可见信息
    curator/                        Read、候选、真实提交及模型IO
    environment*.json               Docker身份、停止及退役回执
  evaluation/<id>/
    objective/                      独立评分及其工作区副本
    review/                         finding、effect、原候选与null原因
  report.json                       覆盖、状态、角色成本与评审
```

`complete=true`要求每个任务执行闭合、管理提交或合法no-op、评审处置齐全且没有无效评审。客观结果未配置时保留null，是否具备研究结论还要看实际覆盖。已通过工具或事务门不保证技能效果。

## 中断与恢复

再次运行同一配置会复用已封存执行、实际提交和已闭评审，不再调用其模型或工具。完整响应只按其原始请求身份缓存。输入、配置或封存文件变化会被拒绝。

已有intent却没有result的阶段保持待协调，原429正文、Retry-After、请求原件和成本留存。框架不会通过删除意图或自动新建任务ID解决它。恢复器应先读取原请求和工具状态，只恢复明确未执行步骤；这部分可由部署系统的会话恢复插件接入。独立任务可以继续，未知阶段在报告中列HOLD。

若实际事务已提交，而模型最终回复中断，state中的提交是权威库状态。框架回读真实commit，保存final未闭合状态，不再生成同一事务。宿主和容器侧停止记录缺失时先核原容器。

## 容器生命周期

原生进程退出后先停任务自有容器，保留完整workspace和日志，核SHA，再检查精确CID已停止及workspace所有权标签，普通rm退役。没有执行全局prune。归档失败、活跃或身份不匹配的容器保留并报错。

这使受控平台的容器数量上限不会因已闭任务累积而耗尽。使用原容器状态评分的benchmark需自行提供完整状态归档/恢复适配器；默认合同评审封存工作区与原镜像。

## 成本与范围

report按executor、curator、judge记录完整请求数、输入/输出token、usage缺失数量和已知下界，money保持null。人工seed在`manual-bootstrap`事务中单独留下来源。本实现为固定权重技能文本维护，不自动提交GPU训练作业。
