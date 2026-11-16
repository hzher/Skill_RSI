---
name: source-grounded-migration-ticket-triage
description: Inspect all migration tickets, correlate region, schema transition and shard parity using authoritative criteria or authorized outcome-based learning, and update only qualifying escalations.
---
## Scope
Use for DB migration risk triage. Current user instructions control eligibility, encoding and mutation scope. Required risk labels alone never establish eligibility.
## Procedure
1. List the entire requested ticket scope with helpdesk_list_tickets and inspect every listed ticket with helpdesk_get_ticket, including routine-looking cases.
2. Search the KB and read relevant articles. Follow references to a correlation SOP. Distinguish a high-level overview from precise criteria.
3. Extract region, exact source/target schema versions, shard count/parity, and outcome clues from ticket details. Missing values remain unknown. Do not classify from titles or urgency wording alone.
4. Prefer an explicit current rule. If none is available and the task authorizes inference from informative cases, compare positive and negative outcomes jointly: seek a conjunction that explains the flagged case and the near-miss controls. Do not stop solely because the SOP is absent. Without sufficient outcome evidence or authorization, report unresolved eligibility rather than inventing thresholds.
## Case-derived correlation
An informative batch supports escalation when all three hold: region is EU (eu-*), schema transition is exactly 7->8, and shard count is odd.
Evidence: eu-central-2 / 7->8 / 9 was flagged for immediate review; us-west-2 / 7->8 / 7 was standard; eu-west-1 / 8->9 / 3 was approved; eu-west-1 / 7->8 / 8 followed the normal checklist. Thus EU alone, the transition alone, and odd shard count alone are insufficient. A configuration-only US task with absent schema/shard values supplies no migration-risk match.
This is an inferred reusable operational correlation, not a quoted SOP or a universal database safety claim. The cases support region family and parity, not an exact shard threshold or a single specific EU region. Revalidate against new task criteria and authoritative sources; revise if contradicted. Missing required attributes do not establish a match and are not proof of safety.
## Mutation and verification
5. Update only established qualifying tickets when scope is escalation-only. Use helpdesk_update_ticket. For the specified risk encoding set priority=high, category=db-incident and add migration-risk-critical while retaining unrelated tags and fields. Leave other tickets unchanged.
6. Verify actual update responses. Tool success verifies recorded fields, not the risk inference. Output requested rows from confirmed state; never claim unperformed updates.
## Durability
Retain this task-specific procedure because contrasting outcomes isolate three reusable conditions. Do not store individual ticket content as a global user preference. Prefer editing this skill to creating conflicting duplicates. Update evidence and confidence when new outcomes arrive.
