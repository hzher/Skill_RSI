---
name: audit-structured-decision-logs
description: 审计规范化的扑克决策双臂日志，复原策略线索在何时对模型可见、哪次动作首次分歧，以及各臂结算的净筹码。
---

# 审计双臂决策日志

收到公开 JSON 输入包后，从本目录运行随包脚本。它接受 `cases[]`；每例包含唯一的 `case_id`、`arms.A` 和 `arms.B` 两列事件。每条事件含 `attempt`、`thread`、`seq`、`event` 和 `payload`。同一臂有多个尝试或线程时，以该例的 `target_bindings.A` 或 `target_bindings.B` 指定 `[attempt, thread]`。接入原始轨迹时，先按模型实际看到的输入、动作回执和结算回执建立这些绑定。

在 Bash 中运行

```bash
python -X utf8 -I -B scripts/public_cli.py < public-packet.json > audit-result.json
```

在 PowerShell 中运行

```powershell
cmd /d /c "python -X utf8 -I -B scripts\public_cli.py < public-packet.json > audit-result.json"
```

只取退出码 0 的输出，按 `case_id` 读取。每臂的 `*_at_first` 是首次行动前能否识别策略，`*_first_affected_action` 给出首次有可解释线索的行动序号，`*_net_chips` 是按原始结算回执去重的净筹码。两臂的 `first_divergence` 和 `identical_first_action` 同时比较动作类型与金额。动作序号从 1 开始，无对应位置时为 `null`。

仅有服务端记录时保留可见性的 `unknown`。行动前的输入覆盖完整、且没有可识别线索时才记为 `no`。`adoption_causal` 固定为 `unknown`，归因还需要独立的干预证据。缺少可绑定的动作或结算等证据时，脚本写出 `evidence_gap` 并以退出码 2 结束；保留原日志，补足证据后再审计。

本包是运行结束后的审计工具。成本实验以同一公开包的整例九字段正确数、模型调用次数及 token 用量为指标。提示文字的实际采用，以及对模型下注、胜手数和原生 reward 的影响，仍待各自的实验验证。
