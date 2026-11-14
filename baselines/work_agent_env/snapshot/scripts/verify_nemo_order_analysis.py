"""Real NeMo third-task diagnostics; all business steps are scripted, no model."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sys
from uuid import uuid4

from mainline_runtime import ROOT, VM, bridge_request
sys.path.insert(0, str(ROOT))
from adapters.order_analysis_validation import TASK_ID, contract_identity, expected_rows
from integrations.episode_cluster import EpisodeProfile, EpisodeCluster, require_dependents_closed
from integrations.nemo_http import NeMoHTTP
from integrations.order_analysis_components import snapshot
from integrations.runtime_identity import expected_identity
from integrations.order_analysis_audit import CHECKS, EXTRA_SOURCES
from scripts.verify_calendar_isolation import shared_snapshot
from workenv.core import save


def main():
    output = ROOT / "evidence/mainline" / ("nemo-order-analysis-" + uuid4().hex)
    output.mkdir()
    clients = [NeMoHTTP() for _ in range(3)]
    tasks = [TASK_ID, TASK_ID, "howtocook-meal-plan-gcal"]
    seeds = [{"resources_session_id": str(uuid4()), "episode_id": {"rollout_id": "order-" + uuid4().hex, "attempt": 0},
        "task_id": {"taskset": "toolathlon_gym:nemo-order-diagnostic", "task_id": task}, "task_data": {}} for task in tasks]
    checks, active, profiles, measurements, retained, pagination = [], [], {}, [], {}, []
    before, after, error = shared_snapshot(), None, None
    other_ledgers = {name: (VM/name).read_bytes() for name in ("resource-sessions.json", "synthetic-sessions.json")}
    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition: raise AssertionError(name)
    def bound(i): return {k: seeds[i][k] for k in ("resources_session_id", "episode_id")}
    def post(i, route, body, expected=200): return clients[i].post("/isolated" + route, body, expected=expected)
    def seed(i):
        post(i, "/seed_session", seeds[i]);active.append(i)
        sid = seeds[i]["resources_session_id"]
        owner = json.loads((ROOT/".runtime/native-episodes"/sid/"cluster_owner.json").read_text())
        profiles[i] = EpisodeProfile(ROOT, sid, owner["slot"])
    def measure(i):
        value = post(i, "/verify", {"episode_id": seeds[i]["episode_id"], "task_id": seeds[i]["task_id"],
            "verification_input": {"resources_session_id": seeds[i]["resources_session_id"]}})["result"]
        measurements.append({"index": i, "result": value})
        return value
    def tool(i, server, name, arguments, request_id=None):
        return post(i, "/tool", {**bound(i), "request_id": request_id or str(uuid4()), "server": server, "name": name, "arguments": arguments})["result"]
    def page_all(i, name):
        values, pages = [], []
        for page in range(1, 65):
            result = tool(i, "woocommerce", name, {"perPage": 10, "page": page})
            if result.get("isError"): raise RuntimeError("Original WooCommerce list returned an error")
            texts = [block["text"] for block in result["content"] if block["type"] == "text"]
            rows = json.loads("".join(texts))
            if not isinstance(rows, list): raise RuntimeError("Original list is not an array")
            pages.append({"page": page, "count": len(rows)})
            values.extend(rows)
            if len(rows) < 10: break
        else: raise RuntimeError("Original pagination exceeded budget")
        if len({row["id"] for row in values}) != len(values): raise RuntimeError("Original pagination duplicated rows")
        pagination.append({"index": i, "tool": name, "pages": pages, "count": len(values)})
        return values
    payloads = {}
    try:
        clients[2].post("/seed_session", seeds[0], expected=400)
        check(CHECKS[0], not bridge_request('/status')['isolated']['active_sessions'])
        for i in (0,1): seed(i)
        check(CHECKS[1], set(bridge_request('/status')['isolated']['active_sessions']) == {s['resources_session_id'] for s in seeds[:2]})
        tools = post(0, '/tools', bound(0))
        check(CHECKS[2], {(t['server'],t['name']) for t in tools['tools']} == {('woocommerce','woo_orders_list'),('woocommerce','woo_products_list'), *[('google_calendar',n) for n in ('create_event','get_event','update_event','delete_event','list_events')]})
        clients[2].post('/isolated/tools',bound(0),expected=409)
        post(0,'/tool',{**bound(0),'request_id':str(uuid4()),'server':'woocommerce','name':'woo_products_create','arguments':{}},expected=400)
        check(CHECKS[3], True)
        check(CHECKS[4], all(measure(i)['reward']==0 for i in (0,1)))
        for i in (0,1):
            orders, products = page_all(i,'woo_orders_list'),page_all(i,'woo_products_list')
            cluster=EpisodeCluster(profiles[i]);data=snapshot(cluster)
            if ({r['id'] for r in orders}!={r['id'] for r in data['orders']} or {r['id'] for r in products}!={r['id'] for r in data['products']}): raise AssertionError('MCP pagination differs from complete owned data')
            for returned, source, fields in ((orders,data['orders'],('status','total')),(products,data['products'],('name','sku','stock_quantity','total_sales'))):
                truth={r['id']:r for r in source}
                def same(left,right,field):
                    if left is None or right is None: return left is right
                    return Decimal(str(left))==Decimal(str(right)) if field in ('total','stock_quantity','total_sales') else left==right
                if any(any(not same(row[f],truth[row['id']][f],f) for f in fields) for row in returned): raise AssertionError('Original tool values differ from source')
            statuses,low=expected_rows(orders,products,source_binding=contract_identity())
            payload=json.loads(json.dumps({'Status_Summary':statuses,'Low_Stock_Products':low},default=lambda v: float(v) if isinstance(v,Decimal) else v))
            payloads[i]=payload
            post(i,'/write_workbook',{**bound(i),'request_id':str(uuid4()),'workbook':payload})
            tool(i,'google_calendar','create_event',{'summary':'Restock Planning Meeting','description':'Restock low stock: '+ '; '.join(p['name']+': stock '+str(p['stock_quantity']) for p in products if p.get('stock_quantity') is not None and p['stock_quantity']<5),
                'start':{'dateTime':'2026-03-10T10:00:00+00:00','timeZone':'UTC'},'end':{'dateTime':'2026-03-10T11:00:00+00:00','timeZone':'UTC'}},request_id=str(uuid4()))
        check(CHECKS[5], len(pagination)==4 and all(p['pages'][-1]['count']<10 for p in pagination))
        check(CHECKS[6], all(measure(i)['reward']==1 for i in (0,1)))
        read=post(0,'/read_workbook',bound(0))['result']
        import base64
        check(CHECKS[7], hashlib.sha256(base64.b64decode(read['base64'])).hexdigest()==read['sha256'])
        wrong=deepcopy(payloads[0]);wrong['Status_Summary'][0]['Total_Revenue']+=0.01
        post(0,'/write_workbook',{**bound(0),'request_id':str(uuid4()),'workbook':wrong})
        bad=measure(0)
        check(CHECKS[8], bad['reward']==0 and bad['evaluation']['upstream_passed'] is True and bad['evaluation']['supplemental_passed'] is False and measure(1)['reward']==1)
        rid=str(uuid4());body={**bound(0),'request_id':rid,'workbook':payloads[0]}
        first=post(0,'/write_workbook',body);second=post(0,'/write_workbook',body)
        check(CHECKS[9], first==second and measure(0)['reward']==1)
        marker=profiles[0].base/'order-analysis-initialized.json';raw=marker.read_bytes();value=json.loads(raw);value['profile']={}
        save(output/'initialization-fault.json',{'ref':marker.relative_to(ROOT).as_posix(),'before_sha256':hashlib.sha256(raw).hexdigest()})
        marker.write_text(json.dumps(value))
        try: unknown=measure(0)
        finally: marker.write_bytes(raw)
        check(CHECKS[10], unknown['reward'] is None and unknown['mask_sample'] is True and measure(0)['reward']==1)
        post(0,'/close_session',bound(0));active.remove(0)
        check(CHECKS[11], measure(1)['reward']==1)
        seed(2)
        check(CHECKS[12], profiles[2].slot==profiles[0].slot and profiles[2].data!=profiles[0].data and measure(2)['reward']==0 and measure(1)['reward']==1)
        for i in (2,1): post(i,'/close_session',bound(i));active.remove(i)
        for i in (0,1):
            post(i,'/seed_session',seeds[i],expected=409)
        check(CHECKS[13], measure(0)['reward'] is None)
        check(CHECKS[14], all(not(p.data/'postmaster.pid').exists() for p in profiles.values()) and not bridge_request('/status')['isolated']['active_sessions'])
        for p in profiles.values(): require_dependents_closed(p)
        after=shared_snapshot()
        check(CHECKS[15], before==after and all((VM/name).read_bytes()==raw for name,raw in other_ledgers.items()))
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)[:500]
        raise
    finally:
        cleanup=[]
        for i in reversed(active):
            try: post(i,'/close_session',bound(i))
            except Exception as exc: cleanup.append(type(exc).__name__)
        for profile in profiles.values():
            for p in profile.base.rglob('*'):
                if p.is_file() and 'pgdata' not in p.parts and p.suffix in ('.json','.txt','.xlsx','.py'):
                    retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        save(output/'http_journal.json',[e for c in clients for e in c.journal])
        save(output/'pagination.json',pagination)
        for p in output.iterdir():
            if p.is_file(): retained[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        receipt={'schema_version':'nemo-original-order-analysis-diagnostic-v1','at':datetime.now(timezone.utc).isoformat(),
            'passed':[c['name'] for c in checks]==list(CHECKS) and all(c['passed'] for c in checks) and not cleanup,
            'checks':checks,'error':error,'cleanup_errors':cleanup,'seeds':seeds,'profiles':{str(i):p.record() for i,p in profiles.items()},
            'measurements':measurements,'retained_sha256':retained,'shared_before':before,'shared_after':after,
            'execution_kind':'scripted_original_task_diagnostic','training_eligible':False,'real_model_calls':0,'training_updates':0,'training_admissions':0,
            'reference_solutions_read':False,'verified_task_bodies_read':False,
            'source_sha256':{**expected_identity(ROOT),**{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in EXTRA_SOURCES}}}
        save(output/'result.json',receipt)
        print(json.dumps({'passed':receipt['passed'],'checks':len(checks),'receipt':(output/'result.json').relative_to(ROOT).as_posix()}))
    return 0 if receipt['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
