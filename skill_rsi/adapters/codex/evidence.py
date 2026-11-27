"""Add source-backed gateway/MCP observations to an explicitly captured CLI run."""
import json

from ...evidence.archive import pointer
from ...evidence.schema import event
from .wire import read as read_wire


def enrich(archive, directory, runs):
    launch_path = directory / 'launch.json'
    if not launch_path.is_file():
        return runs
    launch = archive.json(launch_path)
    if not launch.get('capture_id'):
        return runs
    session = launch.get('session_id')
    matches = [run for run in runs if session and run.identity.get('native_session_id') == session]
    if len(matches) != 1:
        # Session similarity is insufficient to bind a physical request or side effect.
        for run in runs:
            run.diagnostics.append({'reason': 'capture_root_session_unresolved'})
        return runs
    run, = matches
    for path in sorted((directory / 'native-session').rglob('*.jsonl')):
        raw = archive.ref(path, layer='native_session_snapshot')
        run.sources.append(raw)
        run.events.append(event(raw, 'native_session_snapshot', 0, actor_type='harness', teacher_visible=False))
    for path in sorted((directory / 'catalog').glob('*/*')):
        if path.is_file():
            raw = archive.ref(path, layer='native_model_catalog')
            run.sources.append(raw)
            if path.name == 'receipt.json':
                run.events.append(event(raw, 'auxiliary_transport_receipt', 0, actor_type='harness', teacher_visible=False))
    for pattern, kind in (('native-events/*.json', 'native_plugin_event'),
                          ('native-decisions/*.json', 'native_plugin_decision')):
        for path in sorted(directory.glob(pattern)):
            raw = archive.ref(path, layer='native_plugin')
            run.sources.append(raw)
            run.events.append(event(raw, kind, 0, actor_type='harness', teacher_visible=False))
    wire = directory / 'wire'
    if wire.is_dir() and any(wire.rglob('request.raw')):
        for observed in read_wire(archive, wire):
            run.events.extend(observed.events)
            run.requests.extend(observed.requests)
            run.sources.extend(observed.sources)
            run.diagnostics.extend(observed.diagnostics)
        run.diagnostics.append({'reason': 'gateway_requests_not_linked_to_native_inference_ids'})
    for name, kind, layer in (
        ('mcp-events.jsonl', 'mcp_tool_receipt', 'native_mcp'),
        ('plugin-events.jsonl', 'operation_receipt', 'native_plugin'),
    ):
        path = directory / name
        if not path.is_file():
            continue
        for row, raw, sequence in archive.jsonl(path, layer):
            run.events.append(event(raw, kind, sequence, actor_type='harness',
                                    native_session_id=session, timestamp=row.get('timestamp'),
                                    mcp_request_id=row.get('call_id'), tool_name=row.get('name'),
                                    is_error=row.get('error'), model_input_visibility='unknown',
                                    source_actor=row.get('source_actor'), teacher_visible=False))
    for name in ('prompt.json', 'components.json', 'mcp-loaded.json', 'native-install.json',
                 'native-uninstall.json', 'native-session.json', 'native-health.json', 'query-prepared.json'):
        path = directory / name
        if path.is_file():
            raw = archive.ref(path, layer='capture_configuration')
            run.sources.append(raw)
            run.events.append(event(raw, 'capture_receipt', 0, actor_type='harness', teacher_visible=False))
    return runs
