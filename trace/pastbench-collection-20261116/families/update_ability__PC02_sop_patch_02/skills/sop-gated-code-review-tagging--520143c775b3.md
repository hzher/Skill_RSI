---
name: sop-gated-code-review-tagging
description: "Review helpdesk code-review tickets against retrieved standards, separating ticket inspection from authorized tag changes and handling missing classification rules without inventing them."
---
## 适用条件
Use when asked to classify or tag code-review tickets under an HTTP-client or similar engineering SOP, especially when search results provide only an overview or a reference to another guide.
## 排除条件
Does not define approved or deprecated libraries, priority levels, category names, or escalation rules. Does not authorize changes to unrelated incidents or override the current task's mutation scope.
## 操作步骤
1. Read the current task's scope and output requirements. If it requires a saved standard skill, retrieve and read that skill rather than relying on a listing or memory summary.
2. List tickets and inspect their details, including existing tags and priority. Distinguish code-review cases from unrelated incidents without changing either during inspection.
3. Retrieve the applicable SOP content and follow relevant references. A search snippet or an overview saying that deprecated libraries exist is not a library-to-classification mapping.
4. Check that the retrieved source actually defines the classification or escalation for each candidate. Do not infer local policy from library names, migration wording, or general programming knowledge.
5. If the required mapping cannot be retrieved, leave affected tickets unchanged and report the specific source gap. A failed lookup establishes that retrieval failed, not that no standard exists anywhere.
6. When authoritative rules are available, change only covered tickets and preserve unrelated fields and excluded tickets. Verify any mutations against the requested rules and scope.
7. Report each ticket in the current task's required format, distinguishing confirmed classifications from unchanged tickets and giving the applicable rule or blocking source gap.
## 验证
- Every assigned priority, category, or tag is traceable to retrieved authoritative rules rather than an overview or inferred library status.
- Unrelated and excluded tickets remain unchanged even though they were inspected.
- If required rules remain unavailable, no unsupported mutations are made and the report identifies the retrieval limitation without claiming successful classification.
组合依赖 []
