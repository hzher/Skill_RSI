"""Use actual Linux NeMo to resolve project components and build their API schemas."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from nemo_vm import ROOT, ssh_arguments


def main():
    code = '''import os,sys,json,pathlib,hashlib,inspect,asyncio
root=pathlib.Path('/home/workenv/project')
os.environ['NEMO_GYM_EXTRA_ROOTS']=str(root)
os.environ['WORKENV_SESSION_SECRET']='schema-only-no-runtime-credential-'*2
sys.path.insert(0,str(root))
import nemo_gym
from omegaconf import OmegaConf
from nemo_gym import _resolve_under_cwd_or_install
from nemo_gym.config_types import BaseServerConfig
from nemo_gym.base_resources_server import BaseResourcesServerConfig
from nemo_gym.server_utils import ServerClient,GlobalAIOHTTPAsyncClientConfig,set_global_aiohttp_client,global_aiohttp_client_exit,get_response_json,raise_for_status
from integrations.nemo_skill_agent import SkillGeneratorAgent,SkillGeneratorConfig,FrozenModelExecutor
from integrations.nemo_skill_agent import SkillRunRequest
from integrations.nemo_resources import ToolathlonResources
from integrations.nemo_synthetic_resources import SyntheticResourcesServer
from training.seed_contract import prompt_digest
from training.resource_contract import resource_profile,validate_resource_provenance
paths=['responses_api_agents/workagent_skill_generator/app.py','resources_servers/workagent_toolathlon/app.py','resources_servers/workagent_isolated/app.py']
resolved={p:str(_resolve_under_cwd_or_install(p)) for p in paths}
assert all(pathlib.Path(p).is_file() and pathlib.Path(p).is_relative_to(root) for p in resolved.values())
cfg=SkillGeneratorConfig(host='127.0.0.1',port=18704,name='skill_generator',entrypoint='app.py',generator_model={'type':'responses_api_models','name':'policy_model'},executor_model={'type':'responses_api_models','name':'frozen_executor_model'},resources_server={'type':'resources_servers','name':'toolathlon_resources'},generator_policy_id='schema-check-only',generator_model_revision='schema-check-only',tokenizer_revision='schema-check-only',executor_model_revision='schema-check-only')
client=ServerClient(head_server_config=BaseServerConfig(host='127.0.0.1',port=18703),global_config_dict=OmegaConf.create({}))
agent=SkillGeneratorAgent(config=cfg,server_client=client)
schema=agent.setup_webserver().openapi()
assert '/run' in schema['paths'] and '/v1/responses' in schema['paths']
assert 'SkillRunRequest' in schema['components']['schemas']
assert {'provenance','task_prompt','generator_global_step','training_run_id'}<=set(schema['components']['schemas']['SkillRunRequest']['required'])
assert cfg.generator_model.name!=cfg.executor_model.name
isolated_cfg=SkillGeneratorConfig.model_validate(cfg.model_dump()|{'resource_backend':'isolated'})
assert resource_profile(isolated_cfg.resource_backend,isolated_cfg.resources_server.name)['route_prefix']=='/isolated'
resources=ToolathlonResources(config=BaseResourcesServerConfig(host='127.0.0.1',port=18700,name='toolathlon_resources',entrypoint='app.py'),server_client=client)
resource_app=resources.setup_webserver()
mounted=next(route.app for route in resource_app.routes if getattr(route,'path',None)=='/synthetic')
synthetic_schema=mounted.openapi()
assert {'/seed_session','/close_session','/verify','/tool','/tools'}<=set(synthetic_schema['paths'])
isolated_app=next(route.app for route in resource_app.routes if getattr(route,'path',None)=='/isolated')
isolated_schema=isolated_app.openapi()
assert {'/seed_session','/close_session','/verify','/tool','/tools','/write_workbook'}<=set(isolated_schema['paths'])
for update in ({'executor_model':cfg.generator_model.model_dump()},{'num_workers':2}):
    try: SkillGeneratorConfig.model_validate(cfg.model_dump()|update)
    except ValueError: pass
    else: raise AssertionError('Invalid Generator/Executor separation accepted')
raw={'model':'schema-check-only','input':[{'role':'user','content':'Schema-only prompt'}]}
body=SkillRunRequest(responses_create_params=raw,provenance={},episode_id={'rollout_id':'schema-only'},task_id={'taskset':'schema-only','task_id':'schema-only'},generator_global_step=0,training_run_id='schema-only',task_prompt='Schema-only task')
assert prompt_digest(body.task_prompt,raw)==prompt_digest(body.task_prompt,body.responses_create_params.model_dump(mode='json',exclude_none=True,exclude_unset=True))
result={'schema_version':'nemo-component-schema-check-v1','passed':True,'execution_kind':'schema_check_no_model','nemo_version':nemo_gym.__version__,'resolved_components':resolved,'agent_is_abstract':inspect.isabstract(SkillGeneratorAgent),'real_model_calls':0,'training_updates':0,'api_paths':sorted(schema['paths'])}
result['synthetic_resource_api_paths']=sorted(synthetic_schema['paths'])
result['isolated_resource_api_paths']=sorted(isolated_schema['paths'])
async def verify_resource_identity():
    session=set_global_aiohttp_client(GlobalAIOHTTPAsyncClientConfig(global_aiohttp_connector_limit=4,global_aiohttp_connector_limit_per_host=2))
    live=ServerClient(head_server_config=BaseServerConfig(host='127.0.0.1',port=18703),global_config_dict=OmegaConf.create({'toolathlon_resources':{'resources_servers':{'toolathlon_native':{'host':'127.0.0.1','port':18700,'entrypoint':'nemo_resources.py'}}}}))
    try:
        observed={}
        for backend in ('shared','isolated'):
            profile=resource_profile(backend,'toolathlon_resources')
            response=await live.get(server_name=profile['server_name'],url_path=profile['route_prefix']+'/provenance')
            await raise_for_status(response)
            provenance=await get_response_json(response)
            validate_resource_provenance(profile,provenance)
            observed[backend]={'environment_version':provenance['environment_version'],'resource_backend':provenance['resource_backend']}
        return observed
    finally: await session.close()
result['live_resource_identity_via_nemo_sdk']=asyncio.run(verify_resource_identity())
global_aiohttp_client_exit()
names=['integrations/nemo_skill_agent.py','integrations/nemo_resources.py','integrations/nemo_synthetic_resources.py','integrations/model_usage.py','training/generator_runtime.py','training/seed_contract.py','training/skill_contract.py','training/resource_contract.py','responses_api_agents/workagent_skill_generator/app.py','resources_servers/workagent_toolathlon/app.py','resources_servers/workagent_isolated/app.py']
result['source_sha256']={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
print(json.dumps(result))
'''
    if sys.platform == "linux":
        code = code.replace("root=pathlib.Path('/home/workenv/project')", "root=pathlib.Path(" + repr(str(ROOT)) + ")")
        command = [str(ROOT / ".runtime/nemo-venv/bin/python"), "-"]
    else:
        command = ssh_arguments() + ["/home/workenv/nemo-venv/bin/python -"]
    completed = subprocess.run(command, input=code.encode(), cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if completed.returncode:
        print(completed.stderr.decode("utf-8", "replace"))
        return completed.returncode
    result = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
    for name, expected in result["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Guest component differs from current host source: " + name)
    result["source_verified_against_guest"] = True
    result["execution_host"] = "native_linux" if sys.platform == "linux" else "windows_managed_linux_vm"
    target = ROOT / "evidence/mainline/nemo_component_schema.json"
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
