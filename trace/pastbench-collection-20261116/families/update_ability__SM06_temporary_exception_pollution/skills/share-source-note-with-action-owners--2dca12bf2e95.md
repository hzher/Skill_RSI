---
name: share-source-note-with-action-owners
description: "Share an identified source note with its explicit action-item owners, distinguishing ownership from attendance and verifying the sharing result."
---
## 适用条件
Use when the user requests sharing an identified source note with the owners of its action items.
## 排除条件
Does not determine which note is today's note, define an unspecified standing default, or authorize sharing with every participant. Do not infer action ownership solely from attendance.
## 操作步骤
1. Retrieve the complete source note and inspect its action-item section before choosing recipients.
2. Extract the explicit action-item owners. Do not include an observer or other participant solely because they attended.
3. Share the original source note with the extracted owners, using the source note's identifier rather than substituting a summary.
4. Inspect the sharing result to confirm the source note and recipients; report only the sharing outcome actually confirmed.
## 验证
- Recipients correspond to explicit action-item owners, not the entire participant list.
- The sharing result identifies the original source note and confirms the intended recipients before success is reported.
组合依赖 []
