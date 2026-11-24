# 贡献 Skill RSI

## 改完代码，构建本机可用的包

首次开发建议用仓库提供的 Conda 配置创建 `skill-rsi-dev`。改完源码后构建 wheel，开发者和用户安装同一份文件，再到各自的目标项目应用配置。[开发与打包教程](doc/contributor/07_开发与打包教程.md)

## 一次改动应放在哪里

| 开发内容 | 位置与交付方式 |
| --- | --- |
| 新宿主或宿主适配更新 | 在 `skill_rsi/adapters/<host>/` 建立 Python 子包，从 `__init__.py` 导出可无参构造的 `Adapter`，其实例的 `name` 与目录名一致。CLI 从已安装的框架中发现它。宿主专用的回调桥、注册、撤销及所需资源随同一个 wheel 交付。公共辅助子包用下划线开头。 |
| 随框架提供的策略 | 将清单、入口与依赖资源放在 `skill_rsi/components/resources/<component_id>/`。wheel 和源码包递归收录这里及适配器目录中的运行资源，包括嵌套的插件清单、脚本与 Wasm。 |
| 单独发布的策略 | 将含 `manifest.json` 的目录交给已安装的 CLI，用 `skill-rsi artifact pack SOURCE --output RELEASE.zip` 生成可分享的制品。提炼流程生成的策略也使用同一格式。 |
| 外部数据源或 benchmark | 可在独立的 Python 包中注册 `skill_rsi.sources` 或 `skill_rsi.benchmarks` 入口，并将它安装到 CLI 所用环境。数据源工厂接收 `path`、`options`、`follow`，返回实现 `read(cursor)` 的读取器，使用 `skill-rsi import --reader NAME` 选择。benchmark 工厂接收配置与配置文件目录。 |

框架版本标识整份 wheel。每个策略有自己的 `component_id`、`version` 和 `form`。修改已发策略的清单或内容时，递增策略版本，保留旧版供已有项目使用。宿主有能力或协议变更时，同时更新适配器版本和适用范围。

## 提交前

从 `skill-rsi` 的公开入口核对输入、可见范围和输出。宿主或策略改动需记录生效边界和操作回执，数据源需保留来源定位，benchmark 使用任务原生评分、缺分原因和指标方向。提交时记录变更范围、复现命令及已执行的验证，样本使用合成或获准公开的材料。[开发分工与 PR 规范](doc/contributor/05_开发分工与PR规范.md) · [轨迹契约](doc/contributor/02_轨迹数据契约.md)
