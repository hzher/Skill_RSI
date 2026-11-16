---
name: share-source-note-with-action-owners
description: "Share a source note with the owners explicitly assigned its action items, distinguishing ownership from mere attendance."
---
## 适用条件
Use when the user authorizes sharing a source note with action owners and the note identifies assignments and participants.
## 排除条件
Do not use attendance alone as evidence of ownership. Explicit user recipient instructions take priority. If ownership is ambiguous, clarify rather than expanding access to all participants.
## 操作步骤
1. Read the full source note and retain its identifier for the sharing operation.
2. Extract each action item and its explicitly assigned owner; distinguish owners from observers and other attendees.
3. Build a deduplicated recipient list from the assigned owners, applying any explicit user recipient instructions.
4. Share the original source note with those recipients using the retained note identifier.
5. Check the sharing result before reporting success, and summarize the assignments and confirmed recipients without claiming unverified delivery.
## 验证
- Every selected recipient is supported by an explicit action assignment or a user instruction; observers are not included solely because they attended.
- The sharing result confirms the intended source note and recipient list.
组合依赖 []
