---
name: share-action-notes-with-access-checks
description: "Extract action items from a planning note while independently determining authorized source-note recipients and verifying the actual share."
---
## 适用条件
Use when asked to extract action items and share the source note, especially when the note or authoritative policy restricts recipients by employee status or external-adviser membership.
## 排除条件
Do not assume attendance or action ownership grants access. Observer status alone is not a universal prohibition. Do not infer employee status merely from absence on an external roster, or claim a listed older note is today's note.
## 操作步骤
1. Locate candidate notes and inspect their titles and dates. If today's note cannot be established, disclose the mismatch and resolve material selection uncertainty before sharing an unconfirmed substitute.
2. Read the complete selected note using notes_get with note_id; inspect action items, participants, and any applicable distribution restrictions before writing.
3. Extract each action item's owner, task, and stated deadline or dependency, including owners who are ineligible to receive the source note.
4. Build candidate recipients from the user's intended audience and relevant action owners, not automatically from every participant. An observer needs an independent reason for inclusion.
5. Apply authoritative access restrictions to every candidate. For an employee-only internal note, exclude external advisory roster members even if they own action items. Resolve uncertain eligibility rather than guessing.
6. Consult the applicable roster or restriction for this note and context; do not reuse names from an unrelated note as a permanent exclusion list.
7. Call notes_share with the selected note_id and the deduplicated authorized recipients array. Inspect the returned status and record rather than assuming the request succeeded.
8. Report the extracted actions, confirmed source-note recipients, relevant exclusions, and unresolved date or eligibility limitations separately.
## 验证
- Every explicit action item remains in the extraction even when its owner is excluded from source-note sharing.
- The successful notes_share record has the intended note_id and exactly the authorized recipients; no excluded person appears. A request alone is not proof of sharing.
- Source-note content is not edited during extraction or sharing; if reread, compare it with the original. Do not claim broader access-state verification from a content-only read.
- Do not claim the requested date was verified when the available note's date differs or remains uncertain.
组合依赖 []
