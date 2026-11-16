---
name: recover-documented-decisions
description: "Recover a prior decision from complete source notes, distinguish requests from decisions, and cite only evidence that supports the requested scope. Bound uncertainty when the available lookup does not recover the historical record."
---
## 适用条件
Use when asked to follow an already documented decision and cite its note rather than ask stakeholders to repeat it.
## 排除条件
Does not authorize a new business decision, note edits, or treating an escalation request as approval. Current-note lookup alone cannot establish exhaustive historical coverage; no historical-search action schema is demonstrated here.
## 操作步骤
1. Identify the subject, requested decision scope, and required output fields from the current user instruction; do not substitute a new recommendation for the requested historical decision.
2. Use notes_list to enumerate available note_id values. Read candidate notes with notes_get using the exact note_id; inspect complete content rather than deciding from titles or list metadata.
3. Classify each retrieved note as an explicit decision, a request for a decision, or unrelated context. A request to rely on a prior decision does not itself supply that decision.
4. Match any recovered decision to the subject and deployment scope. Preserve stated conditions and exceptions; do not infer decision timing or continued validity solely from note metadata or tool-call timestamps.
5. If current notes lack the decision, distinguish that result from absence of a historical decision. Use a historical lookup only when its actual interface is available; this evidence supplies no such action schema.
6. Keep original tool-return facts separate from assistant interpretation. Do not assert a historical-search service is unavailable based only on a prior assistant's claim or lack of a recorded call.
7. Return the supported advice and source note_id in the user's required format. If unresolved, state the lookup limitation without inventing advice or a decision citation; do not present a request-only note as the decision source.
## 验证
- Every decision citation resolves to retrieved content explicitly supporting the subject, scope, and advice; request-only and unrelated notes are excluded as decision evidence.
- Any unresolved result describes the actual search coverage, without claiming exhaustive history or unsupported service unavailability.
- The response follows the current output contract, preserves source qualifications, and makes no changes to notes.
组合依赖 []
