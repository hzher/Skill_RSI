"""Original inventory evaluator and SQL-derived supplemental component checks.

References are generated from this owned SQL snapshot, never upstream answers.
No original tool execution, runtime activation, task reward or training approval
is implied by a successful component measurement.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from adapters import inventory_alert_validation as validation
from adapters import inventory_alert_artifacts as artifacts
from integrations import inventory_data as data
from integrations.inventory_data_audit import constant_ids
from workenv.core import ResourceError, contained, no_links, save

REFERENCE_MODE = "project_derived_sql_oracle"
SOURCE_NAME = "inventory-evaluation-source.json"


def classify_original(code, stdout, stderr):
    if type(code) is not int or code not in (0,1) or not isinstance(stdout,str) or not isinstance(stderr,str):return None
    if stderr or len(stdout.encode('utf-8'))>256*1024:return None
    stdout=stdout.replace('\r\n','\n')
    markers=('=== Checking out_of_stock_report.txt ===','=== Checking Google Calendar ===','=== Checking Emails ===','=== SUMMARY ===')
    if any(stdout.count(marker)!=1 for marker in markers):return None
    found=re.search(r'=== SUMMARY ===\n\s*Passed: (\d+)\n\s*Failed: (\d+)\n\s*Overall: (PASS|FAIL)\s*$',stdout)
    if not found:return None
    passed,failed=int(found[1]),int(found[2])
    if passed!=stdout.count('[PASS]') or failed!=stdout.count('[FAIL]') or passed+failed<1:return None
    if found[3]=='PASS' and code==0 and failed==0:return True
    if found[3]=='FAIL' and code==1 and failed>0:return False
    return None


def sql_report(snapshot):
    data.validate_snapshot(snapshot)
    return '\n'.join(f'{r["id"]} - {r["name"]} - {r["sku"]} - {r["category"]}' for r in snapshot['sql_zero_stock'])


def supplement(snapshot, report):
    return validation.measure(snapshot['products'],report,snapshot['events'],snapshot['email']['messages'],
        snapshot['email']['sent_log'],source_binding=validation.source_identity())


def combine(original,supplemental):
    known=type(original) is bool and supplemental.get('evaluation_status')=='completed' and type(supplemental.get('supplemental_passed')) is bool
    return {'original_passed':original,'supplemental':supplemental,
        'component_measurement_passed':bool(original and supplemental['supplemental_passed']) if known else None,
        'evaluation_status':'completed' if known else 'error','task_passed':None,'reward':None}


def source_proof(root,cluster,snapshot):
    path=cluster.profile.base/SOURCE_NAME;no_links(path,root)
    if not path.is_file() or path.stat().st_size>8192:raise ResourceError('Inventory original product proof missing')
    record=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(record,dict) or record.get('schema_version')!='inventory-evaluation-source-v1'
        or record.get('task_id')!=data.TASK_ID or record.get('profile')!=cluster.profile.record()
        or record.get('contract_source')!=validation.source_identity()
        or record.get('readonly_sha256')!=snapshot['readonly_sha256']):
        raise ResourceError('Inventory original product proof identity differs')
    pages=contained(root,record['pages_ref'])
    if not pages.is_relative_to(cluster.profile.base) or pages.stat().st_size>4*1024*1024:
        raise ResourceError('Inventory product proof path or size differs')
    raw=pages.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record.get('pages_sha256'):raise ResourceError('Inventory product pages changed')
    data.audit_pages(json.loads(raw),snapshot['products'])
    return path,record


def evaluate(root,cluster):
    root=Path(root).resolve()
    directory=cluster.profile.base/'inventory-evaluations'/uuid4().hex
    no_links(directory,root)
    # Ownership is attested before writing even an error receipt.
    if cluster.profile.owner!=root:raise ValueError('Foreign inventory evaluator resource')
    cluster.require_owner();directory.mkdir(parents=True)
    original=None;supplemental={'supplemental_passed':None,'evaluation_status':'error','errors':['input_unavailable']}
    raw=None;identity=None;executed=False;code=None;error=None
    before=None;after=None;report=None;proof_path=None
    try:
        artifacts.workspace_for(root,cluster)
        report=artifacts.read_report(root,cluster)['text']
        before=data.snapshot(root,cluster)
        proof_path,_=source_proof(root,cluster,before)
        if constant_ids(root)!=[row['id'] for row in before['sql_zero_stock']]:
            raise ResourceError('Original inventory evaluator constants incompatible with owned SQL')
        supplemental=supplement(before,report)
        if supplemental['evaluation_status']!='completed':raise ResourceError('Inventory supplemental inputs unavailable')
        oracle=directory/'oracle';oracle.mkdir()
        (oracle/validation.FILENAME).write_bytes(sql_report(before).encode('utf-8'))
        agent=directory/'agent';agent.mkdir()
        (agent/validation.FILENAME).write_bytes(report.encode('utf-8'))
        raw,identity=data.public_blob(root,'evaluation/main.py')
        script=directory/'original_evaluation.py';script.write_bytes(raw)
        try:
            result=subprocess.run([sys.executable,str(script),'--agent_workspace',str(agent),'--groundtruth_workspace',str(oracle)],
                cwd=directory,env=cluster.profile.environment(),capture_output=True,timeout=30)
        except subprocess.TimeoutExpired as exc:
            executed=True
            (directory/'original.stdout.txt').write_bytes(exc.stdout or b'')
            (directory/'original.stderr.txt').write_bytes(exc.stderr or b'')
            raise ResourceError('Original inventory evaluator exceeded its time budget') from exc
        executed=True;code=result.returncode
        (directory/'original.stdout.txt').write_bytes(result.stdout);(directory/'original.stderr.txt').write_bytes(result.stderr)
        original=classify_original(code,result.stdout.decode('utf-8'),result.stderr.decode('utf-8'))
        after=data.snapshot(root,cluster)
        if after!=before or artifacts.read_report(root,cluster)['text']!=report:
            raise ResourceError('Original inventory evaluator changed its input')
        source_proof(root,cluster,after)
    except Exception as exc:
        original=None;error=type(exc).__name__+': '+str(exc)
    record={**combine(original,supplemental),'schema_version':'inventory-original-component-measurement-v1',
        'task_id':data.TASK_ID,'profile':cluster.profile.record(),'reference_mode':REFERENCE_MODE,
        'upstream_reference_used':False,'reference_files_read':False,'official_benchmark_score_available':False,
        'nemo_task_executed':False,'runtime_activation':False,'training_eligible':False,'real_model_calls':0,'training_updates':0,
        'original_evaluator_executed':executed,'original_evaluator_source':identity,'original_exit_code':code,
        'original_evaluator_sha256':hashlib.sha256(raw).hexdigest() if raw is not None else None,
        'error':error,'evaluation_ref':(directory/'result.json').relative_to(root).as_posix(),
        'source_proof_ref':proof_path.relative_to(root).as_posix() if proof_path else None,
        'source_completeness_verified':proof_path is not None,'owned_database_snapshot_verified':before is not None,
        'input_snapshot_ref':(directory/'input_snapshots.json').relative_to(root).as_posix() if before is not None else None}
    if before is not None:save(directory/'input_snapshots.json',{'before':before,'after':after})
    save(directory/'result.json',record)
    return record
