---
name: helpdesk-sop-classification
description: "Apply an active classification SOP to discovered helpdesk tickets using ticket details, KB references, and confirmed update results."
---
## 适用条件
Use for helpdesk batches requiring ticket inspection, KB consultation, and updates limited to an explicit SOP's scope.
## 排除条件
Does not authorize unrelated ticket changes or establish that a data pipeline ran merely because ticket tags were updated.
## 操作步骤
1. List tickets using the requested status or scope, then retrieve the details of every returned ticket. Use discovered identifiers rather than guessed IDs.
2. Search the KB for the requested procedure and retrieve the relevant article. Combine that reference with the explicit task SOP; a brief overview may omit required update fields.
3. Classify each ticket from its description. Distinguish analysis correctness or missing processing from requests merely mentioning analytics, such as demos or dashboard loading latency.
4. For matching tickets, derive every required stage tag, completion tag, severity tag, category, and priority from the active SOP, even when the reported defect concerns only one stage.
5. Update only tickets covered by the requested classification or escalation. Leave excluded and unrelated tickets unchanged.
6. Check each update response against the required metadata, then report confirmed updates in the requested output format with a concise issue-specific reason.
## 验证
- Every ticket returned by the list call has a corresponding detail read, and both KB search and article retrieval occurred.
- Each updated ticket satisfies the active SOP's classification and has every required metadata value in its returned state.
- Excluded tickets have no update calls; reported outcomes agree with confirmed responses and do not imply unobserved pipeline execution.
组合依赖 []
