---
name: scope-library-policy-ticket-review
description: "Review helpdesk tickets against the current library-use policy, consulting KB references, distinguishing active use from migration history, and limiting verified mutations to covered tickets and fields."
---
## 适用条件
Use when asked to classify or tag code-review tickets under a library deprecation or compliance SOP, especially when KB consultation and restricted mutation scope are required.
## 排除条件
Not a general incident-triage or source-code migration procedure. Do not classify unrelated operational tickets or infer new priorities, deadlines, or policy requirements from a brief KB overview.
## 操作步骤
1. Extract the current task's scope, library classifications, required tags and fields, exclusions, and output format. Treat explicit current corrections as authoritative over conflicting reference material.
2. Use kb_search to find the relevant policy reference, then kb_get_article to read it. If the article is only an overview, distinguish its actual content from details supplied by the current task.
3. List tickets and inspect their details to determine coverage; do not classify solely from titles or departmental ownership.
4. Identify the library currently used. Distinguish active deprecated-library calls from historical mentions in completed-migration descriptions; do not flag a migrated ticket merely for naming its old library.
5. Map each covered ticket to the current SOP's required tags and any explicitly mandated priority or category. Do not extend a deprecated-use rule's extra field changes to compliant tickets without authorization.
6. Update only covered tickets and authorized fields, preserving unrelated fields and tags. Leave unrelated or explicitly excluded tickets unchanged.
7. Read back updated tickets to verify persistence and check excluded records against their original state. Report confirmed results using the requested format, preserving unset fields rather than inventing values.
## 验证
- Required KB search and article retrieval are complete; policy conclusions are traceable to the current instructions or actual reference content.
- Current library use, not a historical mention, determines classification; non-code-review tickets remain outside the mutation set.
- Persisted tags and mandated fields match the applicable rule; fields without an authorized change and excluded tickets retain their prior state.
- The final report reflects verified ticket state and the requested output structure.
组合依赖 []
