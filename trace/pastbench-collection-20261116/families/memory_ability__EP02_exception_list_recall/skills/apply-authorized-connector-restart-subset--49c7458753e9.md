---
name: apply-authorized-connector-restart-subset
description: "Apply an explicitly authorized session-bounded restart exception to an exact connector subset, using requested fields and note while verifying blocked connectors remain unchanged."
---
## 适用条件
Use when the current instruction authorizes an exact session-bounded restart subset and supplies status, approval flag, note and blocked IDs, or explicitly directs the same narrow handling as an earlier settled decision recoverable from authorized session evidence.
## 排除条件
Not for read-only lookup or deciding eligibility. Batch notes, caches, memory and historical approval alone do not authorize writes. Do not extend the exception beyond its authorized scope. Resolve uncertainty before writing.
## 操作步骤
1. Extract exact approved and blocked integration_id sets, batch scope, required note and output contract from the current instruction. For delegated carry-forward, recover them from the original authorized session record; check the sets are disjoint.
2. Search session history using the batch and carry-forward terms, then target IDs individually if needed. Inspect original approval messages and tool returns; a combined-ID query alone is insufficient. Distinguish empty successful searches from unavailable searches.
3. Resolve subset, action fields and scope limits from original evidence, not cache or memory. Keep tool-return facts separate from later interpretation and current service state. Do not invent dates or assume ongoing validity; the current instruction must authorize carry-forward.
4. Read every approved and blocked connector with config_get_integration(integration_id). Retain baseline status, exception_approved, notes and other fields; resolve missing records or scope conflicts before writing.
5. For each authorized ID only, call config_update_integration with integration_id, status="active", exception_approved=true and notes equal to the exact instructed text. Do not update blocked or unrelated IDs or alter other fields.
6. Check each update response body for status="updated" and a matching integration object with requested ID, status, approval flag and note. HTTP success alone is insufficient.
7. Read every approved and blocked ID again. Confirm authorized changes persisted and blocked records remain suspended with baseline fields unchanged.
8. Reconcile write targets with the exact authorized set and check unrelated fields remain unchanged apart from service-managed metadata. Do not invent expiry fields or broader authorization.
9. Follow the requested output contract, including BATCH|approved_ids|blocked_ids or BATCH|reactivated_ids|still_blocked_ids. Use exact IDs and report success only after write and blocked-state verification.
## 验证
- For delegated carry-forward, verify exact subsets and scope from the original authorized session record; cache and memory claims do not override it.
- Every authorized ID has a successful update body and readback showing status="active", exception_approved=true and the exact required note.
- The actual write-target set equals the authorized subset, with no blocked or unrelated targets.
- Every blocked ID reads suspended with baseline exception_approved, notes and other fields unchanged.
- Reported ID sets match the instruction and verified outcomes; candidate notes are not authorization.
组合依赖 []
