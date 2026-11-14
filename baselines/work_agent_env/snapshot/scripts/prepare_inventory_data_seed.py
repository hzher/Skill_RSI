"""Dump owned wc with empty gcal/email, without activating an inventory task."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integrations.inventory_data import SCHEMAS, EMAIL_TABLES, checksum, row_summary
from training.inventory_task_review import PIN, TASK_ID, public_blob
from workenv.core import ResourceError, contained, no_links, save
from workenv.postgres_binary import postgres_binary


def shared_state(root=ROOT):
    from adapters import toolathlon_native as native
    if Path(root).resolve()!=native.ROOT: raise ResourceError("Inventory seed root differs from database owner")
    result={}
    with native.database() as conn:
        conn.rollback();conn.set_session(isolation_level="REPEATABLE READ",readonly=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN ('wc','gcal','email') AND table_type='BASE TABLE' ORDER BY table_schema,table_name")
            tables=cursor.fetchall()
            if not 1<=len(tables)<=96 or {t for s,t in tables if s=='email'}!=set(EMAIL_TABLES) or ('wc','products') not in tables or ('gcal','events') not in tables:
                raise ResourceError("Inventory source schemas incomplete")
            for schema,table in tables:
                if schema not in SCHEMAS or not re.fullmatch(r"[a-z_][a-z0-9_]*",table):raise ResourceError("Inventory table identity differs")
                cursor.execute('SELECT row_to_json(t)::text FROM '+schema+'.'+table+' t ORDER BY row_to_json(t)::text LIMIT 100001')
                rows=[json.loads(r[0]) for r in cursor.fetchall()]
                if len(rows)>100000:raise ResourceError("Inventory shared row budget")
                result[schema+'.'+table]=row_summary(rows)
    return result


def validate_seed(root,record):
    root=Path(root).resolve()
    _,identity=public_blob(root,'task_config.json')
    if (not isinstance(record,dict) or record.get('schema_version')!='inventory-data-seed-v1'
        or record.get('task_id')!=TASK_ID or record.get('source_commit')!=PIN or record.get('task_config_sha256')!=identity['sha256']
        or record.get('schemas')!=list(SCHEMAS) or record.get('writable_rows_included') is not False
        or record.get('runtime_activation') is not False or record.get('training_admission') is not False):
        raise ResourceError('Inventory seed identity or activation claims differ')
    state=record.get('source_state')
    if not isinstance(state,dict) or checksum(state)!=record.get('source_state_sha256') or 'wc.products' not in state:
        raise ResourceError('Inventory seed source state differs')
    path=contained(root,record['seed_ref'])
    if not path.is_relative_to(root/'.runtime/native-episodes/seed-cache') or not path.is_file() or not 0<path.stat().st_size<=16*1024*1024:
        raise ResourceError('Inventory seed path or byte budget differs')
    raw=path.read_bytes()
    if len(raw)!=record.get('bytes') or hashlib.sha256(raw).hexdigest()!=record.get('sha256') or b'COPY email.' in raw or b'COPY gcal.' in raw:
        raise ResourceError('Inventory seed bytes changed or writable rows included')
    return path


def prepare(root=ROOT):
    root=Path(root).resolve()
    from adapters import toolathlon_native as native
    before=shared_state(root)
    _,config=public_blob(root,'task_config.json')
    cache=root/'.runtime/native-episodes/seed-cache';no_links(cache,root);cache.mkdir(parents=True,exist_ok=True)
    temporary=cache/('inventory-dump-'+uuid4().hex+'.sql')
    result=subprocess.run([str(postgres_binary(root,'pg_dump')),'--host=127.0.0.1','--port=5432','--username=eigent',
        '--dbname=toolathlon_gym',*['--schema='+s for s in SCHEMAS],'--exclude-table-data=gcal.*','--exclude-table-data=email.*',
        '--no-owner','--no-privileges','--file='+str(temporary)],env=native.env(),capture_output=True,timeout=40)
    temporary.with_suffix('.stdout.txt').write_bytes(result.stdout);temporary.with_suffix('.stderr.txt').write_bytes(result.stderr)
    if result.returncode or not temporary.is_file() or not 0<temporary.stat().st_size<=16*1024*1024:
        raise ResourceError('Inventory source dump failed')
    after=shared_state(root)
    if after!=before:raise ResourceError('Shared inventory sources changed during dump')
    raw=temporary.read_bytes();sha=hashlib.sha256(raw).hexdigest();target=cache/(sha+'.sql')
    no_links(target,root)
    if target.exists():
        if target.read_bytes()!=raw:raise ResourceError('Inventory immutable seed changed')
    else:
        with target.open('xb') as stream:stream.write(raw)
    record={'schema_version':'inventory-data-seed-v1','task_id':TASK_ID,'source_commit':PIN,
        'task_config_sha256':config['sha256'],'schemas':list(SCHEMAS),'writable_rows_included':False,
        'seed_ref':target.relative_to(root).as_posix(),'sha256':sha,'bytes':len(raw),
        'source_state':before,'source_state_sha256':checksum(before),'runtime_activation':False,'training_admission':False}
    validate_seed(root,record)
    save(root/'.runtime/nemo-vm/inventory-data-seed.json',record)
    return record


if __name__=='__main__':
    record=prepare()
    print(json.dumps({'prepared':True,'seed_sha256':record['sha256'],'runtime_activation':False,'training_admission':False}))
