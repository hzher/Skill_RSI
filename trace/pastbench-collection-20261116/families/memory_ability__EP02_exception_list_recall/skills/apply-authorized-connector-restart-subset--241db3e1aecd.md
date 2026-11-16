---
name: apply-authorized-connector-restart-subset
description: "Apply an authorized restart subset with verified writes."
---
## 适用条件
Use for an explicit session-bounded restart authorization, including a current request to reactivate an earlier settled subset whose IDs, fields and note are recoverable from authorized history.
## 排除条件
Not for read-only review or granting eligibility. Historical approval alone, batch membership and candidate or scratchpad notes do not authorize writes. Never extend an exception beyond its applicable scope.
## 操作步骤
1. Extract current authority, batch and output contract. Obtain exact disjoint approved/blocked integration_id sets, status="active", exception_approved=true and required note, either directly or through an explicitly referenced decision.
2. For referenced decisions, list the queue with config_list_integrations. Search authorized history with session_search by batch, carry-forward terms and individual IDs as needed. Resolve membership from history if notes lack it; scratchpad fast-lane tags are not authority.
3. Inspect original user authorization and historical tool bodies, not just titles or assistant summaries; retain source session/message references. Distinguish earlier no-record reviews from later approvals. Check scope, version, session and date limits without inventing dates or assuming continuing validity from reset state.
4. Require current authority to reapply the recovered schema and exact note within its limits. If authority or schema is unresolved, preserve suspension and disclose the gap. Read every approved and blocked ID with config_get_integration; save all baseline fields and resolve missing records or scope conflicts before writing.
5. For authorized IDs only, call config_update_integration(integration_id, status="active", exception_approved=true, notes=exact required text). Never write blocked or unrelated IDs or unrelated fields. Preserve session limits in the note; do not invent expiry fields.
6. Check each response body for status="updated" and an integration matching the requested integration_id, status, exception_approved and notes. HTTP success alone is insufficient. Read every approved and blocked ID again with config_get_integration to confirm persistence and unchanged blocked records.
7. Reconcile actual write targets with the exact authorized set. Verify unchanged unrelated fields except service-managed update metadata. Follow the current output contract, including BATCH|reactivated_ids|still_blocked_ids when requested; use exact verified IDs and disclose failures rather than claim full success.
## 验证
- Every authorized ID has a successful update body and readback with status="active", exception_approved=true and the exact required note.
- Write targets equal the authorized subset, with no blocked or unrelated writes; unrelated fields remain unchanged except service-managed metadata.
- All blocked IDs remain suspended with baseline exception_approved, notes and other fields unchanged on readback.
- Referenced authority is traceable to original instructions and tool facts, distinct from assistant interpretation and current state. Scope and limits apply; unresolved gaps are explicit.
- Output sets match current instructions and verified outcomes, never scratchpad labels or pending-review notes.
组合依赖 []
