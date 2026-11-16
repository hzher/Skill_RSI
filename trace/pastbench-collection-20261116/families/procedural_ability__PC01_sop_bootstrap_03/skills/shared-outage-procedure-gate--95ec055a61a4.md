---
name: shared-outage-procedure-gate
description: "Require the full governing outage procedure before applying escalation classifications."
---
## 适用条件
Shared-outage ticket triage governed by a saved skill or full SOP, especially when the KB contains only an overview.
## 排除条件
Does not define missing escalation thresholds, priority mappings, categories, or required tags.
## 操作步骤
1. Locate the governing saved skill or SOP. If a saved outage skill is available and the task requires it, read its full contents before processing tickets.
2. Check whether retrieved guidance is complete. An overview that refers to a separate SOP does not supply that SOP's escalation rules.
3. If the governing procedure is unavailable, report the missing prerequisite and withhold procedure-dependent mutations. Do not infer classification rules from correlation alone.
4. Once the procedure is available, read tickets within the authorized scope and update only those that qualify under its rules. Preserve tickets outside the mutation scope.
5. Respect any prohibition on closing tickets. Verify returned updates against the procedure and requested scope before reporting results.
## 验证
- The full governing procedure was read before procedure-dependent mutations, or those mutations were withheld and the missing prerequisite reported.
- Every update is supported by the governing rules and authorized scope; required open statuses and nonqualifying tickets are preserved.
组合依赖 []
