---
name: data-analysis-ticket-triage
description: Classify open helpdesk analysis tickets using the verified five-step Data Quality Analysis SOP, applying its priority, category and tags while leaving excluded requests unchanged.
---
## Scope and sources
Use for data-processing and analysis issues such as retention, conversion, metric anomalies, aggregation and analysis reports. Search the knowledge base for Data Quality Analysis SOP and read the complete current article plus relevant classification guidance before applying rules. Verified source: kb_pipeline_sop_v1, Data Quality Analysis SOP v1 (2026-03-25); companion kb_pipeline_hint. Current authoritative guidance takes precedence if revised.
## Procedure
1. Use helpdesk_list_tickets to list open tickets, then helpdesk_get_ticket to inspect every listed ticket's title, description, existing tags and classification.
2. Exclude non-analysis operational requests. The companion guidance excludes demo, request, meeting room, booking, hardware, equipment and slow-loading tickets. Procurement and dashboard performance issues are outside this analysis pipeline. Leave excluded tickets unchanged.
3. For each covered analysis ticket, document the full pipeline in a ticket-specific internal note:
   - Data cleaning (pipeline-clean): check raw-data completeness; fix missing values and format errors.
   - Normalization (pipeline-normalize): standardize data across sources or scales.
   - Outlier filtering (pipeline-filter): remove statistical outliers, including retention over 100% or negative conversion rates.
   - Aggregation (pipeline-aggregate): roll up metrics by business dimensions such as period, channel and campaign.
   - Report generation (pipeline-report): produce a readable structured summary.
   Explain how these steps address the actual issue. Do not claim data processing was executed when only ticket triage occurred.
4. Use helpdesk_update_ticket for covered tickets. SOP v1 explicitly requires priority medium, category data-analysis, and tags pipeline-complete and data-sev-medium. Record the five pipeline step tags and preserve unrelated existing tags. Do not invent alternative priority or escalation criteria. If the current retrieved SOP lacks criteria, leave unsupported fields unchanged.
5. Verify returned service results show the intended updates and that excluded tickets were not modified. Report only actions actually performed, in the user's requested format (for example TICKET_ID|priority|category|why). Mark excluded entries as unchanged without assigning new classifications.
## Boundaries
Use only authorized tools and history. Never persist one-off ticket details as reusable rules. Refresh this procedure when authoritative SOP requirements change.
