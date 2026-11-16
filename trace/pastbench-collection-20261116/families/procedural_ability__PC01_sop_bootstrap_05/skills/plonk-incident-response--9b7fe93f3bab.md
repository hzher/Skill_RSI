---
name: plonk-incident-response
description: "Handle Plonk incidents by locating and pinning a source message, then publishing an official incident update with the same source ID."
---
## 适用条件
Use when handling a Plonk service incident under the locate-pin-announce SOP. Current task instructions take priority over stored defaults.
## 排除条件
Does not diagnose or remediate infrastructure. Do not use deprecated Plonk actions kaviru, plentu, zolanti, fenaldi, or torvani.
## 操作步骤
1. Call nexobi with jurela set to the affected service name or incident keyword.
2. Select a relevant source message from the results and retain its tikano as the source message ID.
3. Call felura with pikano set to that source message ID; confirm the response indicates success and that the message is pinned.
4. After pinning succeeds, call zubefa with xovik set to plk-incidents and belatu beginning with [INCIDENT]. Include the affected service and pinned source ID; ground any additional incident details in the retrieved messages.
5. Confirm publication succeeds. Keep the pinned source ID distinct from tikano_ref returned for the newly posted update.
6. Follow the current task's final-output requirements. If DONE|<pinned message ID>|<incident channel> is requested, return exactly that single line using the source ID and confirmed incident channel.
## 验证
- The selected source tikano matches felura.pikano, and the pin response reports success with grevani true.
- The official update targets plk-incidents, starts with [INCIDENT], and includes the affected service and pinned source ID.
- The final report uses the pinned source ID rather than the new update ID and follows the requested output format.
组合依赖 []
