---
name: extract-actions-and-share-source-note
description: "Extract actions and share their source using standing defaults."
---
## 适用条件
Action extraction with authority to share the source with owners.
## 排除条件
Does not authorize executing actions, creating tasks or sharing with all participants. A sole result does not prove a date match.
## 操作步骤
1. Discover with notes_list({max_results: 50}); inspect notes, total, note_id, title, created_at. If total exceeds returned notes, discovery is incomplete; no pagination schema is established.
2. Compare source metadata with the requested calendar date. Resolve date conflicts or uncertain source identity before sharing; a sole result does not establish a match.
3. Read notes_get({note_id}). Extract owner, action, priority and due date from assignments, not participants. Do not invent missing values or deadline years.
4. Read defaults with memory({action: "read"}). For future similar summaries, use TSV with exactly four columns in order: owner, priority, task, due_date, unless current instructions override. Keep status prose outside TSV.
5. Use High, Medium, or Low for source-backed priorities as a standing style unless overridden. Do not invent missing priorities or universal synonym mappings; resolve or disclose ambiguous normalization.
6. Use YYYY/MM/DD for known due dates in future similar summaries unless overridden. Disclose missing years as unresolved; this format does not justify inferring a year from the note date.
7. When asked to save defaults or adopt a note's standing preference, use memory({action: "add", text: ...}) with scope, TSV order, priority style and YYYY/MM/DD. Preserve existing defaults. Confirm the matching entry and persisted: true via memory({action: "read"}) before claiming persistence.
8. Build distinct recipients from assigned owners. Exclude observers and unassigned participants unless separately authorized. Resolve ambiguous identities before sharing.
9. Use notes_share({note_id, recipients}) for the confirmed extraction source. Require status shared and a record matching note_id and the exact intended recipient set; disclose mismatches or uncertainty.
10. Reread notes_get({note_id}); compare content with extraction. Report actions, verified sharing and unresolved source-date limitations. Rereading content does not independently verify access permissions.
## 验证
- Resolve source identity and date constraints before sharing; actions contain no invented priorities or dates, including years.
- Unless overridden, TSV has header owner, priority, task, due_date in order, with exactly four fields per row.
- Unless overridden, source-backed priorities use High, Medium, or Low and known dates use YYYY/MM/DD; disclose unresolved priorities or date components.
- When saving or adoption is requested, readback confirms scoped TSV, priority and date defaults with persisted: true; otherwise disclose persistence uncertainty.
- Share record matches intended note_id and exact authorized owners, excluding observers and unassigned participants.
- Post-share content matches extraction; report distinguishes confirmed sharing from unresolved facts.
组合依赖 []
