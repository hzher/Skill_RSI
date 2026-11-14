"""Read tool contracts from retained, closed discovery in the same environment version."""
import hashlib
import re

from integrations.model_endpoint import strict_json
from integrations.model_task_executor import tool_inventory
from integrations.skill_rsi_contract import RSIError
from integrations.skill_rsi_recovery import read_bounded
from workenv.core import no_links


def tool_catalog(service, task_id):
    if task_id not in service.tasks:
        raise RSIError('task_not_available')
    service.assert_frozen()
    manifests = list((service.root/'.runtime/model-task-runs').glob('*/manifest.json'))
    if len(manifests) > 512:
        raise RSIError('catalog_scan_limit')
    candidates = []
    for path in manifests:
        manifest = read_bounded(path,service.root)
        if (manifest.get('task_id') == task_id
            and manifest.get('environment_source_sha256') == service.descriptor['environment_source_sha256']):
            candidates.append((manifest.get('started_at',''),path,manifest))
    for _, path, manifest in sorted(candidates,key=lambda row:row[0],reverse=True):
        if not (path.parent/'result.json').is_file():
            continue
        report = read_bounded(path.parent/'result.json',service.root)
        trace = path.parent/'trace.jsonl'
        no_links(trace,service.root)
        if report.get('closed') is not True or report.get('source_unchanged') is not True or not trace.is_file():
            continue
        inputs=report.get('private_input_sha256')
        if (not isinstance(inputs,dict) or not re.fullmatch('[a-f0-9]{32}',path.parent.name)
            or manifest.get('run_id')!=path.parent.name or report.get('run_id')!=path.parent.name
            or report.get('task_id')!=task_id
            or report.get('environment_source_sha256')!=service.descriptor['environment_source_sha256']
            or inputs.get(path.relative_to(service.root).as_posix())!=hashlib.sha256(path.read_bytes()).hexdigest()
            or not isinstance(report.get('resources_session_id'),str)
            or not isinstance(report.get('episode_id'),dict)):
            raise RSIError('catalog_discovery_binding_mismatch')
        if trace.stat().st_size > 128*1024*1024:
            raise RSIError('catalog_trace_size_limit')
        expected = inputs.get(trace.relative_to(service.root).as_posix())
        if expected != hashlib.sha256(trace.read_bytes()).hexdigest():
            raise RSIError('catalog_trace_hash_mismatch')
        discovered=None;closed=False
        bound={'resources_session_id':report['resources_session_id'],'episode_id':report['episode_id']}
        with trace.open(encoding='utf-8') as stream:
            for line in stream:
                if len(line) > 8*1024*1024:
                    raise RSIError('catalog_trace_line_limit')
                row = strict_json(line)
                value = row.get('value')
                if row.get('kind') == 'nemo' and isinstance(value,dict) and value.get('route') == '/tools':
                    if (discovered is not None or value.get('request')!=bound
                        or not isinstance(value.get('response'),dict)
                        or report.get('task_profile') is not None
                        and value['response'].get('task_profile')!=report['task_profile']):
                        raise RSIError('catalog_discovery_binding_mismatch')
                    discovered=value['response']
                elif row.get('kind')=='nemo' and isinstance(value,dict) and value.get('route')=='/close_session':
                    closed=(discovered is not None and value.get('request')==bound
                            and value.get('response')=={'resources_session_id':report['resources_session_id']})
        if discovered is not None:
            if not closed:raise RSIError('catalog_discovery_binding_mismatch')
            tools,_ = tool_inventory(discovered)
            return {'schema_version':'skill-rsi-tool-catalog-v1','task_id':task_id,
                'source_kind':'retained_closed_discovery','source_run_id':path.parent.name,
                'environment_source_sha256':service.descriptor['environment_source_sha256'],
                'live_availability_attested':False,'model_http_requests':0,'tools':tools}
    raise RSIError('matching_tool_catalog_unavailable')
