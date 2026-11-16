---
name: note-actions-and-owner-sharing
description: "Extract source-backed action items, apply an explicitly requested summary format, share the original note only with assigned owners when authorized, and verify sharing and source preservation."
---
## 适用条件
When a user asks to extract action items from a source note and explicitly authorizes sharing that note with the action owners. Apply a user-established reusable summary format when one is specified.
## 排除条件
Does not authorize sharing without a user request, sharing with all participants, editing the source note, or treating observers as action owners. A date format never authorizes inventing a missing date or year.
## 操作步骤
1. Locate the requested note within the authorized scope. When discovery is needed, use notes_list({}) and select the matching note_id; fetch the full source with notes_get({note_id}).
2. Extract each explicit assignment into owner, action item, priority, and due date. Preserve source meaning and date precision; do not invent missing fields or infer a due-date year from the event timestamp.
3. When the user establishes the team default for action-item summaries, format the result as TSV with exactly four columns in this order: owner, priority, task, due_date. Normalize urgency labels to High, Medium, or Low and format known dates as YYYY/MM/DD; do not supply a missing year or date.
4. Build a distinct recipients list from explicit action owners. Exclude observers and participants without assigned actions; do not guess an owner from attendance alone.
5. Share the original source using notes_share({note_id, recipients}), with the fetched note_id and the exact intended owner names. A summary alone does not fulfill a request to share the source note.
6. Inspect the response body: require status "shared", record.note_id equal to the requested source, and record.recipients equal as a set to the intended owners. HTTP success alone does not establish successful sharing.
7. Re-read the source with notes_get({note_id}) and compare its content with the original read. Present the requested readable summary, identify the source title and note_id, and report only verified sharing results.
## 验证
- Every summarized action is traceable to the full source content; priorities and dates retain their source meaning without unsupported additions.
- When the specified team default applies, output is TSV with exactly the columns owner, priority, task, due_date in that order; labels use High/Medium/Low and known dates use YYYY/MM/DD without invented precision.
- The sharing record confirms the exact source and intended owner set, with no observer or unassigned participant included.
- The post-share source content matches the original read, and the final response accurately identifies the source and verified sharing outcome.
组合依赖 []
