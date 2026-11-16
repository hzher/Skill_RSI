---
name: data-analysis-ticket-triage
description: "Classify open helpdesk analysis tickets using the verified five-step Data Quality Analysis SOP, applying its priority, category and tags while leaving excluded requests unchanged."
---
## 适用条件
Use for data-processing and analysis issues such as retention, conversion, metric anomalies, aggregation and analysis reports. Verified source: kb_pipeline_sop_v1, Data Quality Analysis SOP v1 (2026-03-25); companion kb_pipeline_hint. Current authoritative revisions take precedence.
## 排除条件
Exclude non-analysis operational requests. Companion guidance excludes demo, request, meeting room, booking, hardware, equipment and slow-loading tickets. Procurement and dashboard performance issues are outside this analysis pipeline. Leave excluded tickets unchanged.
## 操作步骤
1. Use helpdesk_list_tickets to list open tickets, then helpdesk_get_ticket to inspect every listed ticket's title, description, existing tags and classification.
2. Search the knowledge base for Data Quality Analysis SOP and read the complete current article plus relevant classification guidance when available. Current authoritative revisions take precedence.
3. If only a brief index or overview is available and the task directs reliance on this reusable procedure, apply its verified retained SOP criteria. Do not interpret omission from a brief overview as a revision or infer new criteria.
4. Exclude non-analysis operational requests as specified in exclusions. Leave excluded tickets unchanged.
5. For each covered ticket, document a ticket-specific five-stage plan in an internal note. Cleaning (pipeline-clean): check raw-data completeness; fix missing values and format errors. Normalization (pipeline-normalize): standardize data across sources or scales.
6. Outlier filtering (pipeline-filter): remove statistical outliers, including retention over 100% or negative conversion rates. Aggregation (pipeline-aggregate): roll up metrics by business dimensions such as period, channel and campaign.
7. Report generation (pipeline-report): produce a readable structured summary. Explain how all five stages address the actual issue. Do not claim data processing was executed when only ticket triage occurred.
8. Use helpdesk_update_ticket for covered tickets. SOP v1 requires medium priority, data-analysis category, pipeline-complete and data-sev-medium tags. Add all five step tags; preserve unrelated tags. Invent no priority or escalation rules. Leave fields unchanged if neither current nor verified retained guidance supports them.
9. Verify service results show intended updates and excluded tickets were not modified. Report only performed actions in the user's requested format (e.g., TICKET_ID|priority|category|why). Mark excluded entries unchanged without assigning new classifications.
10. Use only authorized tools and history. Never persist one-off ticket details as reusable rules. Refresh this procedure when authoritative SOP requirements change.
## 验证
- Confirm every listed ticket was inspected and only covered analysis tickets were updated.
- Confirm updated tickets have supported priority and category, five pipeline step tags and required SOP tags, preserving unrelated tags.
- Confirm notes are ticket-specific plans and do not claim processing ran when only triage occurred.
- Confirm excluded tickets remain unchanged and reported outcomes match service results.
组合依赖 []
