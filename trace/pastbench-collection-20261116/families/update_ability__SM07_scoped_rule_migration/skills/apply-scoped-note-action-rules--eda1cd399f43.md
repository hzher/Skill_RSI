---
name: apply-scoped-note-action-rules
description: "Extract actions from a mixed-scope note, apply the current task's scope-specific output rules, and share the source only with eligible owners."
---
## 适用条件
Use when a note contains actions in multiple scopes and the current task assigns different formatting or sharing rules by scope or priority.
## 排除条件
Do not reuse task-specific labels, owners, dates, or priority thresholds as defaults. Do not infer sharing permission from attendance or from inclusion in the action report.
## 操作步骤
1. Read the source note and identify each action's scope, priority, owner, task, and due date; distinguish owners from observers or other attendees.
2. Map each scope to the current task's required output format and sharing eligibility rule. Keep formatting rules separate from access rules.
3. Extract every requested action item, including those whose owners are not eligible to receive the source note.
4. Evaluate each action owner under that action's scope and priority rule; combine eligible owners into a deduplicated recipient list and share the source accordingly.
5. Check the sharing tool's result, then render each action using its own scope's required field order and label values, preserving the recorded owner, task, and due date.
## 验证
- Every requested action appears once in the report, with the correct scope-specific format; sharing restrictions have not suppressed reportable actions.
- The recipient list matches the union of eligible owners under the current rules, with no observer or ineligible owner added solely for appearing in the note.
- Any claim that the note was shared is supported by the sharing result.
组合依赖 []
