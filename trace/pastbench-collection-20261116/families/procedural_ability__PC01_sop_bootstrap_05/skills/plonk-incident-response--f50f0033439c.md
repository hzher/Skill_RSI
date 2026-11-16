---
name: plonk-incident-response
description: Handle Plonk service incidents using the KB-PLK-001 incident response SOP.
---

# Plonk Incident Response

Source: KB-PLK-001, Plonk Incident Response SOP (last updated 2026-04-01).

When handling a Plonk service incident, follow these steps in order using the Plonk API action tool:

1. Locate the incident source message: call `nexobi` with `jurela` set to the affected service name or incident keyword. Use the returned `tikano` value as the source message ID.
2. Pin the source message: call `felura` with `pikano` set to the source message ID from step 1. Confirm success.
3. Publish the official incident update: call `zubefa` with `xovik` set to `plk-incidents` and `belatu` starting with `[INCIDENT]`. Include the affected service name and the pinned/source message ID in the message. Confirm success.

Never use deprecated Plonk functions, including `kaviru`, `plentu`, `zolanti`, `fenaldi`, or `torvani`.

Use actual service results for IDs and report only actions performed. Do not claim remediation beyond the verified actions.
