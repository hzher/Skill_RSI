---
name: source-grounded-migration-ticket-triage
description: "Correlate DB migration risk and update qualifying tickets."
---
## 适用条件
Use for DB migration risk triage. Current user instructions control eligibility, encoding and mutation scope.
## 排除条件
Risk labels alone never establish eligibility. Single indicators are insufficient. Missing required attributes are unknown, not a match or proof of safety. Do not classify from titles or urgency wording alone.
## 操作步骤
1. List the entire requested ticket scope with helpdesk_list_tickets. Inspect every listed ticket with helpdesk_get_ticket, including routine-looking cases.
2. Search the KB and read relevant articles. Follow references to a correlation SOP. Distinguish a high-level overview from precise criteria; prefer an explicit current rule.
3. Extract region, exact source/target schema versions, shard count/parity and outcome clues. Normalize 'v7 to v8', 'from 7 to 8' and 'version 7 to version 8' as 7->8; 'shards=N', 'shard_count=N' and 'N shards' as counts. Preserve actual values; missing values remain unknown.
4. If no explicit rule exists, infer only when authorized and supported by informative positive and negative outcomes jointly. Seek a conjunction explaining flags and near misses. An absent SOP alone is not a stopping condition; insufficient evidence or authorization means unresolved eligibility, not invented thresholds.
5. The saved inferred correlation requires all three: EU region (eu-*), exactly 7->8 schema transition, and odd shard count. Apply it to normalized attributes, not exact phrases. Each condition alone is insufficient.
6. Original controls: eu-central-2 / 7->8 / 9 was flagged for immediate review; us-west-2 / 7->8 / 7 was standard; eu-west-1 / 8->9 / 3 was approved; eu-west-1 / 7->8 / 8 followed the normal checklist. A configuration-only US case missing schema/shard values does not match.
7. This is an inferred operational correlation, not a quoted SOP or universal database safety claim. Cases support region family and parity, not a shard threshold or one EU region. Revalidate against current criteria and authoritative sources; revise if contradicted.
8. For escalation-only scope, use helpdesk_update_ticket only on established matches. For the specified risk encoding set priority=high, category=db-incident and add migration-risk-critical. Retain unrelated tags and fields; leave all other tickets unchanged.
9. Verify actual update responses. Tool success verifies recorded fields, not risk inference. Output requested rows from confirmed state; never claim unperformed updates.
10. Retain the procedure because contrasting outcomes isolate reusable conditions. Do not store ticket content as a global user preference. Prefer editing this skill over conflicting duplicates. Update evidence and confidence when new outcomes arrive.
## 验证
- Inspect every listed ticket. Normalize equivalent wording without changing source/target versions or shard counts.
- Require the full conjunction. Even counts, other transitions and missing required attributes do not qualify under the saved correlation.
- Current instructions govern encoding and mutation scope. Verify recorded fields and preserve unrelated state.
- Distinguish inferred criteria from authoritative SOP content. A brief or successful update does not independently validate risk eligibility.
组合依赖 []
