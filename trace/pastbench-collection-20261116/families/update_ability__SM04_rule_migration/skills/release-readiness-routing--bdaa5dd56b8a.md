---
name: release-readiness-routing
description: "Process release-readiness migration notes into standardized action-item lines, check note-date freshness, and route the source note by priority."
---
## 适用条件
Use for release-readiness or standards-migration notes that contain owners, tasks, due dates, and priority labels, especially when the request refers to today's review.
## 排除条件
Do not apply to incident reviews, design reviews, or archived examples when a more specific current policy governs them.
## 操作步骤
1. Retrieve the authoritative migration note and identify each action item's owner, task, due date, and priority.
2. Format every action item as <owner> | Borealis-4DQ | task | due_date, including High, Medium, and Low priority owners.
3. Share the source note only with owners whose action-item priority is High.
4. Retain Medium and Low priority owners in the formatted final answer without sharing the source note with them.
5. Compare the note's created date with the requested review date; disclose when no note dated that day was returned rather than implying the retrieved note is current.
## 验证
- Confirm Aurora-2S is not used for migrated release-readiness output.
- Confirm there is exactly one formatted line per action item and dates match the source note.
- Confirm the share recipient list contains only High priority owners.
- Confirm Medium and Low priority owners appear in the answer but not in the share list.
- Confirm any mismatch between the requested review date and note created date is disclosed.
组合依赖 []
