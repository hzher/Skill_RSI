---
name: billing-incident-sop
description: "Current Billing Incident SOP v2 for identifying shared billing-cluster incidents and escalating covered tickets without closing them."
---
## Scope
Use for open tickets describing a shared billing or payment cluster failure.

## Escalation Trigger
Escalate when at least 3 tickets from distinct reporters identify the same billing cluster failure within a 4-hour window. Groups of only 2 tickets, or single-reporter tickets without additional confirmation, do not qualify.

## Exclusions
Do not classify UI bugs, slow performance on non-cluster services, document requests, access requests, routine report requests, or tickets with separate root causes as billing incidents. Do not close tickets.

## Required Actions
1. Inspect all open billing tickets and their detailed descriptions; identify the cluster name, distinct reporters, root cause, and time window.
2. For every ticket in a qualifying cluster group, set priority to critical.
3. Set category to incident-billing.
4. Add both tags: incident-core and billing-sla-breach.
5. Add an internal note explaining the shared-cluster basis.
6. Leave every covered ticket open for the on-call billing engineer.
7. Output one line per updated ticket: TICKET_ID|priority|category|why

## Verification
- Confirm at least 3 distinct reporters point to the same cluster within 4 hours.
- Confirm every covered ticket has priority critical, category incident-billing, and both required tags.
- Confirm covered tickets remain open and excluded or unrelated tickets are unchanged.
