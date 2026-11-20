#!/usr/bin/env python3
"""Test subprocess: drive the real benchmark MCP server with a fixed spectrum submission."""
import json
import os
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path


def event(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


def main(args):
    assert "OPENAI_API_KEY" not in os.environ
    if args[-2:] == ["login", "status"]:
        print("Logged in using ChatGPT (test fixture)")
        return
    if args == ["--version"]:
        print("scripted-codex-fixture")
        return
    assert args[0] == "exec" and "--ignore-user-config" in args
    assert not any("http://" in arg for arg in args)
    settings = next(arg for arg in args if arg.startswith("mcp_servers.benchmark="))
    mcp = tomllib.loads(settings)["mcp_servers"]["benchmark"]
    resumed = "resume" in args
    thread = args[-2] if resumed else str(uuid.uuid4())
    event("thread.started", thread_id=thread)
    event("turn.started")
    requests = [
        {"id": 0, "method": "tools/list"},
        {"id": 1, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
        {"id": 2, "method": "tools/call", "params": {"name": "act", "arguments": {"action": {"transmitters": []}}}},
    ]
    rpc = "".join(json.dumps({"jsonrpc": "2.0", **row}) + "\n" for row in requests)
    child = subprocess.run([mcp["command"], *mcp["args"]], input=rpc, text=True,
                           capture_output=True, check=True, timeout=30)
    replies = [json.loads(line) for line in child.stdout.splitlines()]
    assert {tool["name"] for tool in replies[0]["result"]["tools"]} == {"status", "act"}
    for request, reply in zip(requests[1:], replies[1:], strict=True):
        item = {"type": "mcp_tool_call", "server": "benchmark", "tool": request["params"]["name"],
                "id": str(request["id"]), "arguments": request["params"]["arguments"]}
        assert reply["id"] == request["id"] and not reply["result"]["isError"]
        event("item.started", item=item)
        event("item.completed", item={**item, "result": reply["result"]})
    assert json.loads(replies[-1]["result"]["content"][0]["text"])["done"]
    Path(args[args.index("-o") + 1]).write_text("Submitted the fixed test action.", encoding="utf-8")
    event("turn.completed", usage={"input_tokens": 20 if resumed else 10,
                                    "output_tokens": 4 if resumed else 2})


if __name__ == "__main__":
    main(sys.argv[1:])
