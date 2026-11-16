---
name: triage-exports-with-verified-exception-registry
description: "Classify and update data-export tickets using a fully retrieved exception registry, evaluating export eligibility and approved-tool conditions separately."
---
## 适用条件
Use when triaging data-export requests and the authoritative exception registry is available in full with eligibility criteria and ticket-update rules. Follow current task instructions and the retrieved policy.
## 排除条件
Does not perform data exports or create approval authority. Does not infer eligibility when the required approval source is unavailable, or treat ticket IDs, reporter names, or tool approval alone as export authorization.
## 操作步骤
1. Use helpdesk_list_tickets to identify tickets in scope and helpdesk_get_ticket for each candidate to read its full request and current fields.
2. Search the knowledge base for the exception registry and retrieve the full policy article; use its complete rules rather than search snippets.
3. Extract reusable eligibility criteria, purpose restrictions, priority/category/tag mappings, required notes, and any separate approved-tool conditions from the current policy.
4. Match each ticket's department and stated purpose against the eligibility rules. Apply standing approval only when its conditions are met; follow the policy's pending-approval route otherwise.
5. Evaluate the stated export tool independently. Apply a tool-approval tag only when the tool satisfies policy; tool approval alone does not establish permission to export.
6. Derive priority, category, tags, and required internal notes from the matching policy rules. Do not reuse ticket identities or field values from a prior batch as decision rules.
7. Use helpdesk_update_ticket for each in-scope request. Include required pending-approval notes and do not imply that triage performed an export or supplied missing sign-off.
8. Check each update response and report confirmed priority, category, and tags in the user's requested format. Do not report attempted or failed updates as completed.
## 验证
- Every in-scope ticket has full-detail retrieval and a successful update response before it is reported as updated.
- Each eligibility decision follows the retrieved registry's reusable criteria and any purpose restrictions; requests needing additional sign-off retain the required pending status and note.
- Tool approval is assessed separately from export eligibility, using the tool stated in the request and the policy's approved-tool criteria.
- Reported fields match confirmed update responses and the user's output format; priority and tags are sourced from current policy rather than memorized batch values.
组合依赖 []
