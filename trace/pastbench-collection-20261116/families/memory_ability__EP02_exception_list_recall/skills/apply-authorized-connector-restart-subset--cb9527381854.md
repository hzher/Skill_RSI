---
name: apply-authorized-connector-restart-subset
description: "Apply only an authorized connector restart subset."
---
## 适用条件
Use for explicit session-bounded restart authorization specifying IDs, fields and note, or an explicit current request to reactivate the same previously cleared subset whose applicable details can be recovered.
## 排除条件
Not for read-only lookup or new eligibility decisions. Historical approval alone, candidate notes and batch membership do not authorize writes. Do not widen scope or assume continuing validity.
## 操作步骤
1. Extract current scope and output contract. For direct authorization capture disjoint approved/blocked integration_id sets, status="active", exception_approved=true and exact required note.
2. For explicit carry-forward, list the current queue with config_list_integrations. Search authorized history via session_search for carry-forward terms, batch aliases and individual integration_id values; empty label or combined-ID queries are not conclusive.
3. Inspect original historical user authorization and tool returns; retain session/call references. Recover exact IDs, fields, note and scope/version limits. Separate original facts from assistant summaries, uncertain dates and current service state. Resolve missing details or ambiguity before writes.
4. Require current authority to reuse that applicable decision. Reconcile recovered IDs with the current queue; additional candidates stay blocked. Draft annotations and unrelated audits do not expand authorization.
5. Read every approved and blocked ID with config_get_integration(integration_id); retain baseline status, exception_approved, notes and other fields. Resolve missing records or scope conflicts before writes.
6. For authorized IDs only, call config_update_integration with integration_id, status="active", exception_approved=true and notes equal to exact authorized text, recovered when incorporated by reference. Alter no unrelated fields; send no blocked/unrelated updates.
7. Require update body status="updated" and integration matching integration_id, status, exception_approved and notes, not merely HTTP success. Read every approved/blocked ID again with config_get_integration; confirm persistence and blocked suspension with baseline fields unchanged.
8. Reconcile write targets to exact authorization; permit only service-managed metadata changes beyond requested fields. Retain session limits in notes; invent no expiry fields. Follow current output, e.g. BATCH|reactivated_ids|still_blocked_ids or BATCH|approved_ids|blocked_ids; disclose failures.
## 验证
- Every authorized ID has successful update and readback: status="active", exception_approved=true and exact note.
- Write-target set equals authorized subset; no blocked/unrelated writes or unrelated field changes except service-managed metadata.
- Post-write reads show every blocked ID suspended with baseline exception_approved, notes and other fields unchanged, including additional in-scope candidates.
- Carry-forward has original source references and current authority within applicable limits; historical success alone is not authority.
- Reported exact IDs reflect verified outcomes and partition the current in-scope queue; no omitted or unrelated records.
组合依赖 []
