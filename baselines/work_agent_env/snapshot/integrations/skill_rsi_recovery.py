"""Operator-only reconciliation of retained evidence. Never executes or retries a task."""
import hashlib
import json
import uuid

from integrations.skill_rsi_contract import RSIError
from workenv.core import no_links, save
from integrations.skill_rsi_locks import rsi_lock


def read_bounded(path, root, limit=1024*1024):
    no_links(path,root)
    if not path.is_file() or path.stat().st_size > limit:
        raise RSIError('recovery_evidence_unavailable')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError,RecursionError):
        raise RSIError('invalid_recovery_evidence') from None


def reconcile(service, identity):
    try:
        if str(uuid.UUID(identity)) != identity:
            raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise RSIError('invalid_request_id') from None
    job = service.store.get(identity)
    if job['state'] in ('completed','rejected'):
        return job
    if job['executor_id'] != service.executor_id:
        raise RSIError('recovery_requires_original_executor')
    with rsi_lock(service,'campaign','skill_rsi_recovery',request_id=identity):
        service.preflight();service.assert_frozen()
        job = service.store.get(identity)
        if job['state'] in ('completed','rejected'):
            return job
        with service.store.connect() as db:
            body = json.loads(db.execute('SELECT request_json FROM evaluations WHERE request_id=?',(identity,)).fetchone()[0])
        body = service.validate(body)
        manifests = list((service.root/'.runtime/model-task-runs').glob('*/manifest.json'))
        if len(manifests) > 512:
            raise RSIError('recovery_scan_limit')
        matched = []
        for path in manifests:
            record = read_bounded(path,service.root)
            if record.get('evaluation_request_id') == identity:
                matched.append(path)
        if not matched and job['state'] == 'accepted':
            # accepted is before the executor dispatch. No attempt or task resource exists.
            service.store.transition(identity,'accepted','rejected',{'error':'cancelled_before_dispatch','model_http_requests':0})
            return service.store.get(identity)
        if len(matched) != 1:
            raise RSIError('recovery_attempt_not_unique')
        directory = matched[0].parent
        report = read_bounded(directory/'result.json',service.root)
        if report.get('run_id') != directory.name:
            raise RSIError('recovery_attempt_binding_mismatch')
        result = service.result_from_report(body,report)
        if result['closed'] is not True or result['source_unchanged'] is not True:
            raise RSIError('recovery_completion_not_proven')
        hashes = report.get('private_input_sha256')
        prefix = directory.relative_to(service.root).as_posix()+'/'
        if not isinstance(hashes,dict) or not 1 <= len(hashes) <= 256 or prefix+'trace.jsonl' not in hashes:
            raise RSIError('recovery_input_hashes_missing')
        total = 0
        for name,digest in hashes.items():
            if not isinstance(name,str) or not name.startswith(prefix):
                raise RSIError('recovery_input_path_invalid')
            path = service.root/name
            no_links(path,service.root)
            if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
                raise RSIError('recovery_input_path_invalid')
            total += path.stat().st_size
            if total > 128*1024*1024:
                raise RSIError('recovery_input_size_limit')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RSIError('recovery_input_hash_mismatch')
        trace = directory/'trace.jsonl'
        closed = False
        with trace.open(encoding='utf-8') as stream:
            for line in stream:
                if len(line) > 4*1024*1024:
                    raise RSIError('recovery_trace_line_limit')
                record = json.loads(line)
                if (record.get('kind') == 'close' and isinstance(record.get('value'),dict)
                    and record['value'].get('resources_session_id') == report.get('resources_session_id')
                    and report.get('resources_session_id') is not None):
                    closed = True
        if not closed:
            raise RSIError('recovery_close_receipt_missing')
        public = service.root/'evidence/skill_rsi/runs'/(identity+'.json')
        no_links(public,service.root);save(public,result)
        service.store.transition(identity,job['state'],'completed',result)
    return service.store.get(identity)
