---
name: change-freeze-lookup
description: "Recover the applicable approved change freeze packet from prior decisions before updating or sharing a freeze note."
---
## 适用条件
Use when a user refers to a previously approved change freeze packet without restating its exact details.
## 排除条件
Does not authorize a new freeze or exception. Do not treat drafts, incomplete shells or partial recaps as authoritative packets.
## 操作步骤
1. Establish the requested project and scope from the request and current note, then search session history for matching freeze decisions.
2. When matching approved packets conflict, compare approval revisions, approval issuance times and explicit replacement language to identify the applicable approved amendment; do not rely on search order or a 'latest' title alone.
3. Reject stale or wrong-scope packets and one-off waivers that do not replace the current packet. Do not let incomplete shells, drafts or recaps override a complete approved decision.
4. Recover the exact freeze packet ID, UTC start time, UTC end time and exception projects together from the selected approved revision; do not mix fields from earlier revisions or partial summaries.
5. If approval or supersession remains unclear, search specifically for approval amendments or revisions before proceeding.
6. Only after resolving the applicable packet, update or share the current freeze note or announcement in the requested format.
## 验证
- The selected packet matches the requested project and scope and is not superseded by another applicable approved revision in the retrieved decisions.
- The packet ID, full UTC window and complete exception set all come from the same selected revision, with no stale exceptions carried over.
- Any note update or announcement reproduces the recovered fields accurately and follows the user's requested format.
组合依赖 []
