---
name: triage-data-export-tickets
description: "Triage data-export helpdesk requests against the current exception registry, independently evaluate export-tool approval, and apply and verify the required ticket updates."
---
## 适用条件
Use when asked to classify and update data-export tickets under a supplied sharing policy or exception registry, with helpdesk and KB tools available.
## 排除条件
Does not authorize executing exports or disclosing data. Do not reuse team exceptions, approved libraries, priorities, or note text from a prior task in place of the current governing policy.
## 操作步骤
1. Call helpdesk_list_tickets with the requested scope, then helpdesk_get_ticket for each returned ticket. Use returned identifiers and full details rather than guessed IDs or list summaries.
2. Use kb_search to find relevant sharing-policy guidance, then kb_get_article on the returned reference. A brief KB overview may point to the full registry rather than supply its rules; retain the current task's explicit procedure.
3. For each ticket, match the actual reporter or department and request type against the current registry. Apply its pre-approved or pending-approval branch without inferring authorization from business purpose alone.
4. Evaluate export-tool approval separately from data-sharing approval using explicit library evidence and the current approved-library list. An output format alone does not establish use of an approved library.
5. Build the required category, priority, sharing tags, any independently applicable tool tag, and the policy's required internal note for pending approval.
6. Call helpdesk_update_ticket for every in-scope ticket and inspect the returned ticket to confirm that the intended fields and any required note were persisted.
7. Report each ticket's identifier and confirmed priority, category, and tags in the current task's requested format. Base the report on successful updates rather than merely planned changes.
## 验证
- Every ticket returned in the requested scope has been read and receives the required update; identifiers come from tool results.
- Both KB search and full-article retrieval occurred when required; generic KB text did not substitute for explicit registry rules.
- Sharing approval and tool approval were evaluated independently; unsupported requests remain in the policy's pending-approval branch.
- Returned ticket fields match the policy-derived updates, including any required pending-approval note, before the final report.
组合依赖 []
