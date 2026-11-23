# 贡献 Skill RSI

变更从 `skill-rsi` 的入口定位，沿对应的源码模块确认输入、可见范围和输出。宿主行为由各自适配器解释，公共契约由共享模块维护。

- 扩展导入时，登记来源身份和定位信息，并保留无法观测的字段。
- 扩展宿主或策略时，提供生效边界、版本及操作回执。按项目限制配置范围。
- 扩展 benchmark 时，保留当前任务原生评分、缺分原因及指标方向。
- 提交时记录变更范围、复现命令和已执行的验证。使用合成输入或经授权的公开材料。

[开发分工与 PR 规范](doc/contributor/05_开发分工与PR规范.md) · [轨迹契约](doc/contributor/02_轨迹数据契约.md)

外部数据源可在已安装的 Python 包中注册 `skill_rsi.sources` 入口。工厂接收 `path`、`options` 和 `follow`，返回实现 `read(cursor)` 的读取器。使用 `skill-rsi import --reader NAME` 选择。外部 benchmark 可注册到 `skill_rsi.benchmarks`，工厂接收显式配置和配置文件目录。
