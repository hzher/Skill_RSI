"""Read-only capacity observations; this module never reserves or clears sessions."""
from collections import Counter
import math
import time

from integrations.resource_ledger import ResourceLedger

LEDGERS = {"original_toolathlon": ("resource-sessions.json", 1), "project_synthetic": ("synthetic-sessions.json", 4),
           "isolated_toolathlon": ("isolated-sessions.json", 2)}


def capacity_report(root, *, required_native=0, required_synthetic=0, required_isolated=0, clock=time.time):
    required = {"original_toolathlon": required_native, "project_synthetic": required_synthetic, "isolated_toolathlon": required_isolated}
    if any(type(value) is not int or not 0 <= value <= 128 for value in required.values()):
        raise ValueError("Requested new-session counts must be integers in 0..128")
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now): raise ValueError("Invalid observation clock")
    observations = {}
    for source, (filename, concurrent) in LEDGERS.items():
        path = root / ".runtime/nemo-vm" / filename
        exists = path.is_file()
        if source == "isolated_toolathlon" and not exists and required_isolated == 0: continue
        ledger = ResourceLedger(path, root)
        counts = Counter(row["state"] for row in ledger.rows.values())
        rollback = 0
        for row in ledger.rows.values():
            access = row.get("last_access_unix")
            if type(access) not in (int, float) or not math.isfinite(access):
                raise ValueError("Invalid persisted resource timestamp")
            rollback += access > now
        held = sum(counts[state] for state in ("seeding", "active", "closing"))
        free_identities = ledger.capacity - len(ledger.rows)
        free_workers = max(0, concurrent - held)
        observations[source] = {"ledger_exists": exists, "states": {s: counts[s] for s in ("seeding", "active", "closing", "closed")},
            "retained_identities": len(ledger.rows), "identity_capacity": ledger.capacity, "remaining_new_identities": free_identities,
            "retired_identities": len(ledger.retired),
            "configured_resource_slots": concurrent, "observed_free_resource_slots": free_workers,
            "requested_new_sessions": required[source], "fits_observed_capacity": bool(exists and required[source] <= free_identities and (required[source] == 0 or free_workers > 0)),
            "clock_rollback_rows": rollback, "lease_expiry_not_inferred": True}
    return {"schema_version": "resource-capacity-observation-v1", "sources": observations,
            "fits_observed_capacity": all(row["fits_observed_capacity"] for row in observations.values()),
            "reserves_resources": False, "ledger_modified": False, "automatic_tombstone_deletion": False}
