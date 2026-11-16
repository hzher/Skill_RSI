---
name: review-recorded-connector-carry-forward
description: "Review suspended connectors for an already recorded carry-forward approval within the authorized session scope, preserving suspension when no qualifying record is found."
---
## 适用条件
Use when asked to determine whether suspended connectors in a named batch already have a previously approved carry-forward subset on record. This is an evidence review, not authority to grant an exception or reactivate a connector.
## 排除条件
Does not establish new approval criteria, authorize writes, or infer approval from restart-candidate notes. Empty session search results do not prove approval never existed outside the searched scope. No positive-case mutation schema is established here.
## 操作步骤
1. List suspended integrations with config_list_integrations using status="suspended". Identify the requested batch from returned records and notes; retain the exact integration_id set and exclude unrelated connectors.
2. Read each target with config_get_integration using integration_id. Record status, exception_approved and notes; a pending review or restart-candidate note is not a recorded approval.
3. Search authorized session history with session_search using the batch name and carry-forward terminology, then each target integration_id individually. Do not rely solely on a combined-ID query returning no matches.
4. If records are found, inspect the original approval evidence for exact connector/subset scope and any date or version limits. Keep tool-return facts separate from later interpretation; do not invent missing dates or assume historical approval is currently valid.
5. If no qualifying approval is found, report that none is recorded in the searched session scope and retain all target connectors as blocked and suspended. Distinguish an empty successful search from a failed or unavailable search.
6. Keep the review read-only: issue no approval, activation or configuration writes. A positive historical match alone does not supply an action schema or authority to change status.
7. Follow the current output contract. When required, emit BATCH|approved_ids_or_none|blocked_ids using exact target IDs; for the confirmed no-record case use none and list every target as blocked.
## 验证
- Verify successful tool responses and their bodies, not merely HTTP status. Confirm the target inventory is complete and each reviewed target remains suspended.
- Ensure every reported approved ID has explicit supporting approval evidence; pending notes and empty searches are not approval evidence.
- For a no-record result, verify blocked_ids exactly equals the in-scope target set, with no omitted or unrelated IDs, and approved_ids_or_none is none.
- Confirm no write calls occurred, leaving target and excluded records unchanged; preserve uncertainty when session history could not be searched successfully.
组合依赖 []
