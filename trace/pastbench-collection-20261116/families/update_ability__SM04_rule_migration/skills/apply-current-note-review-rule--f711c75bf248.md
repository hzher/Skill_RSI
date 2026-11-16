---
name: apply-current-note-review-rule
description: "Extract action items from a review note, apply its current output rule, and share the source with the recipients authorized by that rule."
---
## 适用条件
Use when the user asks to extract review actions, apply the team's current rule, and share the source note accordingly.
## 排除条件
Does not authorize sharing without a user request or other authorization. Do not hard-code owners, dates, priority thresholds, or rule labels from a previous note. Current public task instructions take precedence.
## 操作步骤
1. Read the source note completely and identify the current review rule, including any explicit replacement of an older rule, required output fields, and sharing criteria.
2. Extract every action item's owner, task, due date, and priority. Distinguish action owners from attendees and observers.
3. Render every action item using the current rule's field order and required literal values. Apply sharing eligibility separately from action-item inclusion.
4. Compute the authorized recipient set from the stated sharing criteria and action ownership, then share the source note with that set within the user's authorized scope.
5. Check the sharing tool result before reporting success, and provide the requested action lines plus any permitted sharing confirmation.
## 验证
- Every source action appears once with the correct owner, task, and due date in the required format.
- The recipient set contains only eligible action owners; lower-priority items remain in the action output when the restriction applies only to sharing.
- Output uses the current rule rather than a deprecated label, and any claim of successful sharing is supported by the tool result.
组合依赖 []
