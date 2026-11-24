"""Compute the nine fields in the public structured audit-packet contract."""
from collections.abc import Mapping


class EvidenceGap(ValueError):
    """Input does not justify an answer in the registered output schema."""


def required(ok, reason):
    if not ok:
        raise EvidenceGap(reason)


def at(value, *keys):
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def proof(value):
    return isinstance(value, str) and bool(value.strip())


def adjudicate(events, target=None):
    required(isinstance(events, list) and events, "Missing event stream")
    groups = {(e.get("attempt"), e.get("thread")) for e in events}
    required(all(proof(a) and proof(t) for a,t in groups), "Missing attempt/thread binding")
    if target is None:
        required(len(groups) == 1, "Select a specific attempt/thread")
        target = next(iter(groups))
    required(tuple(target) in groups, "Selected attempt/thread absent")
    selected = [e for e in events if (e["attempt"], e["thread"]) == tuple(target)]
    seq = [e.get("seq") for e in selected]
    required(all(type(n) is int for n in seq) and seq == sorted(set(seq)), "Ambiguous event order")
    if len(groups) == 1:
        required(seq == list(range(seq[0],seq[0] + len(seq))), "Missing events inside one attempt")
    complete = False
    gap = False
    explicit = False
    tags, decoded = set(), {}
    acts, receipts, copies = {}, {}, {}
    first = affected = None
    for e in selected:
        k, p = e["event"], e["payload"]
        required(isinstance(p, dict), "Wrong event payload")
        if k == "capture_attestation":
            required(not acts, "Late attestation needs a coverage interval")
            coverage = p.get("actor_input_coverage")
            required(coverage in ("complete", "gap_before_first_action"), "Unknown capture coverage")
            gap |= coverage == "gap_before_first_action"
            complete |= coverage == "complete"
        elif k == "delivery_unknown":
            gap = True
        elif k == "actor_input":
            # Only accept explicitly delivered structured fields. Server-side or
            # assessor-side documents make no changes to the visibility state.
            required(set(p) <= {"tables","notice","previous_settlement","result","earlier_message","history","error","post_settlement_codebook"},
                     "Unsupported actor-input schema")
            tag = at(p, "tables", "opponent_tag")
            if proof(tag):
                tags.add(tag)
            key = at(p, "earlier_message", "decoded_tags")
            if key is not None:
                required(isinstance(key, dict) and all(proof(t) and proof(v) for t,v in key.items()), "Undecodable key")
                required(all(t not in decoded or decoded[t] == v for t,v in key.items()), "Conflicting disclosed keys")
                decoded.update(key)
            explicit |= proof(at(p, "result", "payload", "metadata", "strategy")) or proof(at(p, "error", "details", "opponent_policy"))
            if "post_settlement_codebook" in p:
                required(bool(receipts), "Post-settlement key has no prior settlement")
        elif k == "accepted_act":
            op, kind, amount, cached = (p.get(x) for x in ("call_id","kind","amount","cached"))
            required(proof(op) and proof(kind) and type(amount) is int and type(cached) is bool, "Incomplete action")
            move = (kind,amount)
            if cached:
                required(op not in copies or copies[op] == move, "Conflicting cached action")
                copies[op] = move
                continue
            if op in acts:
                required(acts[op] == move, "Conflicting action ID")
                continue
            required(not receipts, "Action after settlement of the selected attempt")
            acts[op] = move
            visible = explicit or not tags.isdisjoint(decoded)
            if first is None:
                first = "yes" if visible else ("no" if complete and not gap else "unknown")
            if visible and affected is None:
                affected = len(acts)
        elif k in ("settlement","archiver_replay"):
            rid, net = p.get("receipt_id"),p.get("net_chips")
            required(proof(rid) and type(net) is int, "Incomplete settlement")
            if k == "archiver_replay":
                required(rid in receipts, "Replay without original settlement")
            required(rid not in receipts or receipts[rid] == net, "Conflicting settlement")
            receipts[rid] = net
        elif k not in ("host_received","service_generated","assessor_only"):
            raise EvidenceGap("Unrecognized event or unsupported causal intervention")
    required(bool(acts) and bool(receipts), "Missing action or settlement")
    required(all(acts.get(op) == move for op,move in copies.items()), "Unbound cached action")
    return first, affected, list(acts.values()), sum(receipts.values())


def audit_packet(packet):
    cases = packet["cases"]
    required(isinstance(cases, list) and cases, "Missing cases")
    answer = {}
    for case in cases:
        case_id = case["case_id"]
        required(proof(case_id) and case_id not in answer and set(case["arms"]) == {"A","B"}, "Unbound case")
        streams = {}
        row = {"adoption_causal":"unknown"}
        for arm in ("A","B"):
            first, affected, moves, total = adjudicate(case["arms"][arm], case.get("target_bindings",{}).get(arm))
            row[arm + "_at_first"],row[arm + "_first_affected_action"],row[arm + "_net_chips"] = first,affected,total
            streams[arm] = moves
        a,b = streams["A"],streams["B"]
        differing = next((i for i,(x,y) in enumerate(zip(a,b),1) if x != y), None)
        if differing is None and len(a) != len(b):
            differing = min(len(a),len(b)) + 1
        row["first_divergence"] = differing
        row["identical_first_action"] = a[0] == b[0]
        answer[case_id] = row
    return answer
