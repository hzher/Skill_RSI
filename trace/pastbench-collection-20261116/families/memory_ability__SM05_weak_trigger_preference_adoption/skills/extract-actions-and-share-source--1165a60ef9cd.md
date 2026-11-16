---
name: extract-actions-and-share-source
description: "Extract assigned action items from a note and, when requested, share that source note with the distinct action owners rather than all meeting participants."
---
## 适用条件
Use when a user requests action-item extraction from a note and explicitly asks to share the source with the owners.
## 排除条件
Do not share merely because extraction is requested. Do not treat attendees or observers as owners without an explicit assignment. Do not infer a universal priority mapping or date-year rule from a previous assistant response.
## 操作步骤
1. Locate the requested note with notes_list and read its full content with notes_get using its note_id. Resolve ambiguity about which note is intended before sharing.
2. Extract each explicit assignment into an owner, task, stated priority and stated due date; distinguish actual assignments from attendance or observer mentions.
3. Build a deduplicated recipient set from action owners only. Exclude participants who have no assigned action, and resolve ambiguous recipient identities before sharing.
4. Normalize priorities and dates according to the current request and supported source context. If the mapping or date year is unresolved, clarify rather than silently converting uncertainty into a durable rule.
5. Call notes_share with the source note_id and recipients equal to the owner set; do not substitute an extracted summary for the source note.
6. Verify the share response has status shared, the returned record.note_id matches the intended source, and record.recipients matches the exact owner set with no observer-only participants.
7. Render the extracted items in the currently requested format. If the request specifies the four-column TSV contract, use owner, priority, task, due_date in that order, High/Medium/Low priorities and YYYY/MM/DD dates; do not carry this task-only format into other requests.
## 验证
- Every output row corresponds to a source assignment, and every explicitly assigned owner is represented without adding observer-only participants.
- Confirm actual successful sharing from the tool response, including the exact source note and recipient set; rereading note content alone does not verify permissions.
- Check the requested field names, column order, priority vocabulary and date format. Do not claim a successful share when the write failed or its target set differs.
组合依赖 []
