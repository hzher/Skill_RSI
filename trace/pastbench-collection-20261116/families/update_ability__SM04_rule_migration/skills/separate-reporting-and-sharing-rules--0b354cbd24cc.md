---
name: separate-reporting-and-sharing-rules
description: "Apply distinct task rules for reporting action items and sharing their source note."
---
## 适用条件
Use when a task requires both an action-item report and source-note sharing, with different inclusion rules for each.
## 排除条件
Do not retain task-specific labels, recipient names, dates, or priority thresholds as defaults. This procedure does not authorize sharing beyond the current request.
## 操作步骤
1. Identify the current task's required output fields, reporting scope, and sharing eligibility rule.
2. Read the source note and extract each action item's owner, task, due date, and any attribute used to determine sharing eligibility.
3. Build the report from all action items within reporting scope. Do not omit an item solely because its owner is ineligible to receive the note.
4. Derive the recipient list separately using the explicit sharing rule. Attendance alone does not establish eligibility.
5. Share the source note only with eligible recipients and verify the returned result. Produce the report in the requested format.
## 验证
- Every action item within reporting scope appears with the required fields, including items whose owners cannot receive the note.
- Confirmed share recipients satisfy the current sharing rule; excluded owners and unassigned observers receive no share.
- Any claim that sharing succeeded is supported by the share result.
组合依赖 []
