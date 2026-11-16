---
name: extract-actions-and-share-source-note
description: "Extract owner, action, priority and due date from a source note, then share that note with its action owners after confirming the intended source."
---
## 适用条件
Use when the user requests action-item extraction and authorizes sharing the source note with action owners.
## 排除条件
Does not authorize executing the actions, creating tasks, or sharing with all participants. Do not treat a sole available note as proof that it matches a requested date.
## 操作步骤
1. Locate candidate notes with notes_list using max_results. Inspect notes, total, note_id, title and created_at; the observed call used max_results: 50. If total exceeds returned notes, discovery is incomplete; no pagination schema is established by this evidence.
2. For a request such as today's note, compare available date metadata with the requested calendar date. If it conflicts or cannot identify the intended note, resolve the source before sharing. Being the only result does not establish a date match.
3. Read the selected source with notes_get({note_id}). Derive owners from assigned action text, not the participants list. Extract each owner's action, priority and due date as stated; do not invent missing values or a due-date year.
4. Build recipients from the distinct explicit action owners. Exclude observers and participants without an assigned action unless the user separately authorizes including them. Resolve ambiguous owner identities before sharing.
5. Share the confirmed source using notes_share({note_id, recipients}), with the same note_id read for extraction and the exact intended owner set.
6. Inspect the share result: require status shared and a record whose note_id and recipients match the intended source and exact owner set. Tool acceptance alone does not verify correct recipients; disclose any mismatch or uncertain outcome.
7. Read the source again with notes_get({note_id}) and compare its content with the extracted version. Report the extracted actions, the verified sharing outcome and any unresolved source-date limitation. Do not claim the reread independently verifies access permissions.
## 验证
- The selected note satisfies the user's source and date constraints before sharing; unresolved identity or date conflicts are not silently bypassed.
- Every extracted action is traceable to the source, and missing priority or date details have not been invented.
- The returned share record confirms the intended note_id and exactly the authorized recipients, with no observer-only or otherwise excluded participants added.
- The post-share source content matches the version used for extraction, and the final report distinguishes confirmed sharing from unresolved facts.
组合依赖 []
