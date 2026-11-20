"""Retry an explicit account capacity error only while no task action was attempted."""
import json
from pathlib import Path
import re
import time

from console_common import STATE, atomic, digest, read


def retry_window(native, thread_ids, expected_thread, episode, path, broker_error, attempt):
    from skillrl.codex_chatgpt import read_events
    if (native.get('complete') or native.get('timeout') or native.get('unexpected_tools')
            or native.get('invalid_jsonl_lines') or native.get('missing_tool_returns')
            or episode.actions or episode.done or broker_error or len(thread_ids)>1
            or (expected_thread and thread_ids and thread_ids[0]!=expected_thread)):
        return None
    broker, invalid = read_events(path/'broker.jsonl')
    mcp, invalid_mcp = read_events(path/'mcp.jsonl')
    if invalid or invalid_mcp or any(row.get('tool')=='act' for row in broker):
        return None
    if any((r.get('request') or {}).get('params',{}).get('name')=='act' for r in mcp):
        return None
    msgs = [json.dumps(e, ensure_ascii=False) for e in native.get('errors',[])]
    marker = re.compile(r'usage.limit|rate.limit|quota.exceeded|capacity limit|too many requests|\b429\b',re.I)
    harmless = re.compile(r'reconnecting',re.I)
    if not any(marker.search(m) for m in msgs) or any(not (marker.search(m) or harmless.search(m)) for m in msgs):
        return None
    now=time.time()
    limits=read(STATE/'account-models.json',{}) or {}
    resets=[]
    if now-limits.get('checked_at',0)<=600:
        for window in (limits.get('rate_limits') or {}).values():
            if (isinstance(window,dict) and (window.get('usedPercent') or 0)>=100
                    and (window.get('resetsAt') or 0)>now):
                resets.append(window['resetsAt']+5)
    for msg in msgs:
        for n,unit in re.findall(r'(?:retry after|try again in|retry in)\s+(\d+)\s*(seconds?|minutes?|hours?)',msg,re.I):
            resets.append(now+int(n)*{'s':1,'m':60,'h':3600}[unit[0].lower()]+2)
        for epoch in re.findall(r'"(?:resets_at|resetsAt)"\s*:\s*(\d{10})\b',msg):
            if int(epoch)>now: resets.append(int(epoch)+5)
    return {'retry_at':max(resets) if resets else now+min(1800,300*2**(attempt-1)),
            'reset_source':'provider_or_recent_account_window' if resets else 'bounded_backoff',
            'reason':'explicit_capacity_zero_actions'}


def archive_attempt(path, number, native, threads, previous, window):
    directory=path/'capacity-retries'/f'{number:02d}'
    directory.mkdir(parents=True,exist_ok=False)
    for name in ('actor','host-launch.stdout','host-launch.stderr','mcp.jsonl','broker.jsonl','tools'):
        target=path/name
        if target.exists(): target.rename(directory/name)
    files={str(f.relative_to(directory)):digest(f) for f in sorted(directory.rglob('*')) if f.is_file()}
    receipt={'number':number,'native':native,'thread_ids':threads,'requested_thread':previous,
             'task_actions':0,'applied':False,'recorded_at':time.time(),'files':files,**window}
    atomic(directory/'receipt.json',receipt)
    return {'number':number,'receipt':str((directory/'receipt.json').relative_to(path)),
            'sha256':digest(directory/'receipt.json'),**window}


def wait_until(target):
    while True:
        left=target-time.time()
        if left<=0: return
        time.sleep(min(left,30))
