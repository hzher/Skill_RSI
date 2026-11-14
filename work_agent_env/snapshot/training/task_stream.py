"""Propose related task-stream edges using public metadata, without inventing lineage.

Task difficulty/similarity is a retrieval heuristic, never proof of independence.
Candidates are byte-bound and stay outside the reviewed training dataset until
their source/alias review passes the existing EvaluationFirewall.
"""
import re
import hashlib
from integrations.benchmark_tasks.contracts import digest


def task_tools(task):
    return {row['name'] for row in task['tools'] if row.get('kind') == 'mcp_server'} - {'local','terminal','filesystem'}


def prompt_blob(task):
    raw=task['prompt'].encode('utf-8')
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def propose_edges(experiences, tasks, *, evaluation_manifest, parent, limit=20, reserved_prompt_blobs=None):
    reserved = set(evaluation_manifest['task_ids']) | set(evaluation_manifest['family_roots'])
    reserved_prompt_blobs=set(reserved_prompt_blobs or [])
    proposed, rejected = [], []
    for experience_id, record, view in experiences:
        source = record['task']
        if view['result']['status'] != 'measured':
            rejected.append({'experience_id':experience_id,'reason':'unknown_original_score'})
            continue
        if source['task_id'] in reserved:
            rejected.append({'experience_id':experience_id,'reason':'exact_reserved_evaluation_identity'})
            continue
        if prompt_blob(source) in reserved_prompt_blobs:
            rejected.append({'experience_id':experience_id,'reason':'exact_reserved_prompt_blob'})
            continue
        words = set(re.findall(r'[a-z][a-z0-9]+',source['prompt'].lower()))
        options=[]
        for target in tasks:
            if (target['uid']==source['uid'] or target['task_id'] in reserved
                    or digest(target['prompt'])==digest(source['prompt']) or prompt_blob(target) in reserved_prompt_blobs):
                continue
            common=task_tools(source) & task_tools(target)
            if not common:continue
            target_words=set(re.findall(r'[a-z][a-z0-9]+',target['prompt'].lower()))
            similarity=len(common)/max(1,len(task_tools(source)|task_tools(target)))
            lexical=len(words & target_words)/max(1,len(words|target_words))
            options.append((similarity+lexical,target['uid'],target,common))
        if not options:
            rejected.append({'experience_id':experience_id,'reason':'no_related_nonidentical_target'})
            continue
        score,_,target,common=max(options,key=lambda row:(row[0],row[1]))
        pair={'schema_version':'workagent-training-pair-v1','group_id':'pair_'+digest([experience_id,target['uid'],parent])[:20],
              'experience_id':experience_id,'downstream_task':target,'parent':parent,
              'seed_provenance':None,'downstream_provenance':None,
              'view_budget':{'max_steps':30,'observation_chars':2000}}
        proposed.append({'pair':pair,'source_task_uid':source['uid'],'source_experience_sha256':digest(record),
            'downstream_task_sha256':digest(target),'common_business_tools':sorted(common),'similarity_score':score,
            'review_status':'candidate_only','training_admitted':False,
            'unresolved':['source family/alias/ancestor review against reserved evaluation inventory',
                          'exact rendered Curator context and frozen SelfJudge feedback review'],
            'pair_sha256':digest(pair)})
        if len(proposed)>=limit:break
    return {'schema_version':'workagent-task-stream-proposals-v1','proposals':proposed,'rejected':rejected,
            'exact_reserved_prompt_blob_check':bool(reserved_prompt_blobs),
            'evaluation_manifest_sha256':digest(evaluation_manifest),'training_admitted':False,
            'reviewed_pairs_written':0}
