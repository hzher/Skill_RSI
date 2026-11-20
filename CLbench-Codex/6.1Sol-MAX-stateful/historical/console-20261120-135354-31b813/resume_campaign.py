"""Resume the frozen shared collector, gating resources before new attempts only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
FRAMEWORK = ROOT / 'sources/Skill_RSI/framework'
sys.path.insert(0, str(FRAMEWORK))
from skillrl import collect
from skillrl.storage import atomic, identifier


class BetweenTasksStop(BaseException):
    """Leave the current completed attempt sealed; never skip to later tasks."""


def resource_snapshot(config, *, host_timeout=20):
    host_code = """import ctypes,json
class MemoryStatus(ctypes.Structure):
    _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in ('total_physical','available_physical','total_pagefile','available_pagefile','total_virtual','available_virtual','extended')]
value=MemoryStatus(); value.length=ctypes.sizeof(value)
if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)): raise OSError('GlobalMemoryStatusEx failed')
print(json.dumps({'available_physical':value.available_physical,'available_commit':value.available_pagefile}))
"""
    host = json.loads(subprocess.check_output([config['host_python'], '-c', host_code], text=True, timeout=host_timeout))
    linux = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
             if line.startswith(('MemAvailable:', 'SwapFree:'))}
    return {'checked_at': time.time(), 'host': host, 'linux': linux, 'free_disk': shutil.disk_usage(ROOT).free}


def resource_problem(snapshot):
    if snapshot['host']['available_physical'] < 768*1024**2:
        return 'host_physical_memory_below_768_MiB'
    if snapshot['host']['available_commit'] < 2*1024**3:
        return 'host_commit_headroom_below_2_GiB'
    if snapshot['linux']['MemAvailable'] < 512*1024**2:
        return 'linux_available_memory_below_512_MiB'
    if snapshot['free_disk'] < 4*1024**3:
        return 'disk_headroom_below_4_GiB'
    return None


def await_resources(config, log, case_name, wait, sampler=resource_snapshot, sleep=time.sleep):
    while True:
        if (ROOT/'reports/STOP_BEFORE_NEXT_TASK').exists():
            raise BetweenTasksStop('operator_requested_stop_before_next_task')
        try:
            resources = sampler(config)
        except Exception as error:
            raise BetweenTasksStop('resource_probe_failed: '+type(error).__name__) from error
        reason = resource_problem(resources)
        state = {'case':case_name,'resources':resources,'stop_reason':reason,'waiting_for_memory':bool(reason and wait)}
        with (log/'resources.jsonl').open('a') as handle:
            handle.write(json.dumps(state)+'\n')
        atomic(log/'resource-state.json',state)
        if not reason:
            return
        if not wait:
            raise BetweenTasksStop(reason)
        sleep(20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--max-new-tasks', type=int)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--wait-for-memory', action='store_true')
    args = parser.parse_args()
    identifier(args.run_id)
    if args.max_new_tasks is not None and args.max_new_tasks < 1:
        parser.error('--max-new-tasks must be positive')
    config = json.loads((ROOT/'config/account.frozen.json').read_text())
    plan = json.loads((ROOT/'config/independent-301.json').read_text())
    for repo, expected in ((FRAMEWORK.parent, config['framework_commit']), (Path(config['repository']), config['benchmark_commit'])):
        windows = subprocess.check_output(['wslpath','-w',str(repo)], text=True, timeout=20).strip()
        def git(*argv):
            return subprocess.check_output(['/mnt/d/Git/cmd/git.exe','-C',windows,*argv],text=True,timeout=30).strip()
        if git('rev-parse','HEAD') != expected or git('status','--porcelain'):
            raise ValueError('Frozen source checkout changed: '+str(repo))
    network = json.loads(subprocess.check_output(['docker','network','inspect',config['network']],text=True,timeout=20))[0]
    if not network['Internal'] or network['Labels'].get('clbench.campaign') != str(ROOT):
        raise ValueError('Campaign network identity changed')
    snapshot = resource_snapshot(config)
    if args.check:
        print(json.dumps({'source_pins_verified':True,'tasks':len(collect.select_tasks(plan,'independent')),
                          'resources':snapshot,'resource_problem':resource_problem(snapshot),'model_calls':0}),flush=True)
        return 0
    log = ROOT/'reports/resume'/args.run_id
    log.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(Path(__file__),log/'launcher.py')
    atomic(log/'entry.json', {'started_at':time.time(),'pid':os.getpid(),'framework_commit':config['framework_commit'],
           'benchmark_commit':config['benchmark_commit'],'config_sha256':hashlib.sha256((ROOT/'config/account.frozen.json').read_bytes()).hexdigest(),
           'ordered_plan_sha256':hashlib.sha256((ROOT/'config/independent-301.json').read_bytes()).hexdigest(),
           'launcher_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'max_new_tasks':args.max_new_tasks,'wait_for_memory':args.wait_for_memory,
           'collector':'skillrl.collect.run_collection','initial_resources':snapshot})
    original = collect.execute
    started_new = 0
    def guarded(config, family, index, output):
        nonlocal started_new
        path = Path(output)
        if not (path/'result.json').exists():
            if (path/'intent.json').exists():
                raise BetweenTasksStop('unresolved_attempt_requires_reconciliation: '+path.name)
            if args.max_new_tasks is not None and started_new >= args.max_new_tasks:
                raise BetweenTasksStop('requested_batch_boundary')
            await_resources(config,log,path.name,args.wait_for_memory)
            started_new += 1
            print(json.dumps({'starting_case':path.name,'new_attempts':started_new}),flush=True)
        result = original(config, family, index, output)
        if not result.get('resource_closed'):
            raise BetweenTasksStop('attempt_closed_with_unresolved_resources: '+path.name)
        return result
    collect.execute = guarded
    receipt = {'run_id':args.run_id,'collection_exited':False,'campaign_complete':False}
    exit_code = 1
    try:
        result = collect.run_collection(config, plan, 'independent', ROOT/'runs/full-01/collection')
        exit_code = 0 if result['complete'] else 1
        receipt.update(collection_exited=True,collection_complete=result['complete'],stop_reason=result.get('stop_reason'))
        atomic(ROOT/'runs/full-01/exit.json', {'phase':'full','exit_code':exit_code,'ended_at':time.time(),'resumed_by':args.run_id})
    except BetweenTasksStop as error:
        exit_code = 0 if str(error)=='requested_batch_boundary' else 75
        receipt.update(stop_reason=str(error),stopped_between_tasks=True)
    except Exception as error:
        receipt.update(failure={'type':type(error).__name__,'message':str(error)})
        raise
    finally:
        collect.execute = original
        receipt.update(ended_at=time.time(),exit_code=exit_code,new_attempts_started=started_new)
        atomic(log/'exit.json',receipt)
        print(json.dumps(receipt),flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
