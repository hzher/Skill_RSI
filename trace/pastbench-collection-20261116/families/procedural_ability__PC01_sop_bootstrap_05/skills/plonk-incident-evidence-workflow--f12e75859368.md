---
name: plonk-incident-evidence-workflow
description: "Find incident evidence, pin its source, publish a substantive Plonk status, and verify the result."
---
## 适用条件
Use when authorized to handle an incident with Plonk actions nexobi, felura, and zubefa.
## 排除条件
Does not define remediation, severity, escalation, or the unavailable full incident SOP. Never reuse example incident facts or IDs.
## 操作步骤
1. Consult the available SOP. Distinguish broad index guidance from API behavior demonstrated by tool responses.
2. Call plonk_action with action nexobi. In jurova entries, inspect tikano (ID), xovik (channel), belatu (body), kronex (time), and grevani (pinned state).
3. Select the source alert by content, channel, and time; review related updates. Inspect each returned xovik rather than assuming a requested channel filter worked.
4. Pin with action felura, pikano set to the source tikano, and xovik set to its channel. Check the returned target and grevani true.
5. Publish with action zubefa, the designated incident channel in xovik, and a nonempty status in belatu. Ground impact and mitigation in source messages and identify the source alert.
6. Read with nexobi again. Verify the source is pinned and the posted status has the intended channel and nonempty body. Distinguish the source ID from the posted status tikano_ref.
## 验证
- Never test posting by omitting content: zubefa with only a channel created an empty message despite returning success.
- Inspect returned channels: nexobi with a channel argument returned messages from other channels.
- Confirm the pinned source and substantive status by read-back before reporting completion in the user-requested format.
组合依赖 []
