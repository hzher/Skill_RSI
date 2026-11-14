"""Cloud/mail adapter prepared for the existing private resource worker.

This module does not register a task or enable scheduling. Diagnostic reward
conversion is separate from runtime admission and from training eligibility.
"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID, uuid5

from adapters.vip_email_evaluation import evaluate
from adapters.vip_email_validation import TITLE
from integrations.email_components import snapshot
from integrations.episode_cluster import require_dependents_closed
from integrations.vip_email_components import VipEmailComponents, METHODS, collect_sources, save_snapshot
from training.email_task_review import TASK_ID
from workenv.core import ResourceError, no_links, read_json, save

REFERENCE_MODE = "original_database_evaluator_with_project_supplement"
REWARD_VERSION = "workenv-vip-original-db-diagnostic-v1"


def scoped_tool_schema(server, name):
    """Publish the existing validate_call scope instead of the wider raw MCP API.

    These are public argument restrictions, not expected business results.
    Resource ownership and cross-field range/grid checks remain server-side.
    """
    text = lambda limit=256: {"type":"string","minLength":1,"maxLength":limit}
    if server == 'woocommerce' and name == 'woo_customers_list':
        props={'page':{'type':'integer','minimum':1,'maximum':64},'perPage':{'type':'integer','const':10}}
    elif server == 'email':
        props={
            'send_email':{'to':{**text(320),'pattern':r'^[^\s,@<>\r\n]+@[^\s,@<>\r\n]+\.[^\s,@<>\r\n]+$'},
                          'subject':{**text(),'pattern':r'^[^\r\n]+$'},'body':text(16384)},
            'read_email':{'email_id':{'type':'string','pattern':r'^[1-9][0-9]{0,12}$'}},
            'get_emails':{'folder':{'type':'string','const':'Sent'},'page':{'type':'integer','minimum':1,'maximum':64},
                          'page_size':{'type':'integer','minimum':1,'maximum':50}}}[name]
    elif server == 'google_sheet':
        props={
            'create_spreadsheet':{'title':{'type':'string','const':TITLE}},
            'list_sheets':{'spreadsheet_id':text()},
            'rename_sheet':{'spreadsheet':text(),'sheet':text(),'new_name':{'type':'string','const':'Top Customers'}},
            'create_sheet':{'spreadsheet_id':text(),'title':{'type':'string','const':'Store Summary'}},
            'update_cells':{'spreadsheet_id':text(),'sheet':{'type':'string','enum':['Top Customers','Store Summary']},
                            'range':{**text(),'pattern':r'^[A-E]([1-9]|10|11):[A-E]([1-9]|10|11)$'},
                            'data':{'type':'array','minItems':1,'maxItems':11,'items':{'type':'array','minItems':1,'maxItems':5,
                                    'items':{'anyOf':[{'type':'number'},{'type':'string','maxLength':4096,'pattern':r'^(?!=)'}]}}}},
            'get_sheet_data':{'spreadsheet_id':text(),'sheet':{'type':'string','enum':['Top Customers','Store Summary']},
                              'range':{**text(),'pattern':r'^[A-E]([1-9]|10|11):[A-E]([1-9]|10|11)$'}}}[name]
    else:
        raise ValueError('Unregistered scoped VIP tool')
    schema={'type':'object','properties':props,'required':list(props),'additionalProperties':False}
    if server == 'google_sheet' and name in ('update_cells','get_sheet_data'):
        schema['allOf']=[{'if':{'properties':{'sheet':{'const':'Store Summary'}}},
                          'then':{'properties':{'range':{'pattern':r'^[AB][1-5]:[AB][1-5]$'}}}}]
    return schema


def request_identity(resource_id, external_id, *, purpose="tool"):
    """Disjoint verifier/tool namespaces; never manufacture a new ID on retry."""
    if purpose not in ("tool", "source"):
        raise ValueError("Unsupported VIP request namespace")
    for value in (resource_id, external_id):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Canonical VIP resource and request UUIDs required")
    return str(uuid5(UUID(resource_id), "workenv-vip-v1/" + purpose + "/" + external_id))


def episode_result(component):
    """Convert only independently classified diagnostic measurements."""
    if (component.get("task_id") != TASK_ID or component.get("reference_mode") != REFERENCE_MODE
        or component.get("schema_version") != "vip-original-database-component-measurement-v1"
        or component.get("runtime_task_activated") is not False
        or any(component.get(k) is not False for k in ("training_eligible", "upstream_reference_used", "official_benchmark_score_available"))
        or component.get("task_passed") is not None or component.get("reward") is not None
        or type(component.get("external_messages_sent")) is not int or component["external_messages_sent"] != 0
        or component.get("source_completeness_verified") is not True or component.get("owned_database_snapshot_verified") is not True):
        raise ResourceError("VIP component scope or input provenance differs")
    original = component.get("original_passed")
    supplemental = component.get("supplemental", {}).get("supplemental_passed")
    if any(value is not None and type(value) is not bool for value in (original, supplemental)):
        raise ResourceError("VIP measurement classification must be boolean or unknown")
    passed = None if original is None or supplemental is None else original and supplemental
    if component.get("component_measurement_passed") is not passed:
        raise ResourceError("VIP combined classification differs")
    return {"schema_version": "vip-original-db-episode-evaluation-v1", "task_id": TASK_ID,
        "evaluation_status": "completed" if type(passed) is bool else "error", "task_passed": passed,
        "reward_version": REWARD_VERSION, "reference_mode": REFERENCE_MODE,
        "original_passed": original, "supplemental_passed": supplemental,
        "component_evaluation_ref": component["evaluation_ref"], "training_eligible": False,
        "upstream_reference_used": False, "official_benchmark_score_available": False, "external_messages_sent": 0}


class VipEmailResourceAdapter:
    task_id = TASK_ID

    def __init__(self, root, cluster, *, build_record=None, runtime_seed=None):
        self.root, self.cluster = Path(root).resolve(), cluster
        if cluster.profile.owner != self.root:
            raise ResourceError("Foreign VIP task resource")
        cluster.require_owner()
        self.runtime_seed = deepcopy(runtime_seed)
        if runtime_seed is not None:
            from integrations.vip_seed import validated_seed
            validated_seed(self.root, runtime_seed)
        if build_record is None:
            path = self.root / "evidence/mainline/email_build.json"
            no_links(path, self.root)
            build_record = read_json(path)
        self.components = VipEmailComponents(self.root, cluster, build_record)
        self.context, self.run = None, None
        self.bindings = {"schema_version": "vip-resource-request-bindings-v1", "profile": cluster.profile.record(), "entries": {}}

    def new_run(self):
        if self.run is not None:
            raise ResourceError("VIP task resource cannot be reopened")
        self.run = self.cluster.profile.base / "vip-task-session"
        no_links(self.run, self.root)
        self.run.mkdir()
        save(self.run / "run.json", {"task_id": self.task_id, "profile": self.cluster.profile.record(),
            "initial_state_ref": "initial_state.json", "training_eligible": False, "server_model_calls": 0})
        save(self.run / "request_bindings.json", self.bindings)
        return self.run

    async def seed(self, run):
        if run != self.run or self.context is not None:
            raise ResourceError("VIP task seed was rebound")
        self.context = self.components.open()
        try:
            await self.context.__aenter__()
            schemas = self.components.schemas
            if len(schemas) != 10 or {(t["server"], t["name"]) for t in schemas} != {(s,n) for s,names in METHODS.items() for n in names}:
                raise ResourceError("VIP original method inventory differs")
            data = snapshot(self.cluster)
            if self.runtime_seed is not None and data['office']['readonly_sha256'] != self.runtime_seed['readonly_sha256']:
                raise ResourceError('VIP restored read-only source differs from runtime seed')
            return {"profile": self.cluster.profile.record(), "customers": len(data["office"]["customers"]),
                "readonly_sha256": data["office"]["readonly_sha256"], "reference_mode": REFERENCE_MODE,
                "external_messages_sent": 0}
        except BaseException:
            context, self.context = self.context, None
            if self.components.opened:
                await context.__aexit__(None, None, None)
            raise

    def guard(self):
        self.cluster.require_owner()
        if self.context is None or self.run is None:
            raise ResourceError("VIP task services are closed")
        no_links(self.run, self.root)
        path = self.run / "request_bindings.json"
        no_links(path, self.root)
        if path.stat().st_size > 256 * 1024 or json.loads(path.read_text()) != self.bindings:
            raise ResourceError("VIP request binding changed")
        self.components.guard()

    def tools(self):
        self.guard()
        tools=deepcopy(self.components.schemas)
        for tool in tools:
            tool['inputSchema']=scoped_tool_schema(tool['server'],tool['name'])
            suffix=' Project task scope: use exactly the published arguments; preserve owned spreadsheet IDs.'
            if tool['server']=='woocommerce':suffix+=' Customer pagination requires page 1..64 and perPage=10; continue until an empty page.'
            if tool['name']=='update_cells':suffix+=' Data must fill the specified rectangular range exactly, using literal values.'
            tool['description']=tool.get('description',tool['name'])+suffix
        return {"tools": tools, "artifact_tools": []}

    def bind_request(self, external_id, *, purpose="tool"):
        self.guard()
        internal = request_identity(self.cluster.profile.resource_id, external_id, purpose=purpose)
        key = purpose + "/" + external_id
        expected = {"external_request_id": external_id, "internal_request_id": internal, "purpose": purpose}
        old = self.bindings["entries"].get(key)
        if old is not None and old != expected:
            raise ResourceError("VIP request binding differs")
        if old is None:
            if len(self.bindings["entries"]) >= 128:
                raise ResourceError("VIP request binding budget")
            self.bindings["entries"][key] = expected
            save(self.run / "request_bindings.json", self.bindings)
        return internal

    async def call_request(self, server, name, arguments, request_id, trace):
        self.guard()
        internal = self.bind_request(request_id)
        result = await self.components.call(server, name, arguments, internal)
        trace.append({"server": server, "name": name, "arguments": deepcopy(arguments), "response": deepcopy(result),
            "external_request_id": request_id, "internal_request_id": internal})
        save(self.run / "trace.json", trace)
        return result

    async def call(self, server, name, arguments, trace):
        raise ValueError("VIP calls require the original resource request UUID")

    async def write(self, payload):
        raise ValueError("VIP task exposes cloud cells and local PG mail, not workbook artifacts")

    async def read(self):
        raise ValueError("VIP task has no workbook artifact")

    async def verify(self):
        self.guard()
        source = self.components.run / "original_customers.json"
        if not source.exists():
            # Verification gathers original read-only pages, never business output.
            page = 0
            def identity():
                nonlocal page
                page += 1
                external = str(uuid5(UUID(self.cluster.profile.resource_id), "vip-source-page/" + str(page)))
                return self.bind_request(external, purpose="source")
            await collect_sources(self.components, identity)
        component = evaluate(self.components)
        result = episode_result(component)
        path = (self.root / component["evaluation_ref"]).with_name("episode_result.json")
        save(path, result)
        return result, path

    async def close(self, run):
        if run != self.run or self.run is None:
            raise ResourceError("VIP close belongs to another run")
        if self.context is not None:
            context, self.context = self.context, None
            await context.__aexit__(None, None, None)
        require_dependents_closed(self.cluster.profile)
        # Preserve committed and unresolved data alike. A fresh resource UUID gets
        # its own original preprocess; closing must never replay or reset mail.
        data = snapshot(self.cluster)
        path = self.run / "closed_snapshot.json"
        save_snapshot(self.root, path, data, self.components.run / "initial_snapshot.json")
        save(self.run / "closed.json", {"profile": self.cluster.profile.record(), "dependent_processes_closed": True,
            "outcome_unknown": self.components.unknown or self.components.mail.unknown,
            "reset_performed": False, "external_messages_sent": 0, "snapshot_ref": path.relative_to(self.root).as_posix()})
