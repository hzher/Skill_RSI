---
name: process-note-action-items
description: "Extract action items from a source note, share with action owners when requested, and format the result under the current task's output rules."
---
## 适用条件
Use when reviewing a note to report action-item owners, tasks and due dates, especially when the request also requires sharing the source note.
## 排除条件
Do not infer ownership from attendance alone, distribute notes without a sharing request, or carry task-specific labels and expired exceptions into future tasks.
## 操作步骤
1. Read the source note and identify its explicit action items separately from participant and observer lists.
2. Extract each item's owner, task and due date from the note; do not invent missing fields.
3. Use the current request to determine required fields, literal labels, ordering and any applicable exceptions; do not reuse an exception the request explicitly revokes.
4. When asked to share with all action owners, deduplicate the extracted owners and share the source note with them, without adding observers solely because they attended.
5. Check the sharing result before treating distribution as complete, then render each action item using the requested output format.
## 验证
- Every explicit action item is represented with the source owner, task and due date.
- When owner-only sharing is requested, the recipients cover all distinct action owners and exclude attendees with no assigned item; confirm the tool reports success.
- The final output follows the current request's field order and literal labels, with one line per item and no extra prose when that is the required format.
组合依赖 []
