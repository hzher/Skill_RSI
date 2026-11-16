---
name: shared-infrastructure-ticket-triage
description: "Triage helpdesk tickets using service impact and shared failure evidence, then verify targeted updates."
---
## 适用条件
Use when reviewing open helpdesk tickets for incidents, urgent issues, or a possible shared infrastructure failure.
## 排除条件
Does not authorize closing tickets, performing infrastructure repairs, or treating inferred relationships as confirmed causes. Local incident policy governs required fields and severity labels.
## 操作步骤
1. List tickets in the requested scope, then retrieve each ticket's full details before classifying or updating it.
2. Extract affected services, infrastructure identifiers, regions, timing, symptoms, reported impact, and explicit cause evidence. Compare these details across tickets.
3. Correlate reports when their failure evidence supports a shared incident. A shared product or cluster name alone does not establish an outage relationship or cause.
4. Assign priority and category from documented impact and available policy. Distinguish delivery failures and blocked critical work from routine training or maintenance with no reported user impact.
5. When consulting a procedure index, retrieve the referenced procedure. If unavailable, do not invent its escalation criteria, required tags, or other mandatory rules.
6. Apply needed ticket updates with concise evidence-based notes, related ticket references, and supported tags. Preserve existing information and obey any instruction to leave tickets open.
7. Inspect update responses for the requested fields and status; retrieve the ticket again if confirmation is incomplete. Report ticket ID, priority, category, and a brief reason in the requested format.
## 验证
- Each classification is supported by full ticket details rather than the title alone.
- Shared incident links cite failure or cause evidence; routine tickets are not escalated solely because they mention the same infrastructure.
- Updates are confirmed, prohibited status changes have not occurred, and the final report agrees with the confirmed ticket fields.
组合依赖 []
