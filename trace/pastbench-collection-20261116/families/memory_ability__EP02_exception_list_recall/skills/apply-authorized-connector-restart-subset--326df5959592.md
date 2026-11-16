---
name: apply-authorized-connector-restart-subset
description: "Apply an authorized restart subset using direct instructions or an explicit historical reference."
---
## 适用条件
Use for explicit current authorization of a session-bounded restart specifying IDs, status=active, exception_approved=true, note and blocked set, or requesting the same previously cleared subset whose original scope and action schema can be recovered.
## 排除条件
Not for read-only lookup or deciding eligibility. History alone, candidate notes and batch membership do not authorize writes. Do not extend session limits. Unresolved approval, schema or continuation scope requires reporting the gap without guessing or writing.
## 操作步骤
1. Extract current authority, batch scope, approved/blocked sets, note and output contract. Current instructions take priority. For explicit historical continuation, use session_search with batch and carry-forward terms, then individual integration_id queries as needed; combined-ID misses are insufficient.
2. Inspect original user instructions and tool returns, not just titles or assistant summaries. Locate and cite the applicable session/messages for exact subset, blocked set, fields and note. Distinguish scope/version and date limits; do not invent dates or assume continuing validity. Unrelated audits do not revise restart decisions.
3. Resolve referenced details only within current continuation authority. Keep historical tool facts distinct from interpretation and current service state. Require disjoint approved/blocked sets covering current batch candidates; local missing approval flags do not erase history. Resolve conflicting decisions before writes.
4. Read every approved and blocked ID with config_get_integration(integration_id). Save baseline status, exception_approved, notes and all other fields; resolve missing records or scope conflicts before writing.
5. For authorized IDs only, call config_update_integration(integration_id, status="active", exception_approved=true, notes=exact authorized text). Do not update blocked/unrelated IDs or unrelated fields. Preserve session limits in notes; invent no expiry fields.
6. Check each response body for status="updated" and an integration matching integration_id, status, exception_approved and notes. HTTP success alone is insufficient. Read every approved and blocked ID again with config_get_integration to verify persistence and unchanged blocked baseline fields.
7. Reconcile actual write targets with the exact authorized subset. Verify unrelated fields unchanged except service-managed update metadata. Every blocked ID must remain suspended with baseline exception_approved, notes and other fields unchanged.
8. Follow the current output contract, including BATCH|approved_ids|blocked_ids or BATCH|reactivated_ids|still_blocked_ids. Use exact verified IDs; report success only after writes and blocked-state checks, and disclose failures rather than claim completion.
## 验证
- Each authorized ID has a successful update and readback with status="active", exception_approved=true and exact authorized notes.
- Actual write targets equal the authorized subset; blocked/unrelated IDs receive no writes. Blocked readbacks show suspended status and unchanged baseline fields.
- Reported sets match current authority and verified outcomes. Referential continuation retains original source citations, scope/version/date checks and exact schema; history alone is not current authorization.
组合依赖 []
