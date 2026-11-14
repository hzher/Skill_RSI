# 未随交接目录分发的内容

导出只读取 `aa07d24` 的 Git 对象，并按 `tools/export_work_agent_env.py` 的白名单选择文件。没有递归复制原工作区。

- `.git/`、stash、`.runtime/`、`.upstream/`、`.venv/`、缓存、node_modules、实际 `.env` 和机密配置。
- SSH 私钥、服务凭证、用户自己的数据库、模型权重、检查点与运行中资源。
- `integrations/benchmarks/toolathon-gym/`、原 `tasks/` 和完整受限题库、私有原 verifier 与其配置。
- `evidence/`、原运行轨迹、数据库/附件归档、服务器源快照和已生成的 Skill/训练候选。
- `integrations/skill_generator/vendor/` 及其他第三方 vendor；保留第一方接线和公开来源锁，不重新许可这些上游。
- 机器专用服务、模型路径和训练部署配置；保留可用依赖锁、环境变量示例、接口 profile 与训练配置模板。

仅保留 `integrations/benchmarks/file_report/` 这一个项目自建公开诊断样例。排除文件数量按原因记录于 SOURCE_PROVENANCE.json，不公开列出所有私有题目或轨迹文件名。

这意味着部分可选代码需要操作者另行取得上游和授权资源才能运行。缺失项是明确的交接边界，不通过伪造数据、删除路由校验或换用补充 verifier 来绕过。
