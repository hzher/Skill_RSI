"""Standalone MCP stdio transport copied into the task-owned CLI container."""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid


TOOLS = [
    {"name": "status", "description": "Read the current official task input, action schema, environment directory and completion state.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "act", "description": "Apply one action in the official benchmark environment. Match the current action schema returned by status. The result includes the official observation and next action schema. Each accepted action consumes the benchmark's own budget.", "inputSchema": {"type": "object", "properties": {"action": {"type": "object"}}, "required": ["action"], "additionalProperties": False}},
]


def dispatch(request, session):
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "benchmark", "version": "1.0.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method != "tools/call":
        raise ValueError("Unsupported MCP method")
    params = request["params"]
    operation_id = session + "-" + str(request["id"])
    body = json.dumps({"operation_id": operation_id, "tool": params["name"], "arguments": params.get("arguments", {})}).encode()
    message = urllib.request.Request(os.environ["BENCHMARK_RPC_URL"], data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(message, timeout=900) as response:
        return json.load(response)


def stop_decision(result):
    if result.get("isError"):
        return {}
    status = json.loads(result["content"][0]["text"])
    if status["done"]:
        return {}
    return {"decision": "block", "reason": "The official task is still unfinished. Continue using benchmark.status and benchmark.act until it finishes within the original turn and time budgets."}


def main():
    session = uuid.uuid4().hex
    if sys.argv[1:] == ["--stop"]:
        json.load(sys.stdin)
        result = dispatch({"id": "stop", "method": "tools/call", "params": {"name": "status", "arguments": {}}}, session)
        print(json.dumps(stop_decision(result)), flush=True)
        return
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            result = {"jsonrpc": "2.0", "id": request["id"], "result": dispatch(request, session)}
        except Exception as error:
            result = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": type(error).__name__ + ": " + str(error)}}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
