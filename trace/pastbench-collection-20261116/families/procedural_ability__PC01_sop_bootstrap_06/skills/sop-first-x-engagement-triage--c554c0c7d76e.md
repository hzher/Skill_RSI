---
name: sop-first-x-engagement-triage
description: "Review X mentions and timeline, classify posts using the current onboarding SOP and author profiles, and execute only the prescribed engagement action."
---
## 适用条件
Use when authorized to manage a company's X account under an onboarding or engagement reference that routes positive mentions, complaints, relevant expert posts, and competitor-tagged posts.
## 排除条件
Do not treat these routing examples as universal platform rules. Current task instructions and the current SOP govern; do not infer follower reach from post engagement metrics or verification alone.
## 操作步骤
1. Try to read the current onboarding reference for routing, exclusions, thresholds, and wording. If only an index is available and the full reference is unavailable, use a retained procedure only if the task explicitly permits it. Do not infer detailed rules from the index; current instructions and available policy override retained rules.
2. Fetch current mentions and timeline with getMentions and getTimeline. Inspect each post for company sentiment, competitor mentions, and relevant expert content.
3. Apply explicit ignore rules before engagement. Under the observed SOP, posts mentioning a competitor alongside the company receive no like, reply, repost, quote, or DM.
4. Use getUserProfile to obtain the author's username, follower count, and verification before choosing an action dependent on those facts. Do not substitute tweet likes or repost counts for follower count.
5. For positive mentions, apply the current follower threshold. The observed SOP routes accounts with at least 1,000 followers to replyToTweet with a short acknowledgment, the author's handle, and the required company hashtag.
6. For positive mentions below the threshold, like the post only; do not add a public reply. Use the supported action name: the observed successful tool call used likeTweet.
7. For negative mentions, use sendDM rather than a public reply. Follow the SOP's required apology opening and provide relevant support next steps; the observed message requested a ticket number and discouraged sending payment-card details.
8. For a relevant post from a verified expert, use quoteTweet with a company perspective or relevant insight, rather than treating high reach alone as sufficient justification.
9. Check action results for success and correct target before reporting completion. Summarize only confirmed actions and distinguish any skipped or unsuccessful items. If a retained procedure was used because the full current reference was unavailable, disclose that limitation.
## 验证
- Each action traces to a rule in the current reference or an explicitly authorized retained procedure and the post's content; profile-dependent decisions use retrieved profile data.
- Positive high-reach replies contain required wording and hashtag; low-reach positives receive only a like under this SOP.
- Complaints receive private support rather than public replies, competitor-tagged posts remain untouched, and expert quotes require both relevance and verification.
- Tool results confirm the intended action and target; final reporting does not claim unconfirmed changes.
- Any fallback to a retained procedure is explicitly authorized by the task, respects current instructions and available policy, and is disclosed in the final report.
组合依赖 []
