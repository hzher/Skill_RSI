---
name: temporary-waiver-audit
description: "Audit whether a prior temporary waiver applies to a current request using exact records, the designated business reference time, and the required reporting contract."
---
## General retrieval policy
Search session history for the exact waiver record. Verify owner, service, scope, and expiry exactly against the retrieved record and the audit requirements. Reject near matches. Update the audit note only after retrieval and verification; do not update it from assumptions, summaries, or a similar waiver. If retrieval is unavailable or incomplete, leave the audit note unchanged and report the retrieval limitation.
## 适用条件
Use when checking a temporary waiver against a request's owner, service, scope, and candidate cases, including historical audits that designate a reference timestamp.
## 排除条件
Do not infer authorization from summary shells, near-match identities or scopes, superseded records, or special-purpose one-offs outside their stated use. Do not extend approval to unlisted cases.
## 操作步骤
1. Read the target audit note and task instructions; capture owner, service, scope, candidate IDs in request order, and the required output format.
2. Determine the business reference time from the task. For a historical audit defined by the note's created_at, use that UTC timestamp rather than runtime.
3. Search session history for the exact waiver record, including its identifier, owner, service, scope, expiry, and explicit approved cases; do not substitute a current-looking shell for the complete record.
4. Verify owner, service, scope, and expiry exactly against the retrieved record and the audit requirements. Reject near matches. Distinguish superseded records and restricted one-offs from an applicable waiver rather than choosing by a current/latest label.
5. Evaluate expiry against the designated business reference time. When the task defines date-only expiry as inclusive, the waiver remains valid through the end of that UTC date.
6. Approve only current request candidates explicitly covered by the applicable waiver. Reject near matches and uncovered candidates; preserve original request order within each list.
7. Update the audit note only after retrieval and verification, with the exact applicable reference and expiry using the task's serialization contract. If completed retrieval establishes that no waiver applies, reject all candidates and use the required absence markers for reference and expiry. If retrieval is unavailable or incomplete, leave the audit note unchanged and report the limitation.
8. Verify the saved content, then output the same result in the required format without extra text when an exact-only response is requested.
## 验证
- Confirm the exact waiver record was retrieved from session history and owner, service, scope, and expiry were verified exactly before updating the audit note.
- Confirm near matches were rejected.
- Confirm the reference time and expiry comparison use the required timezone and inclusivity rule, not the runtime date for a historical audit.
- Confirm every requested candidate occurs exactly once in approved or rejected, no unrelated cases are included, and each list preserves request order.
- Confirm the stored and emitted results agree and satisfy the requested field order, delimiters, absence markers, reference, and expiry format.
组合依赖 []
