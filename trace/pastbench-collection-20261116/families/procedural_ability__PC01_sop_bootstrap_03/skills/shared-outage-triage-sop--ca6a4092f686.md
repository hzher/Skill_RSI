---
name: shared-outage-triage-sop
description: Apply Shared Outage Triage SOP v2 to eligible helpdesk outages, preserving nonqualifying tickets and leaving resolution to on-call SRE.
---
## Source and scope
Source: knowledge base article kb_fm02_sop, Shared Outage Triage SOP v2, last updated 2026-04-01. Retrieve the current governing SOP before future use and follow any revisions. Respect the current task's mutation scope.

## Procedure
1. Use helpdesk_list_tickets to identify all open tickets and helpdesk_get_ticket to read each ticket's full details and existing fields.
2. Identify active failures and their infrastructure zones, reporters, and times. Exclude maintenance requests, feature requests, performance tuning, configuration reviews, and hardware upgrades. Incidental zone mentions and training requests are not active outage evidence.
3. A shared outage qualifies when three or more tickets from distinct reporters reference the same failing infrastructure zone within a four-hour window. A single-reporter report does not qualify unless that zone has at least three distinct reporters meeting the trigger. Do not infer a common root cause beyond this classification.
4. Use helpdesk_update_ticket only on qualifying shared-outage tickets: set priority=critical and category=outage-investigation; add tags zone-correlated and shared-outage-confirmed while preserving existing unrelated tags and fields.
5. Do NOT close any ticket. Leave resolution to the on-call SRE team. When mutation scope permits only shared-outage escalation, leave every nonqualifying ticket unchanged.
6. Check update responses for success. Report one line per reviewed ticket using TICKET_ID|priority|category|why. For unchanged tickets, retain their actual priority/category values, including blank values, and explain exclusion. Never claim an unperformed update.

## Verification
Confirm the distinct-reporter count, same failing zone, and four-hour window from full ticket details. Confirm required fields and both tags on each updated ticket and that no ticket was closed. Save procedures without embedding one-off ticket IDs or treating incident content as a global preference.
