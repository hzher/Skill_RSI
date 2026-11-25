# Codex 轨迹涌现组件

把“给 Codex 轨迹位置和 prompt，让它自己研究并写包”做成安装包内的一条独立流程。默认模型为本机账号的 `gpt-6.1-sol`，推理强度 `ultra`。候选是什么、采用什么算法、生成几个，由 Codex 根据证据决定。

```mermaid
flowchart LR
    A[选择已采集主会话] --> B[冻结完整脱敏轨迹]
    B --> C[Codex 自主读取、研究、写包]
    C --> D[逐包结构校验与 ZIP]
    D --> E[人审功能、场景和实现]
    E --> F[手动安装、启停与效果判断]
```

## 入口与分工

| 入口 | 作用 |
|---|---|
| `skill_rsi.emergence.start` | 创建一次冻结输入，启动独立 Codex 进程；重复点击复用正在运行的任务。 |
| `skill_rsi.emergence.poll` | 读取进度，结束后逐个校验和打包候选，保留每包错误。 |
| `supervisor.py` | 独立进程跟踪指定生成编号并自动收尾；工作台启动时恢复遗漏的收尾，不重复启动模型。 |
| `snapshot.py` | 保留全部完整可见行和子流，不按固定数量抽样、不截断长输出；排除私有推理与可识别凭据。 |
| `sdk.py` | 从当前安装包提供真实清单、运行协议和示例，不固定研究结论。 |
| `check.py` | 检查清单、文件布局、人审说明和摘要，不导入执行候选代码。 |
| `discovery/codex_settings.py` | 单独保存 CLI、模型、强度；不改用户的 Codex 账号或全局模型配置。 |

UI 使用 `POST /api/emergence` 和 `GET /api/emergence?task_id=...`。终端使用 `skill-rsi emerge --task 任务编号`，附加 `--status` 查看进度，或 `--wait` 等待结果。源任务目录也可直接作为 `--task` 的值。

每次生成使用独立收尾锁，页面轮询与后台收尾可以重复调用。旧任务只更新自己的 `job.json`，不会覆盖新一轮的当前指针。Codex 的辅助脚本使用安装包所在环境的绝对 Python 路径。进度接口将模型进程状态、最终候选校验结果、过程命令非零退出和运行提示分别返回；日志统计截取时注明范围。

## 文件位置

```text
原任务/analysis/emergence/<生成编号>/
├── job.json                 # 过程、参数、产物和校验状态
├── prompt.txt               # 本次交给 Codex 的要求
├── snapshot/                # 完整脱敏快照、原行定位和 SHA 索引
├── sdk/                     # 本次使用的开发契约
├── workspace/
│   ├── REPORT.md            # Codex 自由撰写的发现与限制
│   └── components/<候选>/
│       ├── manifest.json
│       ├── payload/         # 实际说明、代码或配置
│       ├── README.md
│       └── review.json      # 功能、分类、适用、跳过、机制和验证建议
└── artifacts/               # 通过静态校验的 ZIP
```

`source-index.jsonl` 保留原行 SHA、字节位置与脱敏后的 SHA。冻结输入只读，进程结束后再核对摘要。模型命令使用 workspace-write，只给独立工作目录写权限；读取范围还受 prompt 约束，不能将它描述为文件读取隔离。

## 人怎样判断产物

报告、包的源码、功能、使用场景、跳过条件与文件位置直接出现在界面。允许零候选；一个候选失败不遮住其他候选。安装与启用分开，由人选择宿主和模式。

清单有效、宿主可执行、策略有效是不同结果。Skill 仍依赖实际交付节点；新增 Python/native 候选需要对应执行器；Wasm 需要运行器及 ABI 适配。框架不因 Codex 写出了代码就授予可信执行权限。候选收益须在独立任务上由人判断。

旧 API 分析和本机规则保留在高级入口，新流程不使用旧的 60 条摘要、固定 Skill 标题或内置策略选择器。

回归：`python -m unittest discover -s tests/emergence -p 'test_*.py'`。真实模型验收另核对 CLI 参数、实际生成文件、候选 ZIP 和可回查来源，不能用模拟结果代替。
