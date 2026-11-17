# Rollout采集的三种输入模式

本发布保留实际运行过的Claude Code原生采集链，并提供统一题单入口。分类和乱序各100题，共200题次。独立模式逐题串行，每题使用独立HOME与原生会话。三种入口均为空技能、不精炼，客观成绩保存在评测侧。

## 配置

在`framework/`执行下面的命令。Python需要3.11以上，安装固定官方CL-Bench及其Python依赖。官方版本为`5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26`。

```sh
cp examples/clbench/config.example.json config.local.json
```

编辑`config.local.json`中的API端点、Claude二进制、Docker镜像、internal网络及其宿主桥地址。示例使用DeepSeek Chat接口，API Key通过`DEEPSEEK_API_KEY`环境变量交给宿主读取。配置中的凭据命令只读取环境变量，源码不含密钥。

官方任务镜像、题库和Claude Code二进制需要事先准备。示例的镜像摘要和API域名为待替换值。`--check`只验证题单、数量和顺序，Docker、网络、二进制与API就绪需在实际部署时核验。

## 1. 分类顺序100题长会话

```sh
python3 -m skillrl.collect --mode grouped \
  --config config.local.json --plan examples/clbench/paired-100.json --check
python3 -m skillrl.collect --mode grouped \
  --config config.local.json --plan examples/clbench/paired-100.json --output runs/grouped
```

## 2. 相同100题乱序长会话

```sh
python3 -m skillrl.collect --mode shuffled \
  --config config.local.json --plan examples/clbench/paired-100.json --check
python3 -m skillrl.collect --mode shuffled \
  --config config.local.json --plan examples/clbench/paired-100.json --output runs/shuffled
```

两个模式共用同一份100题清单，配额为频谱30、代码7、队列6、数据库13、扑克40、销售4。原选题seed20261116，乱序seed20261117，题单与原200题次归档一致。每条链原生session ID、HOME和notes贯穿100题，官方任务沙箱逐题切换，原生自动压缩开启。可在两个终端同时执行这两条链。

统一入口直接调用原有`skillrl.continual.execute_chain`。原有`python3 -m skillrl.continual --arm grouped/shuffled ...`入口保持可用。未闭合的长会话需要依据原生session和工具原证进行owned续接。

## 3. 300题逐题独立session

```sh
python3 -m skillrl.collect --mode independent \
  --config config.local.json --plan examples/clbench/independent-300.json --check
python3 -m skillrl.collect --mode independent \
  --config config.local.json --plan examples/clbench/independent-300.json --output runs/independent300
```

`independent-300.json`保存此前三批各100题的去重覆盖，缺少的是当时单独补齐的`database_exploration-012`。官方固定版本实际301题，因此同时提供`independent-301.json`覆盖全部官方题目。将上面命令的题单替换为该文件即可运行301题。

该入口按题单逐题调用原有`skillrl.clbench.execute`，每题独立工作区、HOME与CLI进程，启动参数不含跨题resume。官方初始化就已终态的题保留初始状态和客观结果，单独标记`initialization_terminal`，没有对应模型请求。实际模型session数量依据原生记录统计。

独立题的完整终态按原`intent.json/result.json`复用。未闭合任务的intent阻止同题自动重发，采集器记录该题错误并继续其他独立题。`collection-result.json`汇总完整与缺失覆盖，真实缺口保持不完整，进程返回1。修改题单、顺序或配置不能复用原运行目录。

## 留档与验证范围

各题目录保留原始Messages输入、实际Chat输入、完整SSE、API返回的thinking、逐块时间、usage、token/s、原生CLI、工具调用返回、官方动作与评分、工作区及会话文件。长会话额外保存压缩边界、摘要请求与压缩前后笔记/会话快照。文件操作、自动压缩和正式Skill使用分别统计。

原200题次数据、报告和已保留问题见[轨迹仓库](https://github.com/sheepyedu/Skil-RSI-Repository)。一份原生CLI摘要重组两字符变化、两次thinking达到输出上限及辅助计数404等问题仍保留在原件中。

本次发布做CPU回归和输入/文件校验，模型与Docker实测沿用各来源的历史证据。三种模式的CPU接线测试与真实任务达标分开报告。结果不构成上下文策略的因果收益或训练数据采纳结论。

## 两个可编辑上下文CLI框架

额外源码在[companions目录](../companions/README.md)。它们各自保留独立的`clm`包、原生CLI启动器与测试，按各自目录启动。采集器三种模式的默认机制为原生Claude Code会话和自动压缩。
