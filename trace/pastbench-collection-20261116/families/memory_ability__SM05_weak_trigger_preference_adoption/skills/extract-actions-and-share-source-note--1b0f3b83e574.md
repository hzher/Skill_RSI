---
name: extract-actions-and-share-source-note
description: "Extract source-backed action items, apply standing summary defaults, and share the confirmed source note with authorized action owners."
---
## 适用条件
Use for action extraction with authorization to share the source with action owners.
## 排除条件
No authority to execute actions, create tasks or share with all participants. A sole note does not prove a requested date match.
## 操作步骤
1. Discover notes with notes_list({max_results: 50}); inspect notes, total, note_id, title and created_at. If total exceeds returned notes, discovery is incomplete; no pagination schema is established.
2. Compare source date metadata with the requested calendar date. Resolve conflicts or uncertain source identity before sharing; being the only result does not establish a match.
3. Read with notes_get({note_id}). Extract owner, action, priority and due date from assignments, not participants. Do not invent missing values or a due-date year.
4. Default to TSV with exactly four columns: owner, priority, task, due_date, in that order. Use this durable team default in future similar sessions unless current instructions override it. Put status prose outside TSV.
5. Normalize source-backed priority labels as High, Medium, or Low in similar action-item summaries as a standing style, unless current instructions override it. Do not invent missing priorities or assume a universal synonym mapping; resolve or disclose ambiguous normalization.
6. When asked to save defaults, use memory({action: "add", text: ...}) with scope, TSV format and column order, and the standing priority-label style. Verify via memory({action: "read"}) that the matching entry exists with persisted: true before claiming persistence.
7. Build distinct recipients from explicit action owners. Exclude observers and unassigned participants unless separately authorized. Resolve ambiguous identities before sharing.
8. Call notes_share({note_id, recipients}) for the same confirmed note read for extraction. Require status shared and a record matching the intended note_id and exact recipient set; disclose mismatches or uncertain outcomes.
9. Reread with notes_get({note_id}) and compare content with the extracted version. Report actions, verified sharing and unresolved source-date limitations. This reread does not independently verify access permissions.
## 验证
- Source and date constraints are resolved before sharing; each action is source-backed without invented priority or date details.
- Unless overridden, TSV has header owner, priority, task, due_date in that order and exactly four fields per row.
- Unless overridden, assigned priority labels are High, Medium, or Low; missing or ambiguous source priorities are not silently guessed.
- When saving is requested, memory readback confirms the scoped defaults, including priority style, and persisted: true, or persistence uncertainty is disclosed.
- Share record confirms the intended note_id and exact authorized owners, excluding observers and other unassigned participants.
- Post-share source content matches extraction; the report distinguishes confirmed sharing from unresolved facts.
组合依赖 []
