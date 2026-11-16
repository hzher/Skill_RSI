---
name: correlate-helpdesk-cluster-incidents
description: "Triage open helpdesk tickets against the current shared-incident SOP using full ticket details, KB consultation, cross-reporter correlation, and verified selective updates."
---
## 适用条件
Use when asked to identify and update tickets belonging to a shared cluster failure under an explicit incident SOP. Current task instructions govern thresholds, required fields, notes, and output format.
## 排除条件
Do not classify requests, training, low-impact same-system issues, or non-impacting configuration/rate-limit changes as incidents merely because they mention the same system. Do not escalate an isolated report without matching cluster-error evidence.
## 操作步骤
1. List open tickets with the helpdesk tool, then retrieve every listed ticket's full details. Use returned identifiers rather than guessed IDs or titles alone.
2. Search the KB and retrieve relevant articles when required. Distinguish an index or brief from the full procedure and follow its reference if available.
3. If a referenced article is unavailable, do not invent its rules or claim to have read it. Apply the explicit current task SOP when sufficient; seek clarification if necessary decision rules are missing.
4. Group tickets by cluster and matching user-facing failure symptoms. Compare reporters and timestamps against the current SOP's distinct-reporter threshold and time window.
5. Evaluate each ticket separately for exclusions. Shared system or cluster names alone do not establish a shared incident; routine requests remain unchanged.
6. For each qualifying ticket, call the update tool with the SOP's required priority, category, tags, and exact internal note. Do not close tickets when resolution belongs to on-call.
7. Inspect update results for success and required field values, including the ticket remaining open. Do not report an attempted or failed mutation as completed.
8. Return the requested per-ticket output format, using confirmed field values and concise correlation or exclusion reasons. Represent unset fields accurately rather than inventing defaults.
## 验证
- Every open ticket returned by discovery has a corresponding detail read before classification.
- The incident group meets the current SOP's reporter and timing criteria and shares a supported cluster failure; excluded requests are not updated.
- Required KB consultation is evidenced, and unavailable referenced content is not treated as retrieved guidance.
- Successful update responses confirm required fields and open status; final output agrees with these responses and clearly identifies unchanged tickets.
组合依赖 []
