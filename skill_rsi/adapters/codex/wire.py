"""Responses captures with native call IDs and versioned intervention receipts."""
from ...evidence.archive import pointer
from ...evidence.schema import event
from ..gateway import read_wire, wire_directories
from ...evidence.io import digest, encode


def read(archive, source):
    for run in read_wire(archive, source, "codex"):
        directories = wire_directories(source)
        native_items = {}
        for directory in directories:
            for name, layer in (("request.ingress.raw", "gateway_ingress"), ("client.json", "native_request_identity"),
                                ("preparation-error.json", "gateway_fallback"), ("delivery-error.json", "gateway_receipt_failure"),
                                ("gateway.json", "gateway_identity"),
                                ("operations.json", "component_runtime")):
                path = directory / name
                if path.is_file():
                    ref = archive.ref(path, layer=layer)
                    run.sources.append(ref)
                    if name.endswith('error.json'):
                        run.diagnostics.append({'reason': name.removesuffix('.json'), 'source_ref': ref})
                    if name == "operations.json":
                        for index, decision in enumerate(archive.json(path).get("decisions", [])):
                            run.events.append(event({**ref, "locator": pointer("/decisions/" + str(index))},
                                "operation_receipt", index, actor_type="harness", decision=decision,
                                operation_key=decision.get("operation_key"), teacher_visible=False))
            path = directory / "request.ingress.raw"
            if not path.is_file():
                path = directory / "request.raw"
            if not path.is_file():
                continue
            body = archive.json(path)
            if not isinstance(body, dict) or not isinstance(body.get("input"), list):
                continue
            for index, item in enumerate(body["input"]):
                if not isinstance(item, dict) or item.get("type") not in {
                    "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output",
                }:
                    continue
                ref = archive.ref(path, layer="gateway_native_item", locator=pointer("/input/" + str(index)))
                output = item["type"].endswith("_output")
                meta = archive.json(directory / 'gateway.json') if (directory / 'gateway.json').is_file() else {}
                thread = meta.get('native_request_thread_id') or meta.get('session_id')
                key = (thread, item.get('call_id'), item['type'])
                checksum = digest(encode(item))
                if item.get('call_id') and key in native_items:
                    previous, previous_hash = native_items[key]
                    if checksum == previous_hash:
                        previous.setdefault('observation_refs', []).append(ref)
                    else:
                        run.diagnostics.append({'reason': 'native_call_identity_payload_variant', 'source_ref': ref})
                    continue
                record = event(ref, "tool_result" if output else "tool_call", index,
                    actor_type="harness" if output else "agent", native_tool_call_id=item.get("call_id"),
                    tool_call_id=item.get("call_id"), tool_name=item.get("name"),
                    thread_id=thread, logical_call_id=directory.name, teacher_visible=False)
                run.events.append(record)
                if item.get('call_id'):
                    native_items[key] = record, checksum
        yield run
