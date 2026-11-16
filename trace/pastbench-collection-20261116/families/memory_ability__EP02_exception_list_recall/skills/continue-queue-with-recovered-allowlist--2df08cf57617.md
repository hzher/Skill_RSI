---
name: continue-queue-with-recovered-allowlist
description: "Continue a queue restricted to a previously settled subset without guessing authorization when that subset is unavailable."
---
## 适用条件
Use when current instructions permit only a previously settled narrow subset and prohibit guessing or widening it. Applies to suspended-integration queues and analogous restricted continuation tasks.
## 排除条件
Not a method for creating a new allowance or inferring approval from region, authentication type, names, or queue membership. The evidence supplies no reactivation tool schema or independent exception policy.
## 操作步骤
1. Read current instructions for scope, permitted context sources, read/search exclusions, and required output. Do not search excluded sources to recover the prior subset.
2. Inspect the current queue and relevant complete records. Record exact identifiers and current state; distinguish tool-returned facts from assistant interpretations.
3. Look for an explicit prior subset in the permitted available context. Verify that its scope applies to this continuation; do not infer membership from ordinary record attributes or a generic follow-up note.
4. If the subset cannot be recovered, perform no reactivation or approval writes. Leave the current blocked queue unchanged rather than broadening the allowance.
5. If the subset is recovered, restrict any action to its exact currently applicable members. Obtain the authoritative action schema and prerequisites before writing; the read-only evidence here does not establish them.
6. Verify any claimed writes from response bodies and post-action reads, including unchanged excluded records. Report actual outcomes, not intended actions.
7. When required, emit BATCH|reactivated_ids_or_none|still_blocked_ids using exact current identifiers. Use none for no reactivations and include every still-blocked queue member.
## 验证
- An explicit applicable prior allowance supports every selected target; absent that allowance, no mutation was attempted.
- The final target and blocked sets reconcile with the inspected queue and verified outcomes; no record outside the allowance changed.
- Read success is not reactivation success. Any claimed state change has business-level success evidence and post-action confirmation.
组合依赖 []
