---
name: extract-actions-and-share-source-note
description: "Extract assigned actions from a meeting note and share the original note with the distinct action owners, preserving source wording and verifying the recipient set."
---
## 适用条件
Use when the user requests action-item extraction from a provided note and authorizes sharing that source note with its owners.
## 排除条件
Does not authorize sharing with all attendees, creating task records, or changing the source note. Follow any current read/search restrictions; do not retrieve excluded material.
## 操作步骤
1. Locate the requested note using the supplied reference or available note listing; read its complete content before extracting actions or selecting recipients.
2. Extract each action's owner, requested work, priority or status, and due date. Preserve meaningful conditions and deferred actions; do not infer a due-date year from tool timestamps.
3. Build a deduplicated recipient set from explicit action owners. Include owners of deferred actions; exclude participants identified only as observers or attendees without assigned actions.
4. Share the original source note using notes_share with note_id identifying the read note and recipients containing exactly the owner set, within the user's authorization.
5. Inspect the response body for status shared and verify record.note_id and record.recipients match the intended note and exact owner set. Treat transport success alone as insufficient.
6. Present the extracted actions with their owners, priorities or statuses, and source-stated due dates. Report sharing as complete only when the response confirms it; otherwise state the unresolved result.
## 验证
- Each extracted action retains its source owner, qualifiers, status, and due date without an unsupported year.
- The intended and returned recipient sets equal the distinct action-owner set; observer-only participants are excluded.
- The share response confirms status shared for the original note_id, with no unintended recipient additions.
组合依赖 []
