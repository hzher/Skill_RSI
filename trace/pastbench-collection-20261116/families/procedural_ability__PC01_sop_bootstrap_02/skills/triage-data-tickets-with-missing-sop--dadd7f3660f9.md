---
name: triage-data-tickets-with-missing-sop
description: "Inspect data-analysis helpdesk tickets and handle unavailable SOP details through scoped escalation notes without inventing pipeline, priority, or category rules."
---
## 适用条件
Use when asked to triage data-analysis tickets under an SOP and the full governing procedure cannot be retrieved. Follow current task instructions for permitted mutations and escalation.
## 排除条件
Does not supply the data-analysis pipeline or classification rules. Does not authorize updates to infrastructure or other excluded tickets. Use the full governing SOP when available.
## 操作步骤
1. Use helpdesk_list_tickets for the requested status and helpdesk_get_ticket for each listed ticket; determine scope from full ticket details.
2. If a saved pipeline SOP skill is available, read its complete contents before triaging. Use knowledge-base search and article retrieval to seek any needed full governing procedure.
3. Read the retrieved source and follow references to the detailed SOP. An overview or search snippet that lists stages does not establish classification or priority rules.
4. If searches return only the overview, record that the full SOP was not retrieved. Do not infer detailed rules or claim that the SOP does not exist.
5. Identify in-scope data-analysis issues from ticket facts. Leave unrelated or explicitly excluded tickets unchanged.
6. When escalation is within the authorized scope, use helpdesk_update_ticket to add an internal note describing the issue, the unavailable governing source, and the need to obtain it for classification.
7. Preserve existing priority and category when the required rules are unavailable. Keep the note factual and distinguish requested escalation from a completed handoff.
8. Check update responses for success and field preservation. Report actual values and actions in the user's requested format, including unchanged excluded tickets when requested.
## 验证
- Every listed ticket was inspected using its full details, and only tickets within the requested mutation scope were updated.
- No pipeline sequence, priority, or category was inferred from a brief; missing-source notes identify the unresolved classification work.
- Successful update responses confirm the intended notes and preserved fields; the final report accurately distinguishes updates from unchanged tickets.
组合依赖 []
