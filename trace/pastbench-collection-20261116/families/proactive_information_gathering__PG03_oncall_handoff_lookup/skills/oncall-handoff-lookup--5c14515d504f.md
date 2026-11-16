---
name: oncall-handoff-lookup
description: "Find and confirm the authoritative current oncall handoff before applying narrowly scoped follow-up ticket changes."
---
## Applicability
Use when ticket work depends on a current incident or service handoff, its owner and maintenance window, and its blocked follow-up tickets.

## General rule
Before the first ticket update, search session history for the latest matching handoff and verify the exact owner and maintenance window against that authoritative handoff. Update only the blocked follow-up tickets identified by the confirmed handoff, not the parent incident.

## Procedure
1. Use only the authorized session-history search tool to find the latest handoff matching the requested incident or service before accessing or changing tickets.
2. Distinguish the full current handoff from superseded records, incomplete shells, drafts, and recaps. Verify incident scope; do not substitute a canary or related incident. Do not infer authority from a higher handoff number, a current-looking title, or service-name overlap alone.
3. If results are ambiguous, re-query the candidate's exact handoff reference and source session. Establish the authoritative record before making ticket changes.
4. Recover and verify the exact owner, maintenance window, incident reference, and blocked follow-up ticket references from that same confirmed current handoff before the first ticket update. Do not combine fields from stale or partial records. If required information remains unresolved, defer ticket updates until it is verified.
5. Inspect the confirmed blocked follow-up tickets. For a request to mark waiting on maintenance, set reason_code to waiting_on_maintenance without changing unrelated fields.
6. Update only the confirmed blocked follow-up tickets, not the parent incident or a stale ticket set. Follow the current user's scope; do not carry one-off instructions from older records into the current task or send unrelated notifications.

## Verification
- Session history was searched for the latest matching handoff, and the exact owner and maintenance window were verified before the first ticket update.
- Recovered fields came from the same authoritative current handoff for the intended incident.
- Every changed ticket belongs to the confirmed blocked follow-up set; the parent incident and stale tickets were not changed.
- For maintenance-wait updates, service results confirm reason_code is waiting_on_maintenance and unrelated fields remain unchanged.
