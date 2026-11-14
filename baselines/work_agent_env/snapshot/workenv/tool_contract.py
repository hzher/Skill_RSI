"""Immutable local tool signatures and fresh public schema snapshots.

This deliberately supports only the small object/string vocabulary we publish.
Business authorization, paths, transactions and budgets remain in tool handlers.
"""
import hashlib
import json
from types import MappingProxyType


FILE_DEFINITIONS=(
    ("list_files","List public workspace files.",()),
    ("read_file","Read a UTF-8 public workspace file (max 512 KiB).",("path",)),
    ("write_file","Write UTF-8 output/ file (max 512 KiB).",("path","content")))
SERVICE_DEFINITIONS=tuple((name,"Operate the current synthetic support queue; no external notifications.",fields)
    for name,fields in (("tickets_list",()),("tickets_get",("ticket_id",)),("directory_list",()),
                       ("tickets_triage",("ticket_id",)),("tickets_assign",("ticket_id","team","assignee","due_at")),
                       ("tickets_request_approval",("ticket_id",))))
SERVICE_ARGUMENTS=MappingProxyType({name:fields for name,_,fields in SERVICE_DEFINITIONS})


def definitions(adapter):
    if adapter=="local_files":return FILE_DEFINITIONS
    if adapter=="local_tickets":return FILE_DEFINITIONS+SERVICE_DEFINITIONS
    raise ValueError("Public local tool contracts require local_files or local_tickets")


def public_schemas(adapter):
    return [{"name":name,"description":description,"parameters":{
        "type":"object","properties":{field:{"type":"string"} for field in fields},
        "required":list(fields),"additionalProperties":False}}
        for name,description,fields in definitions(adapter)]


def validate_arguments(name,arguments,adapter):
    if not isinstance(name,str) or not isinstance(arguments,dict):
        raise ValueError("Tool name must be a string and arguments an object")
    signatures={key:fields for key,_,fields in definitions(adapter)}
    if name not in signatures or set(arguments)!=set(signatures[name]):
        raise ValueError("Unknown tool or invalid tool arguments")
    if any(not isinstance(value,str) for value in arguments.values()):
        raise ValueError("Tool arguments must be strings")


def capability_contract(adapter):
    tools=public_schemas(adapter)
    return {"schema_version":"local-tool-contract-v0.1","runtime_adapter":adapter,"tools":tools,
            "tools_sha256":hashlib.sha256(json.dumps(tools,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest(),
            "validation_vocabulary":["object","string","required","additionalProperties=false"],
            "business_authorization":"handler-enforced; structural acceptance does not authorize a business transition",
            "is_os_sandbox":False,"training_eligible":False}
