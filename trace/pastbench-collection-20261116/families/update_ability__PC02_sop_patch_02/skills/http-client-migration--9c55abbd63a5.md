---
name: http-client-migration
description: Apply the corrected HTTP client migration guide to code review tickets.
---

# HTTP client migration standard

Read the current HTTP Client Migration Guide and HTTP Client Migration FAQ in the knowledge base before applying this procedure. The corrected guide supersedes the earlier reversed rules.

- requests is deprecated; no new PRs may use it. httpx is the preferred replacement for synchronous and asynchronous HTTP. aiohttp is also compliant for asynchronous HTTP.
- For PRs using import requests or requests.get/post/Session, set priority=medium and category=code-review, and add deprecated-api and needs-migration tags. The entire requests family is covered, including Session and HTTPAdapter. Recommend migration to httpx (preferred) or aiohttp for async-only use.
- PRs using httpx or aiohttp receive api-compliant; no migration is needed. The guide specifies no priority or category change for compliant PRs; preserve those fields unless another applicable rule requires a change.
- requests mocks in tests must also migrate; use respx for httpx mocking.
- The guide's migration deadline is June 30, 2026. Do not invent an escalation or higher priority solely because the deadline has passed; apply the guide's specified classification.
- Do not classify urllib as deprecated under this guide; the corrected guide does not establish that rule.
- Inspect tickets before changing them. Preserve unrelated tags and fields; correct contradictory migration/compliance tags when applying these rules. Leave unrelated or excluded tickets, including documentation requests that are not code reviews, unchanged.

Sources: kb_http_client_migration_v1 (updated April 1, 2026) and kb_http_migration_faq (updated April 5, 2026).
