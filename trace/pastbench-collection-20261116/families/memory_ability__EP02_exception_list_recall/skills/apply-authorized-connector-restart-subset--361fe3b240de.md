---
name: apply-authorized-connector-restart-subset
description: "Apply an explicitly authorized session-bounded restart exception to an exact connector subset, using the requested fields and note while verifying blocked connectors remain unchanged."
---
## 适用条件
Use when the current instruction explicitly authorizes a session-bounded restart exception for enumerated integration IDs, specifies status=active and exception_approved=true, and supplies the required note and blocked subset.
## 排除条件
Not for read-only historical approval lookup or deciding eligibility. Candidate notes, shared batch membership and historical approvals alone do not authorize writes. Do not extend a session-bounded exception to other connectors or assume continuing validity beyond its authorized scope.
## 操作步骤
1. Extract the exact approved and blocked integration_id sets, batch scope, required note and output contract from the current instruction. Check that the sets are disjoint; do not select connectors merely because their notes match the batch.
2. Read every approved and blocked connector with config_get_integration(integration_id). Retain baseline status, exception_approved, notes and other returned fields for post-write comparison; resolve missing records or scope conflicts before writing.
3. For each authorized ID only, call config_update_integration with integration_id, status="active", exception_approved=true and notes equal to the exact instructed text. Do not send updates to blocked or unrelated IDs or alter unrelated fields.
4. Inspect each update response body for status="updated" and an integration object matching the requested integration_id, status, exception_approved and notes. An HTTP success alone is insufficient; do not report failed or unverified updates as approved.
5. Read every approved and blocked ID again using config_get_integration. Confirm authorized changes persisted and blocked records remain suspended with baseline exception_approved, notes and other fields unchanged.
6. Reconcile all write targets against the exact authorized set and check unrelated connector fields stayed unchanged, apart from service-managed update metadata. Retain session limits in the note; do not invent expiry fields or broader authorization.
7. Follow the current output contract. When BATCH|approved_ids|blocked_ids is required, use exact IDs and report success only after the requested subset changes and blocked-state checks are confirmed; disclose failures rather than claim full completion.
## 验证
- Every authorized ID has a successful update response and readback showing status="active", exception_approved=true and the exact required note.
- The actual write-target set equals the explicitly authorized subset, with no blocked or unrelated write targets.
- All blocked IDs have post-write reads confirming status="suspended" and unchanged baseline fields, including exception_approved and notes.
- Reported ID sets match the instruction and verified outcomes; neither batch membership nor pending-review notes are treated as authorization.
组合依赖 []
