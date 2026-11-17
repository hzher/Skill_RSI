# 三模式采集与双CLI源码发布

发布目标为`hzher/Skill_RSI`的`yangyang`，跟踪[Issue 5](https://github.com/hzher/Skill_RSI/issues/5)。核心源码来自Skill-Curator-Inquiry的`e8e567c`，其中持续会话实现为`2f02798`。

新增`skillrl.collect`统一分派分类100题、同100题乱序和逐题独立session。原生任务执行、工具返回、请求留档和默认system构造仍通过原有执行函数。独立题按各自intent/result协调，已闭合结果复用，未解任务记缺口并继续其他独立题。输入或配置变化不能复用原目录。

## 输入

| 输入文件 | 题数 | 选择依据 |
| --- | ---: | --- |
| paired-100.json | 100 | 原200题次实验冻结题单，分类与打乱为相同集合 |
| independent-300.json | 300 | 原三批100题的去重覆盖，排除当时单独补齐的database_exploration-012 |
| independent-301.json | 301 | 官方固定版本全部实例 |

仅保存公开实例标识和顺序。官方问题内容、工具schema、环境及评分由固定官方数据版本加载。初始化终态单独记录，实际模型session数依据原生文件核对。

## 源码与同步

`companions/claude-code-clm`来自`96e6cde7b53dc36a93c58a9ca6c88765e77c531e`，`companions/codex-clm`来自`806b573880f0d352460ddfca6ac60b09795465e2`，各13个源文件逐字节匹配上游Git归档。可执行文件模式保留，独立目录加载各自的clm模块。

yangyang通过merge同步本次master。六个add/add冲突来自早期framework导入，按后续e8e567c版本协调。其中native.py的持续写原生stdout/stderr实现保留了超时日志，修复来源与历史验收见原framework报告。共同协作文档之外的其他目录按Git merge结果集成。

## 本次验证

- 导入后的原核心48项CPU基线通过。
- 新三模式分派、实例覆盖、重复与配置变化保护6项回归先RED后GREEN。
- 核心54项完整回归独立排序运行10轮，540项全部通过。
- Claude Code CLM和Codex CLM各6项测试运行10轮，合计120项全部通过。
- 四份模式/题单组合的check与三个入口help实际退出0。
- 上游26个CLI文件SHA、三个题单、发布文件和凭据模式扫描另存机器可读回执。

回执为`rollout-release-20261117-ten-rounds.json`和`rollout-release-20261117-companions.json`。本次新模型调用、Docker部署及科学执行均为0，历史真实接线证据与本轮CPU验证分别记录。

H20相关路径、镜像与UID保留在两CLI启动器中。示例API域名与镜像摘要需要按使用环境配置。过去100题CLM实验的恢复和容量策略变体见相应数据归档报告，此处两套CLI源码按独立上游版本发布。
