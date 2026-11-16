---
name: triage-export-tickets-with-unverified-approval
description: "Classify and update data-export request tickets when applicable policy references approval information that cannot be verified through available sources."
---
## 适用条件
Use when processing helpdesk data-export requests and the available policy points to a separate exception or approval source that has not been obtained.
## 排除条件
Does not authorize or perform exports, establish exception eligibility, or define universal priority and tag values. Follow current task instructions and any retrieved authoritative policy instead of assuming this fallback always applies.
## 操作步骤
1. Use helpdesk_list_tickets to identify tickets in the requested scope, then helpdesk_get_ticket for each candidate to inspect the full request and current fields.
2. Identify the requested data, requesting team, format or tool, and any urgency stated in the ticket; distinguish request details from verified policy authorization.
3. Retrieve the relevant policy article. If it refers to a separate registry for approved teams, tools, or procedures, do not treat the overview as proof of an exception.
4. Search for the referenced source by title and relevant terms; when instructed, open any saved exception-registry skill in full. Distinguish procedural guidance from actual approval criteria. If only the overview or procedures are available, record approval as unverified, not the registry or exception as nonexistent.
5. Choose category and tags consistent with the request and available guidance. Where approval remains unresolved, mark the need for policy review rather than implying authorization.
6. Apply supported priority rules; absent a supported reason to change priority, retain the existing value rather than inferring urgency from the export request alone.
7. Use helpdesk_update_ticket for each in-scope ticket. Document the policy consulted, the unresolved approval source, and the need for review; do not claim an export was performed when only triage occurred.
8. Check each update response for success and the returned fields. Report confirmed ticket values in the user-requested format, without presenting attempted or failed updates as completed.
## 验证
- Each in-scope ticket has a full-detail retrieval and a successful update response before being reported as updated.
- Unverified exception status remains explicit; unsuccessful searches are not represented as proof of ineligibility or nonexistence.
- Reported priority, category, and tags match the confirmed update response and comply with the current output instructions.
- Any saved registry skill that the task requires inspecting has been opened in full; its title or procedural guidance alone is not treated as approval criteria.
组合依赖 []
