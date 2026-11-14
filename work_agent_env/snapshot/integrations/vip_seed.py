"""Source, build and immutable SQL bindings for the isolated cloud/mail task."""
import hashlib
import json
from pathlib import Path
import re

from training.email_task_review import PIN, TASK_ID, BASE, PUBLIC_SHA256
from workenv.core import ResourceError, contained, no_links, read_json, save

SEED_REF = ".runtime/nemo-vm/vip-seed.json"


def shared_readonly():
    from adapters import toolathlon_native as native
    data = {}
    with native.database() as connection:
        connection.rollback(); connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            for name in ("customers", "orders"):
                cursor.execute("SELECT row_to_json(t)::text FROM wc." + name + " t ORDER BY row_to_json(t)::text LIMIT 100001")
                data[name] = [json.loads(row[0]) for row in cursor.fetchall()]
                if len(data[name]) > 100000: raise ResourceError("VIP seed source row budget")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validated_seed(root, record=None):
    root=Path(root).resolve()
    if record is None:
        no_links(root/SEED_REF,root);record=read_json(root/SEED_REF)
    from integrations.email_build import verify_build
    build_path=root/'evidence/mainline/email_build.json';no_links(build_path,root)
    build=read_json(build_path);verify_build(root,build)
    if (record.get('schema_version')!='vip-isolated-runtime-seed-v1' or record.get('task_commit')!=PIN
        or record.get('task_id')!=TASK_ID or record.get('schemas')!=['wc','gsheet','email']
        or record.get('sheets_and_email_rows_included') is not False
        or record.get('task_config_sha256')!=PUBLIC_SHA256[BASE+'task_config.json']
        or record.get('email_build_fingerprint')!=build.get('build_fingerprint')
        or record.get('email_build_record_sha256')!=hashlib.sha256(build_path.read_bytes()).hexdigest()
        or record.get('training_sample') is not False
        or not isinstance(record.get('readonly_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',record['readonly_sha256'])):
        raise ResourceError('VIP runtime seed task, data, source or email build binding differs')
    path=contained(root,record['seed_ref'])
    if (not path.is_relative_to(root/'.runtime/native-episodes/seed-cache') or not path.is_file()
        or not 0<path.stat().st_size<=16*1024*1024):raise ResourceError('VIP runtime seed escaped owned immutable cache')
    raw=path.read_bytes()
    if (hashlib.sha256(raw).hexdigest()!=record.get('sha256') or len(raw)!=record.get('bytes')
        or b'COPY email.' in raw or b'COPY gsheet.' in raw):raise ResourceError('VIP runtime SQL changed or includes writable rows')
    return record,path


def prepare(root):
    root=Path(root).resolve()
    from adapters import toolathlon_native as native
    if root != native.ROOT.resolve(): raise ResourceError('VIP seed preparation requires its owned project root')
    from scripts.prepare_email_component_seed import main as prepare_components
    before=shared_readonly()
    prepare_components()
    seed=read_json(root/'.runtime/nemo-vm/email-component-seed.json')
    if shared_readonly()!=before:raise ResourceError('VIP seed read-only source changed during dump')
    build=read_json(root/'evidence/mainline/email_build.json')
    record={**seed,'schema_version':'vip-isolated-runtime-seed-v1','task_commit':PIN,
        'readonly_sha256':before,'email_build_fingerprint':build['build_fingerprint'],
        'email_build_record_sha256':hashlib.sha256((root/'evidence/mainline/email_build.json').read_bytes()).hexdigest(),
        'bytes':contained(root,seed['seed_ref']).stat().st_size,'training_sample':False}
    validated_seed(root,record)
    save(root/SEED_REF,record)
    return record
