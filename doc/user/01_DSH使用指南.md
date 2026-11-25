# 在 DSH 中使用 Skill RSI

安装后也可运行 `skill-rsi ui`，在浏览器中点击完成接入、扩展启停与任务启动。[本机工作台教程](04_本机工作台.md)

排查构建失败时，DSH 可以先把错误附近的几行交给模型，需要时再展开完整日志。下面先试跑一次，看看策略会选中哪些行。

## 安装并接入项目

在 macOS 或 Linux 上，先确认 DSH 已配好模型，能使用 `sdk` Profile。激活 Python 3.11+ 环境。以下假设收到的 wheel 放在 `~/Downloads`，也可以换成实际路径。

```sh
python -m pip install --upgrade ~/Downloads/skill_rsi-0.5.0-py3-none-any.whl
skill-rsi --version
dsh --version
```

进入要使用 DSH 的项目根目录。首次执行 `init` 创建 `skill-rsi.json`，再用 `setup` 登记扩展和项目专用的 DSH 插件配置。已有配置时直接编辑它并执行 `setup`。

```sh
skill-rsi init --host dsh --extension builtin:tool-result-view
skill-rsi setup
```

## 跑一次长日志任务

`collect` 会带上刚登记的插件配置启动 DSH，并调用你配置的模型。下面的任务让终端工具成功返回 100 行数字和一条模拟报错。

```sh
skill-rsi collect --host dsh --prompt '请用终端工具执行 seq 1 100 && echo ERROR，然后告诉我最后一行。'
skill-rsi doctor --host dsh
skill-rsi project receipts --host dsh
```

`doctor` 中的 `loaded: true` 表示收到过宿主回调。扩展的 `pending` 表示等待合适的事件，`effective` 表示绑定已生效。默认的 `shadow` 只记录试跑结果。在回执中找到 `long_text_focused`，即可查看选中的原文行号。

## 切换功能或导入自己的扩展

- **精简长日志。** 默认处理至少 60 行的成功文本结果，定位首个 `error`、`failed` 等关键词，保留前后各 4 行。要实际交给下一次模型主调用，在配置的 `extensions` 数组中把原来的字符串换成 `{"source":"builtin:tool-result-view","mode":"apply"}`。需要原文时，把短视图里的引用传给 `skill-rsi project expand HANDLE`。
- **核对旧约束。** 例如接口的耗时单位从毫秒改成了秒。把 `builtin:constraint-check` 加入数组，用 `constraints` 指定来源文件和字段。策略记录一次完整读取后，能在后续操作依赖该字段时核对当前值。设为 `apply` 后可把核验结果交给模型。[耗时单位的配置示例](../../configs/setup-dsh.example.json)
- **换包或停用。** 将收到的 ZIP 路径（如 `./extensions/team-policy.zip`）加入数组即可导入。要停用某项就移除它。每次修改后都执行 `skill-rsi setup`，再用 `collect` 启动任务并查看回执。
