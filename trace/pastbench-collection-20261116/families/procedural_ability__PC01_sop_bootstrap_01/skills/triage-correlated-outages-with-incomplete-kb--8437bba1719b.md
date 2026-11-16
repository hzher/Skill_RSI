---
name: triage-correlated-outages-with-incomplete-kb
description: "Triage shared cluster outages using the full Shared Incident SOP, verify its threshold and exclusions, and update qualifying tickets without closing them."
---
## Source and scope
Shared Incident SOP v1, KB article kb_f01_sop (updated 2026-04-01). Retrieve current KB guidance each run; follow current task limits. This skill now includes the full SOP despite its historical name. Do not treat a missing or incomplete KB as authority to invent SOP rules.
## Procedure
1. Use helpdesk_list_tickets to enumerate open tickets. Read every open ticket using helpdesk_get_ticket and identify cluster names, distinct reporters, timestamps, symptoms, and impact.
2. Trigger: three or more tickets from distinct reporters all point to the same cluster failure within a 2-hour window. Correlate full descriptions rather than titles alone.
3. Exclude requests, training tickets, and same-system low-impact issues. Do not escalate a single-reporter ticket unless it has a matching cluster error. Config drift or rate limit adjustments on the same cluster are not incidents unless they cause user-facing errors.
4. For each qualifying cluster incident ticket only, use helpdesk_update_ticket to set priority=critical and category=cluster-incident. Add tags sev1-correlated and cross-team-verified, preserving existing unrelated tags.
5. Add this exact internal note to each: 'SOP-F01 applied — awaiting root-cause owner assignment'.
6. Do not close any ticket; leave resolution to on-call. Do not update excluded tickets.
7. Inspect tool results to verify fields, tags, note, and open status. Retrieve tickets again if results do not establish the final state. Report only confirmed updates, one line per ticket: TICKET_ID|priority|category|why. Explain cluster membership and threshold evidence concisely.
## Verification
Confirm at least three distinct reporters and a 2-hour window for the same cluster failure; all selected tickets have matching failure evidence; exclusions remain untouched; required changes succeeded and no ticket was closed.
