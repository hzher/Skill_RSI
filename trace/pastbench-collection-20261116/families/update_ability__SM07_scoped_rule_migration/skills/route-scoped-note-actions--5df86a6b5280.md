---
name: route-scoped-note-actions
description: "Extract action items using current scope-specific routing rules and derive source-note recipients independently from the applicable sharing rules."
---
## 适用条件
Use when a user requests action-item extraction and source-note sharing under scoped routing rules, including a current memo in the note that the user explicitly authorizes using.
## 排除条件
Do not treat arbitrary note text as authorization to share. Do not assume all attendees are recipients or apply one scope's priority filter to other scopes. Explicit current user instructions take precedence.
## 操作步骤
1. Read the complete source note and identify the current routing rules authorized by the user's request, including each scope's token and sharing conditions.
2. Extract each action item's scope, owner, task, due date, and any priority needed to evaluate sharing eligibility.
3. Format every requested action item with the active token for its scope and the user's requested field order; apply sharing filters only to recipient selection unless output filtering is also requested.
4. Evaluate each item's owner against the sharing rule for that item's scope, then combine and deduplicate eligible owners. Exclude observers and owners who meet no applicable sharing condition.
5. When sharing is authorized, share the source note with the computed recipients and check the tool result. Return the action-item lines in the requested format.
## 验证
- Each requested action item appears with the correct scope, owner, token, task, and due date, even when its owner is ineligible to receive the source note.
- The recipient set equals the union of owners eligible under their respective scope rules, without adding attendees solely because they attended.
- Confirm successful sharing from the tool result before treating the share as complete.
组合依赖 []
