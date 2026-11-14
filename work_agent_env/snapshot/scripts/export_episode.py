"""Export diagnostic interactions; explicitly exclude them from SFT/RL targets."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workenv.core import ROOT,owned_run,read_json,run_directory,save,now,digest
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run-id',required=True)
args=parser.parse_args()
run=run_directory(args.run_id)
metadata=owned_run(run)
events=read_json(run/'trace.json')
model_output_label=None if metadata['execution_kind']=='external_tool_session' else False
destination=run/'exports'
destination.mkdir(exist_ok=True)
with (destination/'events.jsonl').open('w',encoding='utf-8',newline='\n') as stream:
    for index,event in enumerate(events):
        stream.write(json.dumps({'schema_version':'diagnostic-events-v0.1','run_id':run.name,'index':index,
            'task_id':metadata['task_id'],'execution_kind':metadata['execution_kind'],'is_model_output':model_output_label,
            'eligible_for_sft':False,'eligible_for_rl':False,'event':event},ensure_ascii=False)+'\n')
save(destination/'manifest.json',{'exported_at':now(),'events':len(events),'source_run':run.name,
    'source_run_metadata_sha256':digest(run/'run.json'),'source_trace_sha256':digest(run/'trace.json'),
    'evaluation_status':metadata.get('evaluation_status'),'task_passed':metadata.get('task_passed'),
    'observed_reward':metadata.get('reward'),'reward_version':metadata.get('reward_version'),
    'source_refs':{name:dict(present=(run/name).is_file(),sha256=digest(run/name) if (run/name).is_file() else None)
                   for name in ('initial_state.json','final_state.json','evaluation.json')},
    'model_id':metadata['model_id'],'action_provenance':metadata.get('action_provenance','scripted'),
    'is_model_output':model_output_label,'eligible_for_sft':False,'eligible_for_rl':False,
    'missing_training_fields':['real_model_messages','sampled_token_ids','action_masks','sampling_probabilities','policy_checkpoint'],
    'purpose':'debugging exchange format; not model-generated training data'})
print(json.dumps({'run_id':run.name,'events':len(events),'training_eligible':False},indent=2))
