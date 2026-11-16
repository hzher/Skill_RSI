---
name: triage-correlated-outages-with-incomplete-kb
description: "Correlate open helpdesk tickets into an outage group, distinguish unrelated requests, and apply documented triage updates without claiming unavailable SOP authority or closing tickets."
---
## 适用条件
Use when asked to triage and update open tickets for a shared outage, especially when a KB search yields only an index or brief rather than the full procedure. Follow the current task's limits and output requirements.
## 排除条件
Does not reconstruct missing SOP rules, prescribe universal priority labels or tags, authorize escalation, resolve the outage, fulfill underlying service requests, or authorize ticket closure.
## 操作步骤
1. Search the KB for the triage procedure and retrieve the relevant article. If it only references a full SOP, attempt to retrieve that SOP; explicitly distinguish a missing procedure from usable guidance.
2. Use helpdesk_list_tickets to enumerate open tickets and helpdesk_get_ticket to inspect each candidate's full description before classifying it.
3. Compare affected service or cluster, region, symptoms or endpoint errors, onset time, and reported impact. Group tickets with converging evidence rather than relying on similar titles alone.
4. Keep unrelated requests separate when their descriptions show a different cause or routine maintenance need. Do not infer outage membership merely because both involve authentication.
5. Choose supported priority and category values from available guidance and reported impact. When the SOP is unavailable, document that classification is impact-based; do not present inferred labels or tags as mandatory SOP rules.
6. Use helpdesk_update_ticket for the requested triage changes. Record related ticket references, correlation evidence, impact rationale, and the KB limitation in notes; apply consistent outage labels where appropriate. Preserve open status when closure is prohibited.
7. Inspect update results for the intended fields and unchanged open status. Report only confirmed updates in the requested format, with a concise evidence-based reason for each ticket.
## 验证
- The outage group is supported by multiple matching details in full ticket descriptions, and unrelated requests remain outside it.
- Classification notes distinguish available KB guidance from judgment based on impact; no unavailable SOP requirements are asserted.
- Tool results confirm requested priority/category changes and show tickets remain open; triage does not perform unrelated remediation.
组合依赖 []
