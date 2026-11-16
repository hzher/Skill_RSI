---
name: correlate-migration-risk-tickets
description: "Triage database migration tickets using same-ticket correlation of region, schema upgrade path, and shard configuration; escalate only complete matches to the current SOP."
---
## 适用条件
Use when helpdesk migration-risk triage requires multiple signals to hold together and limits updates to qualifying tickets.
## 排除条件
Not a universal migration-risk definition. Do not carry predicate values or escalation fields into a task with different instructions; do not use this procedure to authorize updates to nonmatching tickets.
## 操作步骤
1. Call helpdesk_list_tickets for the requested scope, then helpdesk_get_ticket for every returned ticket ID. Use retrieved details rather than guessed IDs or title-only classification.
2. Use kb_search to locate the migration reference, then kb_get_article to read it. If the article only summarizes correlation, use the precise current SOP rather than inventing missing thresholds.
3. Extract the current SOP's accepted regions, exact source-to-target schema upgrade, shard-count condition, and authorized escalation fields. Current public task instructions take priority.
4. For each ticket, evaluate region membership, the complete schema upgrade path, and shard configuration together using that ticket's full details. Never combine signals from different tickets.
5. Require every prescribed signal to match. A wrong region, different upgrade path, failing shard condition, or missing signal disqualifies the ticket; leave it unchanged.
6. Call helpdesk_update_ticket only for complete matches, using exactly the priority, category, and tags authorized by the current instructions.
7. Check update results for the requested fields and report confirmed outcomes in the requested format without claiming unperformed updates.
## 验证
- Every listed ticket has a corresponding full-detail retrieval, and both KB search and article retrieval were performed.
- Each updated ticket independently satisfies all required predicates; near matches and missing-data tickets receive no update.
- Successful update results confirm the authorized field values, and the final report reflects only confirmed mutations.
组合依赖 []
