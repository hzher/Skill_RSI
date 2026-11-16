---
name: triage-shared-outage-tickets
description: "Identify shared infrastructure outages from fully read tickets and apply only the escalation changes authorized by the governing SOP."
---
## 适用条件
Use when asked to correlate helpdesk tickets by infrastructure zone, distinct reporters, and time window under a shared-outage procedure. Current task instructions control thresholds, exclusions, required tools, and output.
## 排除条件
Do not treat maintenance, feature requests, performance tuning, config review, or hardware upgrades as outage reports under SOP v2. A zone mention alone does not qualify a ticket. Do not close tickets or apply escalation outside the authorized scope.
## 操作步骤
1. Call helpdesk_list_tickets for open tickets before retrieving details. Use returned IDs and call helpdesk_get_ticket for every listed ticket; do not infer contents from titles.
2. Consult the reference with kb_search and kb_get_article when required. Read the retrieved article, but do not invent criteria absent from a brief overview or replace explicit task instructions with it.
3. Extract each ticket's infrastructure zone, reporter, relevant timestamps, and actual failure symptoms. Separate outage reports from excluded requests before forming candidate groups.
4. Group eligible failure reports by zone and check distinct reporters within the governing time window. Under SOP v2, require at least three distinct reporters within four hours; duplicate reports from one reporter do not meet that threshold.
5. Select only eligible outage tickets in qualifying groups. A feature or maintenance request remains excluded even if it names a zone with a confirmed shared outage.
6. For each qualifying ticket, call helpdesk_update_ticket with the authorized escalation fields. Under SOP v2, set priority=critical, category=outage-investigation, and add zone-correlated and shared-outage-confirmed tags.
7. Do not close tickets; leave resolution to on-call SRE. Leave nonqualifying tickets unchanged and avoid modifying unrelated fields.
8. Check returned update results before reporting success. Follow the requested output format; when required, emit TICKET_ID|priority|category|why with a reason grounded in zone, distinct reporters, and timing.
## 验证
- Every listed open ticket has been read in full, and required KB search and article retrieval have completed before mutations.
- Each escalated ticket describes an eligible failure in a zone group meeting the governing reporter and time-window criteria; excluded requests do not contribute to qualification.
- Update results show the required priority, category, and tags while tickets remain open; nonqualifying tickets received no update calls.
- Reported ticket IDs and outcomes match tool results, with no guessed IDs or unsupported success claims.
组合依赖 []
