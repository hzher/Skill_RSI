"""Four pinned public inventory-task files; no scripts or task answers executed."""
import ast
import hashlib
import json
from pathlib import Path
import re

from training.source_api import pinned_blob
from integrations.mainline_audit import read_json, source_binding
from workenv.core import save

ROOT=Path(__file__).resolve().parents[1]
PIN='ed735ba0af71d82688a55f952a6ea11479ae7206'
TASK_ID='wc-inventory-alert-gcal'
BASE='tasks/finalpool/'+TASK_ID+'/'
PUBLIC_SHA256={'task_config.json':'0896b8d088aaf24deccd04072c895acca7addfb1e80d1e73ec8516f1b0b1c069',
    'docs/task.md':'4cec269be21ca3f8342d19f21337edcad54cd8040e35af30cab1e8a170573fe1',
    'preprocess/main.py':'19bbaba83c2a5883e0c12bb10aabbb5b0131586f201c8e96da462d013a8cb2db',
    'evaluation/main.py':'d95d60b61a1d3c0579b1cf58580ab38431e2b9f3d9dcfb522e84dbc2fb9e7795'}
SOURCES=('training/inventory_task_review.py','training/source_api.py','configs/mainline_sources.lock.json','adapters/email_pg_native.py')
REPORT='evidence/mainline/inventory_task_review.json'


def public_blob(root,member):
    if member not in PUBLIC_SHA256:raise ValueError('Only four enumerated public inventory files may be read')
    source=json.loads((root/'configs/mainline_sources.lock.json').read_text())['sources']['toolathlon_gym']
    if source['commit']!=PIN:raise ValueError('Inventory public source pin differs')
    raw,identity=pinned_blob(root,source,BASE+member)
    if hashlib.sha256(raw).hexdigest()!=PUBLIC_SHA256[member]:raise ValueError('Inventory public source bytes changed')
    return raw,identity


def literal(tree,name):
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            try:return ast.literal_eval(node.value)
            except (ValueError,TypeError):return None
    return None


def script_facts(preprocess,evaluator):
    pre,ev=ast.parse(preprocess),ast.parse(evaluator)
    def strings(tree):return [n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
    def tables(tree,operation):
        return sorted({name for text in strings(tree) for name in re.findall(operation+r'\s+([A-Za-z_]+\.[A-Za-z_]+)',text,re.I)})
    config=next((n.value for n in ev.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DB_CONFIG' for t in n.targets)),None)
    port=None
    if isinstance(config,ast.Dict):
        for k,v in zip(config.keys,config.values):
            if isinstance(k,ast.Constant) and k.value=='port':
                try:port=ast.literal_eval(v)
                except (ValueError,TypeError):pass
    ids=literal(ev,'OOS_PRODUCT_IDS')
    return {'preprocess_delete_tables':tables(pre,'DELETE FROM'),'evaluator_read_tables':tables(ev,'FROM'),
        'evaluator_port_literal':port,'reference_workspace_required':any(isinstance(n,ast.Name) and n.id=='groundtruth_workspace' for n in ast.walk(ev)),
        'public_evaluator_inventory_constant_count':len(ids) if isinstance(ids,list) else None,
        'clock_calls':sorted({ast.unparse(n.func) for tree in (pre,ev) for n in ast.walk(tree)
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ('now','utcnow','today')})}


def inspect(root=ROOT):
    raw={};objects={}
    for name in PUBLIC_SHA256:raw[name],objects[name]=public_blob(root,name)
    facts=script_facts(raw['preprocess/main.py'],raw['evaluation/main.py'])
    sender=literal(ast.parse((root/'adapters/email_pg_native.py').read_text()),'SENDER')
    config=json.loads(raw['task_config.json'])
    return {'schema_version':'inventory-public-task-review-v1','task_id':TASK_ID,'source_commit':PIN,'source_objects':objects,
        'needed_mcp_servers':config['needed_mcp_servers'],'needed_local_tools':config['needed_local_tools'],'facts':facts,
        'reviewed_requirements':{'stock_quantity':0,'report_filename':'out_of_stock_report.txt','sort':'product_id_ascending',
            'calendar_date':'2026-03-10','calendar_start':'09:00','calendar_end':'10:00','calendar_timezone':'America/New_York',
            'calendar_name_limit':50,'category_selection':'first_category_name','missing_category':'Uncategorized',
            'summary_emails':1,'sender':'purchasing@ourstore.com','recipient':'warehouse@ourstore.com',
            'subject':'Out of Stock Alert - March 2026','reference':'pinned_docs_task_md'},
        'existing_email_default_sender':sender,'task_sender_matches_existing_default':sender=='purchasing@ourstore.com',
        'port_compatible_with_current_5432_slots':facts['evaluator_port_literal']==5432,
        'next_required_checks':['complete_original_product_pagination_and_zero_stock_sql',
            'compare_public_evaluator_inventory_constant_without_using_it_as_truth',
            'task_bound_sender_preserving_existing_vip_default','bounded_transactional_utf8_text_report',
            'explicit_project_sql_derived_reference_mode_without_upstream_answers',
            'exact_calendar_title_description_start_end_timezone_and_no_extras',
            'single_exact_summary_email_and_send_log_binding','task_specific_original_preprocess_and_three_child_lifecycle'],
        'original_scripts_executed':False,'reference_files_read':False,'verified_task_bodies_read':False,
        'runtime_activation':False,'training_admission':False,'source_completeness_verified':False,
        'resource_ownership_verified':False,'official_benchmark_score_available':False,
        'source_sha256':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in SOURCES}}


def audit_review(root,path):
    report=read_json(path,root)
    source_binding(root,report.get('source_sha256'))
    if report!=inspect(root):raise ValueError('Inventory public review changed or acquired runtime authority')
    return {'public_files_reviewed':4,'original_scripts_executed':False,'reference_files_read':False,
        'runtime_profiles_added':0,'training_admission':False}


def main():
    report=inspect();save(ROOT/REPORT,report);print(json.dumps(audit_review(ROOT,ROOT/REPORT)))


if __name__=='__main__':main()
