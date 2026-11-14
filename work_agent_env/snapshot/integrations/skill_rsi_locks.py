"""RSI lease attestations and explicit dead-owner recovery; no task replay or process killing."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import uuid

from integrations.model_endpoint import strict_json
from integrations.skill_rsi_contract import RSIError, descriptor_id
from training.skill_contract import canonical
from workenv.core import no_links, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity

LOCKS = {'campaign':('.runtime/model-task-campaign.lock',{'skill_rsi_evaluation','skill_rsi_recovery'}),
         'http':('.runtime/skill-rsi-http.lock',{'skill_rsi_http'})}


def host_identity():
    """Stable host evidence stays private; cross-host copies cannot authorize recovery."""
    try:
        if os.name == 'nt':
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Cryptography',
                                0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
                machine = str(uuid.UUID(winreg.QueryValueEx(key,'MachineGuid')[0]))
        else:
            raw = Path('/etc/machine-id').read_bytes()
            if len(raw)>128 or not re.fullmatch(b'[a-fA-F0-9]{32}',raw.strip()):
                raise ValueError()
            machine = raw.strip().decode().lower()
        return hashlib.sha256(canonical({'os':os.name,'machine':machine,'node':socket.gethostname()})).hexdigest()
    except (OSError,ValueError,TypeError,AttributeError):
        raise RSIError('lock_host_identity_unavailable') from None


def read_record(path, root, limit=16384):
    no_links(path,root)
    if not path.is_file() or path.stat().st_size>limit:
        raise RSIError('lock_record_unavailable')
    raw=path.read_bytes()
    try:
        value=strict_json(raw)
        if not isinstance(value,dict):raise ValueError()
    except Exception:
        raise RSIError('invalid_lock_record') from None
    return raw,value


@contextmanager
def rsi_lock(service, which, kind, *, request_id=None):
    if which not in LOCKS or kind not in LOCKS[which][1]:
        raise RSIError('invalid_rsi_lock_kind')
    relative,_=LOCKS[which]
    with owned_lock(service.root/relative,service.root,kind) as record:
        raw,observed=read_record(service.root/relative,service.root)
        if observed!=record:
            raise RSIError('lock_ownership_changed')
        directory=service.store.directory/'lock-claims'
        no_links(directory,service.root);directory.mkdir(parents=True,exist_ok=True)
        if len(list(directory.glob('*.json')))>=2048:
            raise RSIError('lock_claim_limit')
        claim={'schema_version':'skill-rsi-lock-claim-v1','workspace_owner':str(service.root),
            'resource':relative,'owner_token':record['owner_token'],'process':record['process'],
            'host_sha256':host_identity(),'executor_id':service.executor_id,'request_id':request_id,
            'lock_sha256':hashlib.sha256(raw).hexdigest()}
        target=directory/(record['owner_token']+'.json')
        no_links(target,service.root);save(target,claim);os.chmod(target,0o600)
        yield record


def _dead_owner(record):
    birth=record.get('process')
    if (not isinstance(birth,dict) or birth.get('status')!='alive' or not birth.get('creation_id')
        or type(birth.get('pid')) is not int):
        raise RSIError('lock_process_identity_unavailable')
    try:
        actual=process_identity(birth['pid'])
    except ValueError:
        raise RSIError('lock_process_identity_unavailable') from None
    if actual.get('source')!=birth.get('source'):
        raise RSIError('lock_process_source_changed')
    if actual.get('status')=='not_found':return 'not_found'
    if actual.get('status')=='exited' and actual.get('creation_id')==birth['creation_id']:return 'exited'
    if actual.get('status')=='alive' and actual.get('creation_id'):
        if actual['creation_id']!=birth['creation_id']:return 'pid_reused'
        raise RSIError('lock_owner_still_alive')
    raise RSIError('lock_process_identity_unavailable')


def recover_lock(service, which):
    """Explicit single-lock action. SQLite serializes reclaimers and releases on process exit."""
    if which not in LOCKS:raise RSIError('invalid_rsi_lock_kind')
    relative,kinds=LOCKS[which];path=service.root/relative
    service.assert_frozen()
    # No permanent recovery lock: a failed recovery process must not add another stale lock.
    with service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        no_links(path,service.root)
        if not path.exists():
            return {'schema_version':'skill-rsi-lock-recovery-v1','resource':relative,'state':'already_absent',
                    'model_http_requests':0,'evaluation_replayed':False,'evaluation_state_modified':False}
        raw,record=read_record(path,service.root)
        token=record.get('owner_token')
        if (record.get('schema_version')!='workenv-lock-v0.1' or record.get('workspace_owner')!=str(service.root)
            or record.get('resource')!=relative or record.get('kind') not in kinds
            or not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token)):
            raise RSIError('lock_not_owned_by_skill_rsi')
        _,claim=read_record(service.store.directory/'lock-claims'/(token+'.json'),service.root)
        if (claim.get('schema_version')!='skill-rsi-lock-claim-v1' or claim.get('workspace_owner')!=str(service.root)
            or claim.get('resource')!=relative or claim.get('owner_token')!=token
            or claim.get('process')!=record.get('process') or claim.get('lock_sha256')!=hashlib.sha256(raw).hexdigest()):
            raise RSIError('lock_claim_binding_mismatch')
        if claim.get('host_sha256')!=host_identity():
            raise RSIError('lock_host_identity_changed')
        executor=claim.get('executor_id')
        if not isinstance(executor,str) or not re.fullmatch('[a-f0-9]{64}',executor):
            raise RSIError('invalid_lock_executor')
        _,descriptor=read_record(service.store.directory/'executors'/executor/'descriptor.json',service.root,1024*1024)
        if descriptor_id(descriptor)!=executor:
            raise RSIError('lock_executor_snapshot_invalid')
        if which=='campaign':
            identity=claim.get('request_id')
            try:
                if str(uuid.UUID(identity))!=identity:raise ValueError()
            except (ValueError,TypeError,AttributeError):
                raise RSIError('invalid_lock_request') from None
            row=db.execute('SELECT executor_id FROM evaluations WHERE request_id=?',(identity,)).fetchone()
            if row is None or row['executor_id']!=executor:
                raise RSIError('lock_request_binding_mismatch')
        elif claim.get('request_id') is not None:
            raise RSIError('invalid_lock_request')
        observation=_dead_owner(record)
        try:
            service.preflight()  # Actual runtime must be owned, ready and idle. Does not close resources.
        except Exception:
            raise RSIError('lock_runtime_not_ready') from None
        service.assert_frozen()
        if _dead_owner(record) not in ('not_found','exited','pid_reused'):
            raise RSIError('lock_process_identity_unavailable')
        current,_=read_record(path,service.root)
        if current!=raw:raise RSIError('lock_ownership_changed')
        event_id=str(uuid.uuid4())
        directory=service.store.directory/'lock-recovery'/event_id
        no_links(directory,service.root)
        if len(list((service.store.directory/'lock-recovery').glob('*')))>=256:
            raise RSIError('lock_recovery_event_limit')
        directory.mkdir(parents=True)
        (directory/'original.lock').write_bytes(raw)
        os.chmod(directory/'original.lock',0o600)
        save(directory/'claim.json',claim)
        result={'schema_version':'skill-rsi-lock-recovery-v1','event_id':event_id,'resource':relative,
            'state':'recovered','previous_lock_sha256':hashlib.sha256(raw).hexdigest(),
            'owner_observation':observation,'model_http_requests':0,'evaluation_replayed':False,
            'evaluation_state_modified':False,'task_closure_proven_by_lock_recovery':False}
        save(directory/'intent.json',{'phase':'prepared','action':result})
        # Only cooperating reclaimers remove stale locks; they share this SQLite transaction.
        # Normal owners use O_EXCL and cannot replace the still-present file.
        no_links(path,service.root)
        if path.read_bytes()!=raw:raise RSIError('lock_ownership_changed')
        path.unlink()
        save(directory/'completion.json',result)
        target=service.root/'evidence/skill_rsi/lock_recovery'/(event_id+'.json')
        no_links(target,service.root);save(target,result)
    return result
