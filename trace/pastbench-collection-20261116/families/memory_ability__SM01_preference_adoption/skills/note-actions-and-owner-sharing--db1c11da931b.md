---
name: note-actions-and-owner-sharing
description: "Extract clear, source-backed action items, share the original note with exactly its assigned owners, and verify sharing and source preservation."
---
## 适用条件
When a user asks to extract action items from a source note and explicitly authorizes sharing that note with the action owners.
## 排除条件
Does not authorize sharing without a user request, sharing with all participants, editing the source note, or treating observers as action owners.
## 操作步骤
1. Locate the requested note within the authorized scope. When discovery is needed, use notes_list({}) and select the matching note_id; fetch the full source with notes_get({note_id}).
2. Extract each explicit assignment into owner, action item, priority, and due date. Preserve source meaning and date precision; do not invent missing fields or infer a due-date year from the event timestamp.
3. Build a distinct recipients list from explicit action owners. Exclude observers and participants without assigned actions; do not guess an owner from attendance alone.
4. Share the original source using notes_share({note_id, recipients}), with the fetched note_id and the exact intended owner names. A summary alone does not fulfill a request to share the source note.
5. Inspect the response body: require status "shared", record.note_id equal to the requested source, and record.recipients equal as a set to the intended owners. HTTP success alone does not establish successful sharing.
6. Re-read the source with notes_get({note_id}) and compare its content with the original read. Present a readable owner/action/priority/due table or list, identify the source title and note_id, and report only verified sharing results.
## 验证
- Every summarized action is traceable to the full source content; priorities and dates retain their source meaning without unsupported additions.
- The sharing record confirms the exact source and intended owner set, with no observer or unassigned participant included.
- The post-share source content matches the original read, and the final response accurately identifies the source and verified sharing outcome.
组合依赖 []
