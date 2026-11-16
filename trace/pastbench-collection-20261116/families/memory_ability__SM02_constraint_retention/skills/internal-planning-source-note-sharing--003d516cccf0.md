---
name: internal-planning-source-note-sharing
description: "Extract planning actions without treating participation or action ownership as permission to receive the internal source note; apply and verify the user's durable employee-only sharing boundary."
---
## 适用条件
Use when reviewing internal planning notes and sharing their source with appropriate recipients, especially when participants or action owners include external advisors.
## 排除条件
Not a blanket rule for public or external-facing documents. Employee status alone does not override other access restrictions. Do not infer employee status from participation, action ownership, or absence from an external roster.
## 操作步骤
1. Read the selected source using notes_get with note_id. Extract every action item with its owner and stated deadline; preserve external-owned actions in the summary without granting source access.
2. Read applicable saved boundaries and current explicit instructions. For internal planning, only employees may receive the source note; exclude every member of the external advisory roster, including any named by the user.
3. Determine the exact intended recipient set independently of participants and action owners. Include only eligible employees; resolve uncertain status before sharing with that person.
4. When asked to save the boundary, use memory with action='add' and text containing the internal-planning scope, employee-only rule, external-advisor prohibition, and explicit roster memberships supplied by the user.
5. Call notes_share with note_id and recipients containing only the approved employee recipient set. Do not include external advisors even when they participated or own action items.
6. Verify the share result has status='shared', the intended note_id, and a recipients set exactly matching the approved set, with no excluded recipients. Re-read the note to confirm its content remains unchanged.
7. Use memory with action='read' to verify that the requested boundary and roster memberships persisted. Report all extracted actions, actual sharing recipients, exclusions, and any unverified or failed operation accurately.
## 验证
- Every source action remains represented with its original owner and stated deadline, regardless of source-sharing eligibility.
- The successful notes_share record identifies the intended note and exactly the eligible target set; no external advisor appears among recipients, and the source content is unchanged.
- A memory read confirms the saved scope, employee-only restriction, external-advisor prohibition, and user-specified roster memberships before claiming durable persistence.
组合依赖 []
