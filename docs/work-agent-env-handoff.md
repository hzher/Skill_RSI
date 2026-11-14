# sunchengze：旧基建源码交接

本个人分支将逸凡已同步的 `work_agent_env@aa07d24` 整理为独立目录，供黄郑好及团队评估复用。

入口：[源码、配置与复跑说明](../baselines/work_agent_env/README.md)；[模块复用边界](../baselines/work_agent_env/MODULE_MAP.md)；[逐文件来源](../baselines/work_agent_env/SOURCE_PROVENANCE.json)。

范围为第一方通用任务、API Harness、资源服务、环境适配、经验/技能及训练接口。保留版本与依赖锁；不复制私有配置、数据库、完整轨迹、模型、受限题库和第三方 vendor。本提交不修改共享 `framework/`，不带入 CL-Bench 实验封装，也不宣称完整 RSI、GPU 更新或办公 benchmark 验收通过。

问题与交付：旧基建和共享框架存在重叠，团队需要可核查的源码及启动边界，而非把两个根目录直接覆盖合并。交接目录提供最小离线入口、来源校验和实际检查回执，后续按模块单独评审接入共享接口。

本说明可作为后续 Issue/PR 的范围与验收正文；本次没有创建远端 Issue/PR、推送或合并 master。用户检查后自行推送 `sunchengze` 分支。
